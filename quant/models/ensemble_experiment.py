"""Seed-ensemble + XGBoost experiment — the two remaining cheap model levers.

    python3 -m quant.models.ensemble_experiment --run

Tests on identical purged folds (2019–2026), against the single-GBM
reference:
  ENS5   five LightGBMs (different seeds, feature/bagging fractions),
         per-day rank scores averaged — the CORRECT ensemble (strong+strong),
         unlike yesterday's strong+weak dilution
  XGB    XGBoost with comparable capacity (same-class sanity check)
Metrics: mean daily rank-IC + production tranche simulation (net@5bp).
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.config import STAGING_DIR
from quant.models.train_ranker import (LGB_PARAMS, NUM_ROUNDS, V2_FEATURES,
                                       load_features, rank_label)
from quant.models.model_zoo import _mat

TEST_YEARS = list(range(2019, 2027))
EMBARGO_DAYS = 10
N_SEEDS = 5


def fit_gbm_seed(Xtr, ytr, Xte, seed: int):
    import lightgbm as lgb
    params = dict(LGB_PARAMS)
    params.update({
        "seed": seed,
        "bagging_seed": seed * 7 + 1,
        "feature_fraction_seed": seed * 13 + 3,
        "feature_fraction": [0.7, 0.75, 0.8, 0.85, 0.9][seed % 5],
        "bagging_fraction": [0.7, 0.8, 0.9][seed % 3],
    })
    m = lgb.train(params, lgb.Dataset(Xtr, label=ytr),
                  num_boost_round=NUM_ROUNDS)
    return m.predict(Xte)


def fit_xgb(Xtr, ytr, Xte):
    import xgboost as xgb
    dtr = xgb.DMatrix(_mat(Xtr), label=ytr.to_numpy(dtype="float32"))
    dte = xgb.DMatrix(_mat(Xte))
    m = xgb.train({"objective": "reg:squarederror", "eta": 0.05,
                   "max_depth": 6, "min_child_weight": 500,
                   "subsample": 0.8, "colsample_bytree": 0.8,
                   "lambda": 1.0, "nthread": 0},
                  dtr, num_boost_round=NUM_ROUNDS)
    return m.predict(dte)


def run():
    df = load_features(v2=True)
    feats = V2_FEATURES
    df["y"] = rank_label(df)
    trading_days = pd.Series(sorted(df["date"].unique()))

    preds = {m: [] for m in ["GBM1", "ENS5", "XGB"]}
    print(f"{'year':>5s} {'GBM1':>8s} {'ENS5':>8s} {'XGB':>8s}")
    for year in TEST_YEARS:
        test_start = pd.Timestamp(f"{year}-01-01")
        pre = trading_days[trading_days < test_start]
        purge = pre.iloc[-EMBARGO_DAYS]
        train = df[df["date"] < purge]
        test = df[(df["date"] >= test_start) & (df["date"] <= f"{year}-12-31")]
        if test.empty:
            continue
        Xtr, ytr, Xte = train[feats], train["y"], test[feats]
        base = test[["date", "symbol", "fwd_ret_5d", "fwd_ret_1d",
                     "vol_63d", "adv63"]].copy()

        seed_scores = [fit_gbm_seed(Xtr, ytr, Xte, s) for s in range(N_SEEDS)]
        tmp = base.copy()
        ranked = []
        for s in seed_scores:
            tmp["score"] = s
            ranked.append(tmp.groupby("date")["score"].rank(pct=True).values)
        ens = np.mean(ranked, axis=0)
        xgb_s = fit_xgb(Xtr, ytr, Xte)

        ics = {}
        for name, s in [("GBM1", seed_scores[0]), ("ENS5", ens), ("XGB", xgb_s)]:
            out = base.copy()
            out["score"] = s
            preds[name].append(out)
            ics[name] = (out.groupby("date")
                         .apply(lambda g: g["score"].corr(g["fwd_ret_5d"],
                                                          method="spearman"))
                         .mean())
        print(f"{year:>5d} {ics['GBM1']:>+8.4f} {ics['ENS5']:>+8.4f} "
              f"{ics['XGB']:>+8.4f}", flush=True)

    from quant.backtest.portfolio_sim import simulate_tranches
    print("\n=== tranche simulation (net@5bp, 2019-2026 OOS) ===")
    for name, chunks in preds.items():
        p = pd.concat(chunks, ignore_index=True)
        res = simulate_tranches(p)
        r = res["net_ret"]
        sh = r.mean() / r.std() * np.sqrt(252)
        cagr = (1 + r).cumprod().iloc[-1] ** (252 / len(r)) - 1
        print(f"{name:6s} Sharpe={sh:5.2f}  CAGR={cagr:+7.1%}")
        p.to_parquet(f"{STAGING_DIR}/preds_ens_{name}.parquet", index=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()
    if not args.run:
        ap.print_help()
        sys.exit(1)
    run()
