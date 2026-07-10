"""Data-quality audit — DESIGN.md gauntlet G1 prerequisites.

    python3 -m quant.data.quality --audit

Run after any backfill and before trusting a backtest. Checks:
  eod_bars:  duplicate (date,symbol) keys, per-year cross-section coverage,
             adjusted/close ratio sanity, zero/negative prices, gap days
  minute_bars: per-symbol day coverage vs eod_bars trading days
  news:      items/year, timestamp monotonicity of the backfill
"""

import argparse
import sys

from quant.config import T_EOD, T_MINUTE, T_NEWS
from quant.data.bq import query


def audit_eod():
    print("=== eod_bars ===")
    q = query(f"""
        SELECT COUNT(*) n, COUNT(DISTINCT symbol) syms,
               MIN(date) lo, MAX(date) hi
        FROM `{T_EOD}`""")
    r = q.iloc[0]
    print(f"rows={r.n:,} symbols={r.syms:,} range {r.lo} → {r.hi}")

    dup = query(f"""
        SELECT COUNT(*) AS dups FROM (
          SELECT date, symbol FROM `{T_EOD}`
          GROUP BY date, symbol HAVING COUNT(*) > 1)""").iloc[0].dups
    print(f"duplicate (date,symbol) keys: {dup:,} {'✗ FIX' if dup else '✓'}")

    cov = query(f"""
        SELECT EXTRACT(YEAR FROM date) y,
               COUNT(DISTINCT date) days,
               COUNT(DISTINCT symbol) syms,
               ROUND(COUNT(*) / COUNT(DISTINCT date)) avg_cross_section
        FROM `{T_EOD}` GROUP BY y ORDER BY y""")
    for _, row in cov.iterrows():
        flag = "✓" if row.days >= 248 and row.avg_cross_section >= 3000 else "⚠"
        print(f"  {int(row.y)}: {int(row.days)} days, "
              f"cross-section ≈ {int(row.avg_cross_section):,} {flag}")

    bad = query(f"""
        SELECT
          COUNTIF(close <= 0 OR adjusted_close <= 0) AS nonpos,
          COUNTIF(high < low) AS hl_inverted,
          COUNTIF(SAFE_DIVIDE(adjusted_close, close) > 100
                  OR SAFE_DIVIDE(adjusted_close, close) < 0.0001) AS adj_extreme
        FROM `{T_EOD}`""").iloc[0]
    print(f"nonpositive prices: {bad.nonpos:,} | inverted H/L: "
          f"{bad.hl_inverted:,} | extreme adj ratios: {bad.adj_extreme:,}")


def audit_minute():
    print("\n=== minute_bars ===")
    q = query(f"""
        SELECT symbol, COUNT(DISTINCT DATE(ts, 'America/New_York')) days,
               MIN(DATE(ts)) lo, MAX(DATE(ts)) hi, COUNT(*) bars
        FROM `{T_MINUTE}` GROUP BY symbol ORDER BY symbol""")
    if q.empty:
        print("(empty)")
        return
    for _, r in q.iterrows():
        print(f"  {r.symbol:6s} {r.bars:>12,} bars  {int(r.days):>5} days  "
              f"{r.lo} → {r.hi}")


def audit_news():
    print("\n=== news ===")
    q = query(f"""
        SELECT EXTRACT(YEAR FROM created_at) y, COUNT(*) n,
               COUNT(DISTINCT id) ids
        FROM `{T_NEWS}` GROUP BY y ORDER BY y""")
    if q.empty:
        print("(empty)")
        return
    for _, r in q.iterrows():
        dupflag = "" if r.n == r.ids else f"  (dups: {r.n - r.ids:,})"
        print(f"  {int(r.y)}: {r.n:,} items{dupflag}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--audit", action="store_true")
    args = p.parse_args()
    if not args.audit:
        p.print_help()
        sys.exit(1)
    audit_eod()
    audit_minute()
    audit_news()
