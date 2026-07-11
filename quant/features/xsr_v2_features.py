"""XSR v2 feature layer — fundamentals joined point-in-time onto features_daily.

    python3 -m quant.features.xsr_v2_features --build

Produces quant.features_daily_v2 = features_daily + fundamentals features,
with strict PIT discipline: a quarter's numbers become usable only from
report_date + 1 trading day (fallback: period_end + 90 days when the filing
date is missing — conservative). All new features are cross-sectionally
z-scored within date, consistent with v1.

New features (families with decades of published cross-sectional evidence):
  value:    ep_ttm, bp, sp_ttm, fcfp_ttm
  quality:  roe_ttm, gross_margin_ttm, accruals (NI-OCF)/assets
  issuance: net share issuance over ~4 quarters (negative = buybacks)
  earnings: latest SUE (surprise %), days since last report
  sector:   integer-coded real sector (LightGBM categorical)
"""

import argparse
import sys

from quant.config import BQ_DATASET, GCP_PROJECT, T_EOD, T_FEATURES
from quant.data.bq import client

T_FUND_Q = f"{GCP_PROJECT}.{BQ_DATASET}.fundamentals_quarterly"
T_EARN = f"{GCP_PROJECT}.{BQ_DATASET}.earnings_history"
T_V2 = f"{GCP_PROJECT}.{BQ_DATASET}.features_daily_v2"

V2_CS_FEATURES = ["ep_ttm", "bp", "sp_ttm", "fcfp_ttm", "roe_ttm",
                  "gross_margin_ttm", "accruals", "issuance_4q", "sue"]

SQL = f"""
CREATE OR REPLACE TABLE `{T_V2}`
PARTITION BY DATE_TRUNC(date, MONTH)
CLUSTER BY symbol AS
WITH fq AS (
  SELECT symbol, period_end,
    COALESCE(report_date, DATE_ADD(period_end, INTERVAL 90 DAY)) AS eff_from,
    revenue, net_income, gross_profit, total_equity, total_assets,
    operating_cf, capex, shares_out, sector
  FROM `{T_FUND_Q}`
  WHERE period_end >= '1999-01-01'
),
-- TTM sums over the trailing 4 quarters per symbol
ttm AS (
  SELECT symbol, period_end, eff_from, total_equity, total_assets,
    shares_out, sector,
    SUM(revenue)     OVER w4 AS rev_ttm,
    SUM(net_income)  OVER w4 AS ni_ttm,
    SUM(gross_profit) OVER w4 AS gp_ttm,
    SUM(operating_cf) OVER w4 AS ocf_ttm,
    SUM(capex)       OVER w4 AS capex_ttm,
    LAG(shares_out, 4) OVER (PARTITION BY symbol ORDER BY period_end) AS shares_4q_ago
  FROM fq
  WINDOW w4 AS (PARTITION BY symbol ORDER BY period_end
                ROWS BETWEEN 3 PRECEDING AND CURRENT ROW)
),
earn AS (
  SELECT symbol, report_date,
    DATE_ADD(report_date, INTERVAL 1 DAY) AS eff_from,
    surprise_pct
  FROM `{T_EARN}` WHERE report_date IS NOT NULL
),
base AS (
  SELECT f.*, e.raw_close_px
  FROM `{T_FEATURES}` f
  JOIN (SELECT date, symbol, close AS raw_close_px FROM `{T_EOD}`) e
    USING (date, symbol)
),
-- most recent fundamentals row effective on or before each panel date
joined AS (
  SELECT b.*, t.rev_ttm, t.ni_ttm, t.gp_ttm, t.ocf_ttm, t.capex_ttm,
    t.total_equity, t.total_assets, t.shares_out, t.shares_4q_ago, t.sector
  FROM base b
  LEFT JOIN ttm t
    ON t.symbol = b.symbol
   AND t.eff_from <= b.date
   AND t.eff_from > DATE_SUB(b.date, INTERVAL 400 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY b.date, b.symbol
                             ORDER BY t.eff_from DESC) = 1
),
joined2 AS (
  SELECT j.*, e.surprise_pct AS sue,
    DATE_DIFF(j.date, e.report_date, DAY) AS days_since_report
  FROM joined j
  LEFT JOIN earn e
    ON e.symbol = j.symbol
   AND e.eff_from <= j.date
   AND e.eff_from > DATE_SUB(j.date, INTERVAL 200 DAY)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY j.date, j.symbol
                             ORDER BY e.eff_from DESC) = 1
),
feat AS (
  SELECT *,
    shares_out * raw_close_px AS mcap,
    SAFE_DIVIDE(ni_ttm, NULLIF(shares_out * raw_close_px, 0)) AS ep_ttm,
    SAFE_DIVIDE(total_equity, NULLIF(shares_out * raw_close_px, 0)) AS bp,
    SAFE_DIVIDE(rev_ttm, NULLIF(shares_out * raw_close_px, 0)) AS sp_ttm,
    SAFE_DIVIDE(ocf_ttm + capex_ttm, NULLIF(shares_out * raw_close_px, 0)) AS fcfp_ttm,
    SAFE_DIVIDE(ni_ttm, NULLIF(total_equity, 0)) AS roe_ttm,
    SAFE_DIVIDE(gp_ttm, NULLIF(rev_ttm, 0)) AS gross_margin_ttm,
    SAFE_DIVIDE(ni_ttm - ocf_ttm, NULLIF(total_assets, 0)) AS accruals,
    SAFE_DIVIDE(shares_out, NULLIF(shares_4q_ago, 0)) - 1 AS issuance_4q
  FROM joined2
)
SELECT * EXCEPT(rev_ttm, ni_ttm, gp_ttm, ocf_ttm, capex_ttm, total_equity,
                total_assets, shares_out, shares_4q_ago, raw_close_px),
  {", ".join(
    f"SAFE_DIVIDE({c} - AVG({c}) OVER (PARTITION BY date), "
    f"NULLIF(STDDEV({c}) OVER (PARTITION BY date), 0)) AS z_{c}"
    for c in V2_CS_FEATURES)},
  DENSE_RANK() OVER (ORDER BY IFNULL(sector, 'Unknown')) AS sector_id
FROM feat
"""


def build():
    print("materializing features_daily_v2 ...")
    client().query(SQL).result()
    stats = client().query(f"""
        SELECT COUNT(*) n,
          COUNTIF(ep_ttm IS NOT NULL) with_fund,
          COUNTIF(sue IS NOT NULL) with_sue,
          MIN(date) lo, MAX(date) hi
        FROM `{T_V2}`""").result()
    for r in stats:
        print(f"v2 panel: {r.n:,} rows | fundamentals coverage "
              f"{r.with_fund/r.n:.0%} | SUE coverage {r.with_sue/r.n:.0%} | "
              f"{r.lo} → {r.hi}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--build", action="store_true")
    args = p.parse_args()
    if not args.build:
        p.print_help()
        sys.exit(1)
    build()
