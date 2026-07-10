"""Walk-forward LightGBM cross-sectional ranker.

    python3 -m quant.models.train_ranker --walk-forward
    python3 -m quant.models.train_ranker --walk-forward --start-year 2004

Protocol (institutional-grade, no excuses):
  - For each test year Y: train on all data from TRAIN_MIN_YEAR to Y-1,
    with the last EMBARGO_DAYS trading days before Y purged (the 5-day
    forward label would otherwise leak across the boundary).
  - Predict every day of year Y. Concatenated predictions across years form
    a single out-of-sample record — no fold ever sees its own future.
  - Label: cross-sectional percentile rank of fwd_ret_5d (robust to fat
    tails; the model learns ordering, not magnitudes).

Outputs per-fold models to quant/_staging/models/ and out-of-sample
predictions to BigQuery quant.predictions (walk_forward run tag).
"""

import argparse
import datetime as dt
import os
import sys

import lightgbm as lgb
import numpy as np
import pandas as pd
from google.cloud import bigquery

from quant.config import STAGING_DIR, T_FEATURES, T_PREDICTIONS
from quant.data.bq import client, ensure_table, load_df

TRAIN_MIN_YEAR = 2001
EMBARGO_DAYS = 10          # > label horizon (5d) with margin
LABEL = "fwd_ret_5d"

FEATURES = [
    # cross-sectional z-scores are the primary inputs
    "z_ret_1d", "z_ret_5d", "z_ret_10d", "z_ret_21d", "z_ret_63d", "z_ret_126d",
    "z_mom_12m_ex1m", "z_vol_21d", "z_vol_63d", "z_parkinson_21d",
    "z_volume_ratio", "z_amihud_21d", "z_gap_mean_5d", "z_intraday_mean_5d",
    "z_high_52w_prox", "z_sma50_dist", "z_sma200_dist", "z_skew_63d",
    "z_beta_63d", "z_log_adv",
    # raw levels that carry information beyond the daily cross-section
    "vol_21d", "beta_63d", "high_52w_prox",
    # calendar
    "dow", "month",
]

LGB_PARAMS = {
    "objective": "regression",
    "metric": "l2",
    "learning_rate": 0.05,
    "num_leaves": 63,
    "min_data_in_leaf": 500,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "verbosity": -1,
    "num_threads": 0,
}
NUM_ROUNDS = 400

PRED_SCHEMA = [
    bigquery.SchemaField("date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("score", "FLOAT64"),
    bigquery.SchemaField("fold", "INT64"),
    bigquery.SchemaField("run", "STRING"),
]


def load_features() -> pd.DataFrame:
    cols = ["date", "symbol"] + FEATURES + [LABEL, "fwd_ret_1d", "vol_63d", "adv63"]
    cols = list(dict.fromkeys(cols))
    sql = f"SELECT {', '.join(cols)} FROM `{T_FEATURES}` WHERE {LABEL} IS NOT NULL"
    print("Pulling features from BigQuery ...")
    df = client().query(sql).result().to_dataframe(create_bqstorage_client=True)
    df["date"] = pd.to_datetime(df["date"])
    for c in df.columns:
        if df[c].dtype == "float64":
            df[c] = df[c].astype("float32")
    df = df.sort_values(["date", "symbol"]).reset_index(drop=True)
    print(f"{len(df):,} rows, {df['date'].dt.year.min()}–{df['date'].dt.year.max()}")
    return df


def rank_label(df: pd.DataFrame) -> pd.Series:
    """Cross-sectional percentile rank of the forward return, per date."""
    return df.groupby("date")[LABEL].rank(pct=True).astype("float32")


def walk_forward(df: pd.DataFrame, start_year: int, end_year: int) -> pd.DataFrame:
    df = df.copy()
    df["y"] = rank_label(df)
    trading_days = pd.Series(sorted(df["date"].unique()))
    preds = []
    os.makedirs(os.path.join(STAGING_DIR, "models"), exist_ok=True)

    for i, year in enumerate(range(start_year, end_year + 1)):
        test_start = pd.Timestamp(f"{year}-01-01")
        test_end = pd.Timestamp(f"{year}-12-31")
        # Purge: drop the EMBARGO_DAYS trading days right before the test year.
        pre_test_days = trading_days[trading_days < test_start]
        if len(pre_test_days) < 250:
            print(f"{year}: not enough history, skipping")
            continue
        purge_cutoff = pre_test_days.iloc[-EMBARGO_DAYS]

        train = df[(df["date"] >= f"{TRAIN_MIN_YEAR}-01-01")
                   & (df["date"] < purge_cutoff)]
        test = df[(df["date"] >= test_start) & (df["date"] <= test_end)]
        if test.empty:
            continue

        dtrain = lgb.Dataset(train[FEATURES], label=train["y"])
        model = lgb.train(LGB_PARAMS, dtrain, num_boost_round=NUM_ROUNDS)
        model.save_model(os.path.join(STAGING_DIR, "models", f"ranker_{year}.txt"))

        out = test[["date", "symbol", LABEL, "fwd_ret_1d", "vol_63d", "adv63"]].copy()
        out["score"] = model.predict(test[FEATURES]).astype("float32")
        out["fold"] = i
        # daily rank IC for a quick health read
        ic = (out.groupby("date")
                 .apply(lambda g: g["score"].corr(g[LABEL], method="spearman"))
                 .mean())
        print(f"{year}: train {len(train):,} rows → test {len(test):,} rows, "
              f"mean daily rank-IC {ic:+.4f}", flush=True)
        preds.append(out)

    return pd.concat(preds, ignore_index=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--walk-forward", action="store_true")
    p.add_argument("--start-year", type=int, default=2003)
    p.add_argument("--end-year", type=int, default=dt.date.today().year)
    p.add_argument("--run-tag", default="wf_v1")
    args = p.parse_args()
    if not args.walk_forward:
        p.print_help()
        sys.exit(1)

    df = load_features()
    preds = walk_forward(df, args.start_year, args.end_year)

    ensure_table(T_PREDICTIONS, PRED_SCHEMA, partition_field="date",
                 clustering=["run"])
    up = preds[["date", "symbol", "score", "fold"]].copy()
    up["date"] = up["date"].dt.date
    up["run"] = args.run_tag
    client().query(
        f"DELETE FROM `{T_PREDICTIONS}` WHERE run = '{args.run_tag}'").result()
    load_df(T_PREDICTIONS, up, schema=PRED_SCHEMA)
    print(f"Stored {len(up):,} out-of-sample predictions (run={args.run_tag})")

    # Local parquet copy for the backtester (includes labels).
    path = os.path.join(STAGING_DIR, f"preds_{args.run_tag}.parquet")
    preds.to_parquet(path, index=False)
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
