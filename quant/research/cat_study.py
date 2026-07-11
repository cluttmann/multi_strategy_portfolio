"""CAT sleeve Phase-A study — LLM-scored overnight catalyst drift.

    python3 -m quant.research.cat_study --extract    # candidates from news
    python3 -m quant.research.cat_study --score      # FinBERT scoring
    python3 -m quant.research.cat_study --backtest   # walk-forward + verdict

Scope discipline (timestamp-honest and auction-executable with data on hand):
only OVERNIGHT-ARRIVAL events (created_at between 16:00 ET and 09:00 ET next
session) on liquid names. Entry = next opening auction (`opg`, the official
open print in daily bars); exits = same close (T0) and next close (T1).
Intraday-arrival events need minute-bar fills and are Phase B.

Pipeline: catalyst regex classes → liquidity join (top-1000 ADV) → FinBERT
sentiment on headlines (local, MPS) → walk-forward LightGBM meta-model on
(catalyst class × sentiment × recency/novelty × gap context) → top-k trades
per day, never-fade blacklist honored. Costs 10bp/side base, 20bp stress.
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd

from quant.config import STAGING_DIR, T_EOD, T_NEWS
from quant.data.bq import client

TOP_ADV = 1000
MIN_PRICE = 5.0
K_SIDE = 5
COST_BPS = 10.0
STRESS_BPS = 20.0
CAND_PATH = os.path.join(STAGING_DIR, "cat_candidates.parquet")
SCORED_PATH = os.path.join(STAGING_DIR, "cat_scored.parquet")

EXTRACT_SQL = f"""
WITH nx AS (
  SELECT s AS symbol, id, created_at, headline, source,
    DATETIME(created_at, 'America/New_York') AS et,
    CASE
      WHEN REGEXP_CONTAINS(LOWER(headline), r'guidance|outlook|raises|lowers|cuts forecast') THEN 'guidance'
      WHEN REGEXP_CONTAINS(LOWER(headline), r'earnings|revenue|eps|beats|misses|quarter') THEN 'earnings'
      WHEN REGEXP_CONTAINS(LOWER(headline), r'fda|approval|phase (1|2|3|i|ii|iii)|clinical') THEN 'fda'
      WHEN REGEXP_CONTAINS(LOWER(headline), r'merger|acquisition|acquire|takeover|buyout|deal') THEN 'mna'
      WHEN REGEXP_CONTAINS(LOWER(headline), r'offering|dilution|priced|placement') THEN 'offering'
      WHEN REGEXP_CONTAINS(LOWER(headline), r'upgrade|downgrade|initiates|price target') THEN 'analyst'
      WHEN REGEXP_CONTAINS(LOWER(headline), r'contract|award|partnership|wins') THEN 'contract'
      ELSE NULL
    END AS cat_class
  FROM `{T_NEWS}`, UNNEST(symbols) AS s
  WHERE ARRAY_LENGTH(symbols) <= 3   -- multi-symbol stories are market wraps
),
events AS (
  SELECT *,
    -- overnight arrival: after 16:00 rolls to next session's open
    DATE(TIMESTAMP_ADD(created_at, INTERVAL 8 HOUR), 'America/New_York') AS eff_date,
    (EXTRACT(HOUR FROM et) >= 16 OR EXTRACT(HOUR FROM et) < 9) AS overnight
  FROM nx
  WHERE cat_class IS NOT NULL
),
px AS (
  SELECT date, symbol,
    open * SAFE_DIVIDE(adjusted_close, close) AS ao,
    adjusted_close AS ac, close AS raw_close,
    close * volume AS dollar_vol
  FROM `{T_EOD}` WHERE close > 0 AND adjusted_close > 0
),
r0 AS (
  SELECT date, symbol, raw_close, ao, ac, dollar_vol,
    SAFE_DIVIDE(ac, LAG(ac) OVER w) - 1 AS ret_1d,
    SAFE_DIVIDE(ao, LAG(ac) OVER w) - 1 AS gap,
    SAFE_DIVIDE(ac, NULLIF(ao,0)) - 1 AS intraday_t0,
    SAFE_DIVIDE(LEAD(ac) OVER w, NULLIF(ao,0)) - 1 AS ret_open_t1close,
    SAFE_DIVIDE(LAG(ac) OVER w, LAG(ac,6) OVER w) - 1 AS ret_5d_prior
  FROM px
  WINDOW w AS (PARTITION BY symbol ORDER BY date)
),
r AS (
  SELECT *,
    STDDEV(ret_1d) OVER w20 AS vol20,
    AVG(dollar_vol) OVER w63 AS adv63
  FROM r0
  WINDOW
    w20 AS (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING),
    w63 AS (PARTITION BY symbol ORDER BY date ROWS BETWEEN 63 PRECEDING AND 1 PRECEDING)
),
u AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY date ORDER BY adv63 DESC) AS liq_rank
  FROM r WHERE raw_close >= {MIN_PRICE} AND adv63 IS NOT NULL
)
SELECT e.symbol, e.eff_date AS date, e.headline, e.source, e.cat_class,
       e.created_at,
       u.gap, u.intraday_t0, u.ret_open_t1close, u.ret_5d_prior, u.vol20,
       u.adv63, u.liq_rank,
       COUNT(*) OVER (PARTITION BY e.symbol, e.eff_date) AS n_stories
FROM events e
JOIN u ON u.symbol = e.symbol AND u.date = e.eff_date
WHERE e.overnight AND u.liq_rank <= {TOP_ADV}
QUALIFY ROW_NUMBER() OVER (PARTITION BY e.symbol, e.eff_date
                           ORDER BY e.created_at DESC) = 1
