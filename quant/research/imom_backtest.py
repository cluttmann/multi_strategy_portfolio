"""IMOM sleeve research backtest — ETF intraday momentum (DESIGN.md Sleeve 1).

    python3 -m quant.research.imom_backtest --run

Effect: Gao–Han–Li–Zhou (2018 JFE) — the first half-hour return on liquid
ETFs predicts the last half-hour return. Rule core: at 15:30 ET, if the
first-half-hour return (prev close → 10:00) exceeds a threshold, trade its
sign into the close; exit at the closing auction. A small LightGBM
meta-filter estimates P(trade clears costs) on top; per DESIGN.md G7 the
meta-filter must beat the rule by ≥ 0.1 net Sharpe or the rule deploys alone.

Timing honesty: entry decisions use only data available at 15:30 ET
(first-half-hour tape, midday drift, previous days' vol/volume, VIX close of
the PRIOR day). Entry price = 15:30 minute-bar close (marketable-limit
approx); exit = official daily close (auction print, `cls` order).

Cost model: per-side bps by liquidity tier, doubled in the stress column.
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd

from quant.config import FRED_KEY, STAGING_DIR, T_EOD, T_MINUTE
from quant.data.bq import client

RULE_THRESHOLD_BPS = 25.0
META_GATE = 0.55
TRAIN_MIN_YEARS = 3          # first test year = 2016 + 3 = 2019
COST_TIER_1 = {"QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV", "XLI", "XLP",
               "XLU", "XLY", "XLB", "EEM", "EFA", "HYG", "LQD", "IEF", "GLD",
               "SLV", "GDX", "SMH", "KRE", "XOP", "FXI", "EWZ"}
COST_BPS_TIER1 = 1.0         # per side
COST_BPS_TIER2 = 2.0         # XBI, XLRE, XLC and anything else

MARKS_SQL = f"""
WITH b AS (
  SELECT symbol, ts, open, close, volume,
         DATETIME(ts, 'America/New_York') AS et
  FROM `{T_MINUTE}`
),
rth AS (
  SELECT symbol, DATE(et) AS d, TIME(et) AS t, ts, open, close, volume
  FROM b
  WHERE TIME(et) >= '09:30:00' AND TIME(et) < '16:00:00'
)
SELECT
  symbol, d,
  ARRAY_AGG(IF(t < '10:00:00', open, NULL) IGNORE NULLS ORDER BY ts ASC  LIMIT 1)[SAFE_OFFSET(0)] AS open_0930,
  ARRAY_AGG(IF(t < '10:00:00', close, NULL) IGNORE NULLS ORDER BY ts DESC LIMIT 1)[SAFE_OFFSET(0)] AS px_1000,
  ARRAY_AGG(IF(t < '15:30:00', close, NULL) IGNORE NULLS ORDER BY ts DESC LIMIT 1)[SAFE_OFFSET(0)] AS px_1530,
  ARRAY_AGG(close ORDER BY ts DESC LIMIT 1)[SAFE_OFFSET(0)] AS px_last,
  SUM(IF(t < '10:00:00', volume, 0)) AS vol_first30,
  SUM(volume) AS vol_day,
  COUNT(*) AS n_bars
