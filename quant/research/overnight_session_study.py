"""Overnight-session (Blue Ocean ATS) study — the newest venue on the account.

    python3 -m quant.research.overnight_session_study --run

Session: 20:00–04:00 ET, live since ~Jan/Feb 2026 (~6 months of history).
This is an EXISTENCE study — 6 months cannot clear the full gauntlet; the
questions are structural:
  1. Where is the liquidity (symbols, hours)?
  2. Does the overnight-session move continue or revert at the RTH open?
  3. Do Benzinga news arrivals during the session produce tradable in-session
     drift (react at 21:00 instead of queuing for the 09:30 auction)?
Costs: the session is thin; minute-bar high-low range is used as a spread
proxy and reported alongside every signal.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.config import BQ_DATASET, GCP_PROJECT, T_EOD, T_NEWS
from quant.data.bq import client

T_BOATS = f"{GCP_PROJECT}.{BQ_DATASET}.boats_bars"

SESSION_SQL = f"""
WITH b AS (
  SELECT symbol, ts, open, high, low, close, volume,
         DATETIME(ts, 'America/New_York') AS et
  FROM `{T_BOATS}`
),
-- assign each bar to its session date: bars 20:00-23:59 belong to the NEXT
-- calendar day's open (the session runs into that morning)
s AS (
  SELECT *,
    IF(EXTRACT(HOUR FROM et) >= 20,
       DATE_ADD(DATE(et), INTERVAL 1 DAY), DATE(et)) AS session_date,
    EXTRACT(HOUR FROM et) AS hh
  FROM b
  WHERE EXTRACT(HOUR FROM et) >= 20 OR EXTRACT(HOUR FROM et) < 4
)
SELECT
  symbol, session_date,
  ARRAY_AGG(open ORDER BY ts ASC LIMIT 1)[SAFE_OFFSET(0)] AS sess_open,
  ARRAY_AGG(close ORDER BY ts DESC LIMIT 1)[SAFE_OFFSET(0)] AS sess_close,
  SUM(volume) AS sess_vol,
  COUNT(*) AS n_bars,
  AVG(SAFE_DIVIDE(high - low, NULLIF(close, 0))) * 1e4 AS avg_range_bp,
  SUM(IF(hh >= 20, volume, 0)) AS vol_evening,
  SUM(IF(hh < 4, volume, 0)) AS vol_early