"""


def extract():
    print("extracting overnight catalyst events ...")
    df = client().query(EXTRACT_SQL).result().to_dataframe(
        create_bqstorage_client=True)
    df["date"] = pd.to_datetime(df["date"])
    os.makedirs(STAGING_DIR, exist_ok=True)
    df.to_parquet(CAND_PATH, index=False)
    print(f"{len(df):,} events, {df['date'].nunique():,} days, classes: "
          f"{df['cat_class'].value_counts().to_dict()}")


def score():
    from transformers import pipeline as hf_pipeline

    df = pd.read_parquet(CAND_PATH)
    print(f"scoring {len(df):,} headlines with FinBERT (MPS) ...")
    nlp = hf_pipeline("sentiment-analysis", model="ProsusAI/finbert",
                      device="mps", batch_size=64, truncation=True)
    heads = df["headline"].fillna("").str.slice(0, 200).tolist()
    out = []
    for i in range(0, len(heads), 2048):
        res = nlp(heads[i:i + 2048])
        out.extend(res)
        if i % 20480 == 0:
            print(f"  {i:,}/{len(heads):,}", flush=True)
    df["fb_label"] = [r["label"] for r in out]
    df["fb_score"] = [r["score"] for r in out]
    df["sentiment"] = np.where(df["fb_label"] == "positive", df["fb_score"],
                       np.where(df["fb_label"] == "negative", -df["fb_score"], 0.0))
    df.to_parquet(SCORED_PATH, index=False)
    print(f"scored. sentiment: {df['fb_label'].value_counts().to_dict()}")


FEATURES = ["sentiment", "gap", "gap_x_sent", "ret_5d_prior", "vol20",
            "n_stories", "cls_guidance", "cls_earnings", "cls_fda", "cls_mna",
            "cls_offering", "cls_analyst", "cls_contract", "log_adv"]


def backtest():
    import lightgbm as lgb

    df = pd.read_parquet(SCORED_PATH)
    df["gap_x_sent"] = df["gap"] * df["sentiment"]
    df["log_adv"] = np.log(df["adv63"])
    for c in ["guidance", "earnings", "fda", "mna", "offering", "analyst",
              "contract"]:
        df[f"cls_{c}"] = (df["cat_class"] == c).astype(int)
    # label: open→T1 close return, artifact-guarded
    df = df[df["ret_open_t1close"].abs() <= 0.5].dropna(
        subset=FEATURES + ["ret_open_t1close"])
    df["y"] = df.groupby("date")["ret_open_t1close"].rank(pct=True)
    print(f"backtest set: {len(df):,} events")

    out = []
    for year in range(2019, 2027):
        train = df[df["date"] < f"{year}-01-01"]
        train = train[train["date"] < train["date"].max() - pd.Timedelta(days=7)]
        test = df[(df["date"] >= f"{year}-01-01") & (df["date"] <= f"{year}-12-31")]
        if len(train) < 3000 or test.empty:
            continue
        m = lgb.train({"objective": "regression", "learning_rate": 0.05,
                       "num_leaves": 15, "max_depth": 4,
                       "min_data_in_leaf": 200, "feature_fraction": 0.8,
                       "bagging_fraction": 0.8, "bagging_freq": 1,
                       "verbosity": -1},
                      lgb.Dataset(train[FEATURES], label=train["y"]),
                      num_boost_round=300)
        te = test.copy()
        te["score"] = m.predict(te[FEATURES])
        ic = te.groupby("date").apply(
            lambda g: g["score"].corr(g["ret_open_t1close"], method="spearman")
        ).mean()
        print(f"{year}: train {len(train):,} → test {len(test):,}, IC {ic:+.3f}")
        out.append(te)
    wf = pd.concat(out, ignore_index=True)

    never_fade = (wf["cls_mna"] > 0) | (wf["cls_fda"] > 0)
    rows = []
    for d, g in wf.groupby("date"):
        g = g.sort_values("score", ascending=False)
        longs, shorts = g.head(K_SIDE), g.tail(K_SIDE)
        shorts = shorts[~never_fade.reindex(shorts.index).fillna(False)]
        n = len(longs) + len(shorts)
        if n == 0:
            continue
        rows.append({"date": d, "n": n, "gross":
                     (longs["ret_open_t1close"].sum()
                      - shorts["ret_open_t1close"].sum()) / n})
    res = pd.DataFrame(rows).set_index("date").sort_index()
    # 2-day hold → cost amortized over the hold, charged once round-trip
    res["net"] = res["gross"] - 2 * COST_BPS / 1e4
    res["net_stress"] = res["gross"] - 2 * STRESS_BPS / 1e4
    span_y = (res.index.max() - res.index.min()).days / 365.25
    freq = len(res) / span_y / 2  # 2-day holds → capital cycles every 2 days
    for col in ["gross", "net", "net_stress"]:
        r = res[col]
        sh = r.mean() / r.std() * np.sqrt(len(res) / span_y) if r.std() > 0 else 0
        print(f"{col:12s} Sharpe={sh:5.2f}  ann@1x={r.mean() * freq * 2:+7.1%}")
    yearly = res.groupby(res.index.year)["net"].apply(lambda r: (1 + r).prod() - 1)
    print("per-year net: " + "  ".join(f"{y}:{v:+.0%}" for y, v in yearly.items()))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--extract", action="store_true")
    p.add_argument("--score", action="store_true")
    p.add_argument("--backtest", action="store_true")
    args = p.parse_args()
    if args.extract:
        extract()
    if args.score:
        score()
    if args.backtest:
        backtest()
    if not (args.extract or args.score or args.backtest):
        p.print_help()
        sys.exit(1)