FROM rth
GROUP BY symbol, d
HAVING n_bars >= 300 AND open_0930 IS NOT NULL AND px_1530 IS NOT NULL
"""


def load_marks() -> pd.DataFrame:
    print("Aggregating session marks in BigQuery ...")
    df = client().query(MARKS_SQL).result().to_dataframe(create_bqstorage_client=True)
    df["d"] = pd.to_datetime(df["d"])
    # Official close from daily bars where available (auction print).
    daily = client().query(
        f"SELECT date, symbol, close FROM `{T_EOD}` WHERE symbol IN "
        f"({', '.join(repr(s) for s in df['symbol'].unique())})"
    ).result().to_dataframe()
    daily["d"] = pd.to_datetime(daily["date"])
    df = df.merge(daily[["d", "symbol", "close"]], on=["d", "symbol"], how="left")
    df["px_close"] = df["close"].fillna(df["px_last"])
    return df.drop(columns=["close", "date"], errors="ignore")


def load_vix() -> pd.Series:
    import requests
    r = requests.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={"series_id": "VIXCLS", "api_key": FRED_KEY, "file_type": "json",
                "observation_start": "2015-01-01"},
        timeout=30,
    ).json()
    s = pd.Series(
        {pd.Timestamp(o["date"]): float(o["value"])
         for o in r["observations"] if o["value"] != "."},
        name="vix",
    )
    return s


def make_dataset(marks: pd.DataFrame) -> pd.DataFrame:
    marks = marks.sort_values(["symbol", "d"]).reset_index(drop=True)
    g = marks.groupby("symbol", group_keys=False)
    marks["prev_close"] = g["px_close"].shift(1)
    marks["r_fh"] = marks["px_1000"] / marks["prev_close"] - 1        # incl. overnight
    marks["r_fh_x"] = marks["px_1000"] / marks["open_0930"] - 1       # excl. gap
    marks["gap"] = marks["open_0930"] / marks["prev_close"] - 1
    marks["r_midday"] = marks["px_1530"] / marks["px_1000"] - 1
    marks["r_last30"] = marks["px_close"] / marks["px_1530"] - 1      # target
    marks["vol_ratio_f30"] = marks["vol_first30"] / g["vol_first30"].transform(
        lambda s: s.rolling(20, min_periods=10).mean().shift(1))
    marks["rv_20d"] = g["r_last30"].transform(
        lambda s: s.rolling(20, min_periods=10).std().shift(1)) * np.sqrt(252)
    daily_ret = marks["px_close"] / marks["prev_close"] - 1
    marks["dvol_20d"] = daily_ret.groupby(marks["symbol"]).transform(
        lambda s: s.rolling(20, min_periods=10).std().shift(1)) * np.sqrt(252)

    vix = load_vix()
    marks["vix_prev"] = marks["d"].map(vix.shift(1))  # prior-day close only
    marks["dow"] = marks["d"].dt.dayofweek
    marks = marks.dropna(subset=["r_fh", "r_midday", "r_last30", "prev_close"])
    return marks


def add_trade_frame(df: pd.DataFrame, threshold_bps=RULE_THRESHOLD_BPS) -> pd.DataFrame:
    """Rule candidates + per-trade gross/net returns."""
    t = df[np.abs(df["r_fh"]) * 1e4 >= threshold_bps].copy()
    t["side"] = np.sign(t["r_fh"])
    t["gross"] = t["side"] * t["r_last30"]
    cost = np.where(t["symbol"].isin(COST_TIER_1), COST_BPS_TIER1, COST_BPS_TIER2)
    # entry side pays the spread; MOC exit assumed at the print (half a tier
    # charged anyway for auction slippage realism)
    t["cost"] = (cost + 0.5 * cost) / 1e4
    t["net"] = t["gross"] - t["cost"]
    t["net_stress"] = t["gross"] - 2 * t["cost"]
    return t


def daily_pnl(trades: pd.DataFrame, col: str) -> pd.Series:
    """Equal-weight capital across same-day signals; days without trades = 0."""
    return trades.groupby("d")[col].mean()


def sharpe(daily: pd.Series, days=252) -> float:
    if len(daily) < 20 or daily.std() == 0:
        return 0.0
    # Sharpe on trade-day returns scaled by trade-day frequency per year
    freq = days * len(daily) / max((daily.index.max() - daily.index.min()).days, 1) / (days / 365)
    return float(daily.mean() / daily.std() * np.sqrt(min(freq, days)))


FEATURES = ["r_fh", "r_fh_x", "gap", "r_midday", "vol_ratio_f30", "rv_20d",
            "dvol_20d", "vix_prev", "dow"]


def walk_forward_meta(trades: pd.DataFrame):
    """Yearly walk-forward: P(net>0), gate at META_GATE."""
    import lightgbm as lgb

    trades = trades.dropna(subset=FEATURES).copy()
    trades["y"] = (trades["net"] > 0).astype(int)
    years = sorted(trades["d"].dt.year.unique())
    test_years = [y for y in years if y >= years[0] + TRAIN_MIN_YEARS]
    out = []
    for y in test_years:
        train = trades[trades["d"] < f"{y}-01-01"]
        # 5-day purge before the test year
        train = train[train["d"] < train["d"].max() - pd.Timedelta(days=7)]
        test = trades[(trades["d"] >= f"{y}-01-01") & (trades["d"] <= f"{y}-12-31")]
        if len(train) < 500 or test.empty:
            continue
        model = lgb.train(
            {"objective": "binary", "learning_rate": 0.05, "num_leaves": 7,
             "max_depth": 3, "min_data_in_leaf": 100, "feature_fraction": 0.8,
             "bagging_fraction": 0.8, "bagging_freq": 1, "verbosity": -1},
            lgb.Dataset(train[FEATURES], label=train["y"]),
            num_boost_round=200,
        )
        te = test.copy()
        te["p"] = model.predict(te[FEATURES])
        out.append(te)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def report(marks: pd.DataFrame):
    df = make_dataset(marks)
    trades = add_trade_frame(df)
    print(f"\ndataset: {len(df):,} symbol-days, {df['d'].min():%Y-%m-%d} → "
          f"{df['d'].max():%Y-%m-%d}")
    print(f"rule candidates (|r_fh| ≥ {RULE_THRESHOLD_BPS}bp): {len(trades):,} "
          f"trades, {trades['d'].nunique():,} days "
          f"({len(trades)/max(trades['d'].nunique(),1):.1f} trades/day)")

    def block(label, t):
        if t.empty:
            print(f"{label:28s} —")
            return
        d_net = daily_pnl(t, "net")
        d_str = daily_pnl(t, "net_stress")
        hit = (t["net"] > 0).mean()
        print(f"{label:28s} n={len(t):6,}  hit={hit:5.1%}  "
              f"avg_gross={t['gross'].mean()*1e4:+6.2f}bp  "
              f"avg_net={t['net'].mean()*1e4:+6.2f}bp  "
              f"Sharpe(net)={sharpe(d_net):5.2f}  "
              f"Sharpe(2x cost)={sharpe(d_str):5.2f}")

    print("\n=== RULE BASELINE (full sample) ===")
    block("all trades", trades)
    for y0, y1 in [(2016, 2018), (2019, 2021), (2022, 2025), (2026, 2026)]:
        sub = trades[(trades["d"].dt.year >= y0) & (trades["d"].dt.year <= y1)]
        block(f"  {y0}-{y1}", sub)
    print("\nper-year net bps/trade:")
    yr = trades.groupby(trades["d"].dt.year)["net"].agg(["mean", "count"])
    for y, row in yr.iterrows():
        print(f"  {y}: {row['mean']*1e4:+6.2f}bp × {int(row['count']):,}")

    print("\n=== META-FILTER (walk-forward, OOS from "
          f"{2016 + TRAIN_MIN_YEARS}) ===")
    wf = walk_forward_meta(trades)
    if not wf.empty:
        block("all OOS candidates", wf)
        gated = wf[wf["p"] >= META_GATE]
        block(f"gated (p ≥ {META_GATE})", gated)
        # G7: compare on the same OOS window
        rule_sh = sharpe(daily_pnl(wf, "net"))
        gate_sh = sharpe(daily_pnl(gated, "net")) if not gated.empty else 0.0
        print(f"\nG7 baseline dominance: rule={rule_sh:.2f} vs "
              f"meta-gated={gate_sh:.2f} → "
              f"{'META' if gate_sh - rule_sh >= 0.1 else 'RULE'} deploys")
        # G6 score-shuffle null: break the score↔outcome link and re-gate.
        # The gated per-trade mean must beat ~all null draws or the model's
        # selection is noise.
        rng = np.random.default_rng(7)
        nulls = []
        for _ in range(500):
            p_shuf = rng.permutation(wf["p"].values)
            sel = wf.loc[p_shuf >= META_GATE, "net"]
            if len(sel):
                nulls.append(sel.mean())
        nulls = np.array(nulls)
        actual = gated["net"].mean() if not gated.empty else float("nan")
        pct = (nulls < actual).mean() * 100
        print(f"G6 score-shuffle null: actual {actual*1e4:+.2f}bp vs null "
              f"{nulls.mean()*1e4:+.2f}±{nulls.std()*1e4:.2f}bp "
              f"→ percentile {pct:.1f}% (need >95%)")
    os.makedirs(STAGING_DIR, exist_ok=True)
    trades.to_parquet(os.path.join(STAGING_DIR, "imom_trades.parquet"))
    if not wf.empty:
        wf.to_parquet(os.path.join(STAGING_DIR, "imom_wf.parquet"))
    print(f"\nSaved trade frames to {STAGING_DIR}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    report(load_marks())
