"""PEAD — post-earnings announcement drift with real earnings dates + SUE.

    python3 -m quant.research.pead_study --run

The strategy the fundamentals feed most directly enables: earnings events
with actual report dates, timing flags, and surprise magnitudes. Entry at
the first tradable open after the announcement (AfterMarket → next open;
BeforeMarket/undefined → same-day open is NOT safely tradable on the
announcement print, so we also enter next open — conservative one-day lag
for those), hold 5/20/60 days. Universe: liquid names only (price ≥ $5,
ADV ≥ $5M at event). Long top-SUE-quintile / short bottom-quintile.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.config import BQ_DATASET, GCP_PROJECT, T_EOD
from quant.data.bq import client

T_EARN = f"{GCP_PROJECT}.{BQ_DATASET}.earnings_history"

SQL = f"""
WITH ev AS (
  SELECT symbol, report_date, before_after_market, surprise_pct,
    -- first tradable open: AfterMarket → next day; else conservative next day
    DATE_ADD(report_date, INTERVAL 1 DAY) AS entry_date
  FROM `{T_EARN}`
  WHERE surprise_pct IS NOT NULL AND report_date >= '2003-01-01'
),
px AS (
  SELECT date, symbol,
    open * SAFE_DIVIDE(adjusted_close, close) AS ao,
    close AS raw_close,
    AVG(close * volume) OVER (PARTITION BY symbol ORDER BY date
      ROWS BETWEEN 63 PRECEDING AND 1 PRECEDING) AS adv63
  FROM `{T_EOD}` WHERE close > 0 AND adjusted_close > 0
),
oo AS (
  SELECT date, symbol, ao, raw_close, adv63,
    LEAD(ao, 5)  OVER w AS ao5,
    LEAD(ao, 20) OVER w AS ao20,
    LEAD(ao, 60) OVER w AS ao60
  FROM px WINDOW w AS (PARTITION BY symbol ORDER BY date)
),
-- map entry_date to the first trading day >= entry_date
joined AS (
  SELECT e.symbol, e.report_date, e.surprise_pct, e.before_after_market,
    o.date AS entry_traded, o.ao AS entry_px, o.raw_close, o.adv63,
    SAFE_DIVIDE(o.ao5, o.ao) - 1 AS fwd5,
    SAFE_DIVIDE(o.ao20, o.ao) - 1 AS fwd20,
    SAFE_DIVIDE(o.ao60, o.ao) - 1 AS fwd60
  FROM ev e
  JOIN oo o ON o.symbol = e.symbol
    AND o.date >= e.entry_date
    AND o.date <= DATE_ADD(e.entry_date, INTERVAL 5 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY e.symbol, e.report_date
                             ORDER BY o.date ASC) = 1
)
SELECT * FROM joined
WHERE raw_close >= 5 AND adv63 >= 5e6
  AND ABS(fwd20) < 1.0
"""


def run():
    print("building PEAD event panel ...")
    df = client().query(SQL).result().to_dataframe(create_bqstorage_client=True)
    df["report_date"] = pd.to_datetime(df["report_date"])
    print(f"{len(df):,} liquid earnings events, "
          f"{df.report_date.dt.year.min()}–{df.report_date.dt.year.max()}, "
          f"{len(df)/df.report_date.dt.year.nunique():,.0f}/yr")

    # SUE quintiles within each quarter (cross-sectional)
    df["q"] = df["report_date"].dt.to_period("Q")
    df["sue_q"] = df.groupby("q")["surprise_pct"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False))
    print("\ndrift by SUE quintile (mean fwd returns, open-entry):")
    g = df.groupby("sue_q")[["fwd5", "fwd20", "fwd60"]].mean() * 100
    g.index = ["Q1 (worst miss)", "Q2", "Q3", "Q4", "Q5 (best beat)"]
    print(g.round(2).to_string())

    ls20 = df[df.sue_q == 4]["fwd20"].mean() - df[df.sue_q == 0]["fwd20"].mean()
    ls5 = df[df.sue_q == 4]["fwd5"].mean() - df[df.sue_q == 0]["fwd5"].mean()
    print(f"\nL/S spread (Q5-Q1): 5d {ls5*100:+.2f}%  20d {ls20*100:+.2f}% "
          f"(costs ~0.2-0.4% round trip both legs)")

    print("\nper-period L/S 20d spread (%):")
    for a, b in [(2003, 2009), (2010, 2015), (2016, 2021), (2022, 2026)]:
        sub = df[(df.report_date.dt.year >= a) & (df.report_date.dt.year <= b)]
        if len(sub) < 200:
            continue
        s = (sub[sub.sue_q == 4]["fwd20"].mean()
             - sub[sub.sue_q == 0]["fwd20"].mean()) * 100
        n = len(sub)
        print(f"  {a}-{b}: {s:+.2f}%  (n={n:,})")

    # simple tradable estimate: ~top/bottom quintile events, 20d hold,
    # equal-weight, both legs, 30bp round trip per leg
    t = df[df.sue_q.isin([0, 4])].copy()
    t["pnl"] = np.where(t.sue_q == 4, t.fwd20, -t.fwd20) - 0.003
    events_yr = len(t) / df.report_date.dt.year.nunique()
    ann = t["pnl"].mean() * events_yr / 40  # ~40 concurrent positions at 20d hold
    print(f"\nnaive net estimate: {t['pnl'].mean()*100:+.2f}%/event, "
          f"{events_yr:,.0f} tradable events/yr; "
          f"portfolio-level ≈ {t['pnl'].mean() * min(events_yr/12.6, 999):.1%}/yr "
          f"at ~20d holds (rough)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    run()