FROM s
GROUP BY symbol, session_date
HAVING n_bars >= 10
"""


def load():
    print("aggregating overnight sessions in BigQuery ...")
    sess = client().query(SESSION_SQL).result().to_dataframe(
        create_bqstorage_client=True)
    sess["session_date"] = pd.to_datetime(sess["session_date"])
    syms = [s for s in sess["symbol"].unique()]
    daily = client().query(
        f"SELECT date, symbol, open, close FROM `{T_EOD}` WHERE symbol IN "
        f"({', '.join(repr(s) for s in syms)}) AND date >= '2025-12-15'"
    ).result().to_dataframe()
    daily["date"] = pd.to_datetime(daily["date"])
    df = sess.merge(daily.rename(columns={"date": "session_date"}),
                    on=["session_date", "symbol"], how="inner")
    # prior RTH close
    daily_sorted = daily.sort_values(["symbol", "session_date"] if "session_date" in daily else ["symbol", "date"])
    prev = daily.sort_values(["symbol", "date"]).copy() if "date" in daily else None
    daily2 = daily.sort_values(["symbol", "date"]) if "date" in daily.columns else None
    return df, daily


def run():
    sess = client().query(SESSION_SQL).result().to_dataframe(
        create_bqstorage_client=True)
    sess["session_date"] = pd.to_datetime(sess["session_date"])
    print(f"{len(sess):,} symbol-sessions, {sess['symbol'].nunique()} symbols, "
          f"{sess['session_date'].min():%Y-%m-%d} → {sess['session_date'].max():%Y-%m-%d}")

    # ── 1. liquidity structure ────────────────────────────────────────────
    top = (sess.groupby("symbol")
           .agg(sessions=("session_date", "nunique"), vol=("sess_vol", "sum"),
                range_bp=("avg_range_bp", "median"))
           .sort_values("vol", ascending=False).head(15))
    print("\ntop-15 by overnight volume (median 1-min range bp = spread proxy):")
    for s, r in top.iterrows():
        print(f"  {s:6s} sessions={int(r.sessions):3d} vol={r.vol:>12,.0f} "
              f"range≈{r.range_bp:5.0f}bp")

    # ── 2. join RTH prices ───────────────────────────────────────────────
    syms = list(top.index[:40]) if len(top) >= 15 else list(sess["symbol"].unique())
    syms = list(sess["symbol"].unique())
    daily = client().query(
        f"SELECT date, symbol, open AS rth_open, close AS rth_close "
        f"FROM `{T_EOD}` WHERE symbol IN ({', '.join(repr(s) for s in syms)}) "
        f"AND date >= '2025-12-15'").result().to_dataframe()
    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily.sort_values(["symbol", "date"])
    daily["prev_close"] = daily.groupby("symbol")["rth_close"].shift(1)
    df = sess.merge(daily.rename(columns={"date": "session_date"}),
                    on=["session_date", "symbol"], how="inner").dropna(
        subset=["prev_close", "sess_open", "sess_close", "rth_open"])
    df = df[df["sess_vol"] > 5000]  # minimum session activity
    print(f"\njoined sessions with activity: {len(df):,}")

    df["r_sess"] = df["sess_close"] / df["sess_open"] - 1          # in-session move
    df["r_close_to_sessopen"] = df["sess_open"] / df["prev_close"] - 1
    df["r_sessclose_to_open"] = df["rth_open"] / df["sess_close"] - 1  # residual gap
    df["r_open_to_close"] = df["rth_close"] / df["rth_open"] - 1   # next RTH day

    # ── 3. continuation vs reversal at the open ──────────────────────────
    print("\n═══ overnight-session move → what happens next ═══")
    df["bucket"] = pd.qcut(df["r_sess"], 5, labels=["Q1(most neg)", "Q2", "Q3", "Q4", "Q5(most pos)"])
    g = df.groupby("bucket", observed=True)[["r_sess", "r_sessclose_to_open", "r_open_to_close"]].mean() * 1e4
    g.columns = ["sess move bp", "sess-close→RTH-open bp", "RTH open→close bp"]
    print(g.round(1).to_string())
    ic1 = df["r_sess"].corr(df["r_sessclose_to_open"], method="spearman")
    ic2 = df["r_sess"].corr(df["r_open_to_close"], method="spearman")
    print(f"\nrank-corr(sess move, sess-close→open): {ic1:+.3f}")
    print(f"rank-corr(sess move, next RTH open→close): {ic2:+.3f}")

    # ── 4. news reaction in-session ──────────────────────────────────────
    news = client().query(f"""
        SELECT s AS symbol,
          IF(EXTRACT(HOUR FROM DATETIME(created_at, 'America/New_York')) >= 20,
             DATE_ADD(DATE(DATETIME(created_at, 'America/New_York')), INTERVAL 1 DAY),
             DATE(DATETIME(created_at, 'America/New_York'))) AS session_date,
          COUNT(*) AS n_news
        FROM `{T_NEWS}`, UNNEST(symbols) AS s
        WHERE created_at >= '2025-12-15'
          AND (EXTRACT(HOUR FROM DATETIME(created_at, 'America/New_York')) >= 20
               OR EXTRACT(HOUR FROM DATETIME(created_at, 'America/New_York')) < 4)
        GROUP BY symbol, session_date""").result().to_dataframe()
    news["session_date"] = pd.to_datetime(news["session_date"])
    df = df.merge(news, on=["symbol", "session_date"], how="left")
    df["has_news"] = df["n_news"].fillna(0) > 0
    print("\n═══ in-session news vs no-news ═══")
    for flag, sub in df.groupby("has_news"):
        print(f"  news={flag!s:5s} n={len(sub):5,}  |sess move|={sub['r_sess'].abs().mean()*1e4:6.1f}bp  "
              f"sess→open drift={sub['r_sessclose_to_open'].mean()*1e4:+6.1f}bp  "
              f"next RTH={sub['r_open_to_close'].mean()*1e4:+6.1f}bp")
    # conditional: big in-session move WITH news — continuation into open?
    big = df[df["has_news"] & (df["r_sess"].abs() > 0.01)].copy()
    if len(big) > 30:
        big["side"] = np.sign(big["r_sess"])
        cont = (big["side"] * big["r_sessclose_to_open"]).mean() * 1e4
        cont2 = (big["side"] * big["r_open_to_close"]).mean() * 1e4
        print(f"\nnews + |sess move|>1%: n={len(big):,}, signed drift to open "
              f"{cont:+.1f}bp, signed next-RTH {cont2:+.1f}bp "
              f"(vs median session spread proxy "
              f"{df['avg_range_bp'].median():.0f}bp)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    run()
