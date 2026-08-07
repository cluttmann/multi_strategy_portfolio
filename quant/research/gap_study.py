"""GAP sleeve Phase-A study — overnight gap drift-vs-fade (DESIGN.md Sleeve 3).

    python3 -m quant.research.gap_study --run

Phase A = signal-existence test on daily bars + news conditioning:
  universe   point-in-time top-600 by 63d ADV, price ≥ $5 (from eod_bars)
  candidates |overnight gap| between 1.5x and 6x the name's own 20d vol
  label      intraday return (open→close) of the gap day
  model      walk-forward LightGBM (yearly folds, purged), predicting the
             intraday return; trade top-k/bottom-k candidates per day
  news       Benzinga corpus: overnight item count (16:00 prev → 09:30 ET),
             catalyst keyword classes (earnings/guidance/FDA/M&A/offering/
             analyst), never-fade blacklist honored (M&A/FDA never faded)
  costs      10bp/side base (opening spreads are 2-4x midday), 20bp stress

Timing honesty: entry is approximated at the official opening print (the
daily 'open'); the real sleeve enters at 09:35 after the opening range. The
approximation OVERSTATES capture (we get the first 5 minutes for free), so
Phase A can only kill or provisionally pass the sleeve: a pass mandates
Phase B (minute-bar re-study with true 09:35 entries) before deployment.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.config import T_EOD, T_NEWS
from quant.data.bq import client

TOP_ADV = 600
MIN_PRICE = 5.0
GAP_LO, GAP_HI = 1.5, 6.0
K_SIDE = 5
COST_BPS = 10.0
STRESS_BPS = 20.0

PANEL_SQL = f"""
WITH px AS (
  SELECT date, symbol,
         open * SAFE_DIVIDE(adjusted_close, close) AS ao,
         adjusted_close AS ac,
         close AS raw_close,
         close * volume AS dollar_vol
  FROM `{T_EOD}`
  WHERE close > 0 AND adjusted_close > 0
),
r AS (
  SELECT date, symbol, raw_close,
    SAFE_DIVIDE(ao, LAG(ac) OVER w) - 1 AS gap,
    SAFE_DIVIDE(ac, NULLIF(ao, 0)) - 1 AS intraday,
    SAFE_DIVIDE(ac, LAG(ac) OVER w) - 1 AS ret_1d,
    LAG(SAFE_DIVIDE(ac, NULLIF(ao,0)) - 1) OVER w AS prev_intraday,
    SAFE_DIVIDE(LAG(ac) OVER w, LAG(ac, 6) OVER w) - 1 AS ret_5d_prior,
    AVG(dollar_vol) OVER w63 AS adv63
  FROM px
  WINDOW w AS (PARTITION BY symbol ORDER BY date),
         w63 AS (PARTITION BY symbol ORDER BY date
                 ROWS BETWEEN 63 PRECEDING AND 1 PRECEDING)
),
v AS (
  SELECT *,
    STDDEV(ret_1d) OVER (PARTITION BY symbol ORDER BY date
                         ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS vol20
  FROM r
),
u AS (
  SELECT *,
    ROW_NUMBER() OVER (PARTITION BY date ORDER BY adv63 DESC) AS liq_rank
  FROM v
  WHERE raw_close >= {MIN_PRICE} AND adv63 IS NOT NULL AND vol20 > 0
)
SELECT date, symbol, gap, intraday, prev_intraday, ret_5d_prior,
       vol20, adv63, liq_rank,
       SAFE_DIVIDE(ABS(gap), vol20) AS gap_x
FROM u
WHERE liq_rank <= {TOP_ADV}
  AND SAFE_DIVIDE(ABS(gap), vol20) BETWEEN {GAP_LO} AND {GAP_HI}
  AND ABS(gap) <= 0.5
  AND ABS(intraday) <= 0.5
"""

NEWS_SQL = f"""
SELECT s AS symbol,
       DATE(TIMESTAMP_ADD(created_at, INTERVAL 8 HOUR), 'America/New_York')
         AS eff_date,   -- items after 16:00 ET roll to the NEXT day's open
       COUNT(*) AS n_items,
       COUNTIF(REGEXP_CONTAINS(LOWER(headline),
         r'earnings|revenue|eps|quarter|guidance|outlook')) AS n_earnings,
       COUNTIF(REGEXP_CONTAINS(LOWER(headline),
         r'fda|phase (1|2|3|i|ii|iii)|trial|approval')) AS n_fda,
       COUNTIF(REGEXP_CONTAINS(LOWER(headline),
         r'merger|acquisition|acquire|takeover|buyout')) AS n_mna,
       COUNTIF(REGEXP_CONTAINS(LOWER(headline),
         r'offering|dilut|priced|private placement')) AS n_offering,
       COUNTIF(REGEXP_CONTAINS(LOWER(headline),
         r'upgrade|downgrade|initiat|price target|rating')) AS n_analyst
FROM `{T_NEWS}`, UNNEST(symbols) AS s
GROUP BY symbol, eff_date
"""


def load_panel() -> pd.DataFrame:
    print("building gap candidate panel in BigQuery ...")
    df = client().query(PANEL_SQL).result().to_dataframe(
        create_bqstorage_client=True)
    df["date"] = pd.to_datetime(df["date"])
    print(f"candidates: {len(df):,} over {df['date'].nunique():,} days "
          f"({len(df)/df['date'].nunique():.1f}/day)")
    news = client().query(NEWS_SQL).result().to_dataframe(
        create_bqstorage_client=True)
    news["eff_date"] = pd.to_datetime(news["eff_date"])
    df = df.merge(news, left_on=["symbol", "date"],
                  right_on=["symbol", "eff_date"], how="left")
    for c in ["n_items", "n_earnings", "n_fda", "n_mna", "n_offering",
              "n_analyst"]:
        df[c] = df[c].fillna(0).astype("int32")
    df["has_news"] = (df["n_items"] > 0).astype(int)
    print(f"with news joined: {df['has_news'].mean():.0%} of candidates "
          f"have overnight news")
    return df.drop(columns=["eff_date"])


FEATURES = ["gap", "gap_x", "prev_intraday", "ret_5d_prior", "vol20",
            "n_items", "n_earnings", "n_fda", "n_mna", "n_offering",
            "n_analyst", "has_news"]


def walk_forward(df: pd.DataFrame) -> pd.DataFrame:
    import lightgbm as lgb

    df = df.dropna(subset=FEATURES + ["intraday"]).copy()
    df["y"] = df.groupby("date")["intraday"].rank(pct=True)
    out = []
    for year in range(2019, 2027):
        train = df[df["date"] < f"{year - 0}-01-01"]
        train = train[train["date"] < train["date"].max() - pd.Timedelta(days=7)]
        test = df[(df["date"] >= f"{year}-01-01") & (df["date"] <= f"{year}-12-31")]
        if len(train) < 3000 or test.empty:
            continue
        model = lgb.train(
            {"objective": "regression", "learning_rate": 0.05,
             "num_leaves": 15, "max_depth": 4, "min_data_in_leaf": 200,
             "feature_fraction": 0.8, "bagging_fraction": 0.8,
             "bagging_freq": 1, "verbosity": -1},
            lgb.Dataset(train[FEATURES], label=train["y"]),
            num_boost_round=300,
        )
        te = test.copy()
        te["score"] = model.predict(te[FEATURES])
        ic = te.groupby("date").apply(
            lambda g: g["score"].corr(g["intraday"], method="spearman")).mean()
        print(f"{year}: train {len(train):,} → test {len(test):,}, "
              f"daily IC {ic:+.3f}")
        out.append(te)
    return pd.concat(out, ignore_index=True)


def backtest(wf: pd.DataFrame):
    # never-fade blacklist: M&A / FDA names may only be traded long
    fade_blocked = (wf["n_mna"] > 0) | (wf["n_fda"] > 0)
    rows = []
    for d, g in wf.groupby("date"):
        g = g.sort_values("score", ascending=False)
        longs = g.head(K_SIDE)
        shorts = g.tail(K_SIDE)
        shorts = shorts[~fade_blocked.reindex(shorts.index).fillna(False)]
        n = len(longs) + len(shorts)
        if n == 0:
            continue
        gross = (longs["intraday"].sum() - shorts["intraday"].sum()) / n
        rows.append({"date": d, "gross": gross, "n": n})
    res = pd.DataFrame(rows).set_index("date").sort_index()
    res["net"] = res["gross"] - 2 * COST_BPS / 1e4
    res["net_stress"] = res["gross"] - 2 * STRESS_BPS / 1e4

    def stats(col):
        r = res[col]
        # trades happen ~every day; annualize on trade-day count per year
        span_y = (res.index.max() - res.index.min()).days / 365.25
        freq = len(r) / span_y
        sh = r.mean() / r.std() * np.sqrt(freq) if r.std() > 0 else 0
        ann = r.mean() * freq
        return sh, ann

    print(f"\ntrade days: {len(res):,} "
          f"({len(res)/((res.index.max()-res.index.min()).days/365.25):.0f}/yr), "
          f"avg names/day {res['n'].mean():.1f}")
    for col, label in [("gross", "gross"), ("net", f"net@{COST_BPS:.0f}bp"),
                       ("net_stress", f"net@{STRESS_BPS:.0f}bp")]:
        sh, ann = stats(col)
        print(f"{label:14s} Sharpe={sh:5.2f}  ann.return(1x intraday)={ann:+7.1%}")
    yearly = res.groupby(res.index.year)["net"].apply(lambda r: (1 + r).prod() - 1)
    print("per-year net: " + "  ".join(f"{y}:{v:+.0%}" for y, v in yearly.items()))
    print("\nlong-only vs short-only attribution (gross, bps/trade-day):")
    print(f"  note: shorts exclude never-fade names")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    panel = load_panel()
    wf = walk_forward(panel)
    backtest(wf)
