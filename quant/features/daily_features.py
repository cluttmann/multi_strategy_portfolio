"""Daily cross-sectional feature/label pipeline (BigQuery SQL).

    python3 -m quant.features.daily_features --build

Reads quant.eod_bars (full survivorship-free cross-section), computes
time-series features per symbol, restricts to a point-in-time liquid
common-stock universe, adds cross-sectional z-scores, and materializes
quant.features_daily.

Execution alignment: signals are computed from day-t close data; entry is
assumed at day t+1 OPEN. All labels are open(t+1) → open(t+1+h) so the
backtest can never trade on information it wouldn't have had.

BigQuery quirks honored here: no SKEW function (computed from raw moments),
and analytic functions cannot nest (hence the two-stage r → ts structure).
"""

import argparse
import sys

from quant.config import T_EOD, T_FEATURES, T_SYMBOLS
from quant.data.bq import client

UNIVERSE_TOP_N = 1500
MIN_PRICE = 5.0
MIN_ADV_USD = 5_000_000  # 63d average dollar volume floor

# Cross-sectionally z-scored feature columns (within each date's universe).
CS_FEATURES = [
    "ret_1d", "ret_5d", "ret_10d", "ret_21d", "ret_63d", "ret_126d",
    "mom_12m_ex1m", "vol_21d", "vol_63d", "parkinson_21d", "volume_ratio",
    "amihud_21d", "gap_mean_5d", "intraday_mean_5d", "high_52w_prox",
    "sma50_dist", "sma200_dist", "skew_63d", "beta_63d", "log_adv",
]


def final_sql() -> str:
    zcols = ",\n  ".join(
        f"SAFE_DIVIDE({c} - AVG({c}) OVER (PARTITION BY date), "
        f"NULLIF(STDDEV({c}) OVER (PARTITION BY date), 0)) AS z_{c}"
        for c in CS_FEATURES
    )
    return f"""
CREATE OR REPLACE TABLE `{T_FEATURES}`
PARTITION BY DATE_TRUNC(date, MONTH)
CLUSTER BY symbol AS
WITH px AS (
  SELECT
    b.date, b.symbol,
    b.adjusted_close AS ac,
    b.open * SAFE_DIVIDE(b.adjusted_close, b.close) AS ao,
    b.high * SAFE_DIVIDE(b.adjusted_close, b.close) AS ah,
    b.low  * SAFE_DIVIDE(b.adjusted_close, b.close) AS al,
    b.close AS raw_close,
    b.volume,
    b.close * b.volume AS dollar_vol
  FROM `{T_EOD}` b
  JOIN (SELECT DISTINCT symbol FROM `{T_SYMBOLS}` WHERE type = 'Common Stock') s
    USING (symbol)
  WHERE b.close > 0 AND b.adjusted_close > 0 AND b.volume >= 0
),
mkt AS (  -- market return from SPY (data only, never traded)
  SELECT date,
         SAFE_DIVIDE(adjusted_close, LAG(adjusted_close) OVER (ORDER BY date)) - 1
           AS mkt_ret
  FROM `{T_EOD}` WHERE symbol = 'SPY'
),
-- Stage 1: everything needing LAG/LEAD on prices (no nesting allowed later)
r AS (
  SELECT
    p.date, p.symbol, p.raw_close, p.volume, p.dollar_vol, p.ac, p.ao, p.ah, p.al,
    m.mkt_ret,
    SAFE_DIVIDE(p.ac, LAG(p.ac, 1)   OVER w) - 1 AS ret_1d,
    SAFE_DIVIDE(p.ac, LAG(p.ac, 5)   OVER w) - 1 AS ret_5d,
    SAFE_DIVIDE(p.ac, LAG(p.ac, 10)  OVER w) - 1 AS ret_10d,
    SAFE_DIVIDE(p.ac, LAG(p.ac, 21)  OVER w) - 1 AS ret_21d,
    SAFE_DIVIDE(p.ac, LAG(p.ac, 63)  OVER w) - 1 AS ret_63d,
    SAFE_DIVIDE(p.ac, LAG(p.ac, 126) OVER w) - 1 AS ret_126d,
    SAFE_DIVIDE(LAG(p.ac, 21) OVER w, LAG(p.ac, 252) OVER w) - 1 AS mom_12m_ex1m,
    SAFE_DIVIDE(p.ao, LAG(p.ac, 1) OVER w) - 1 AS gap_1d,
    SAFE_DIVIDE(p.ac, NULLIF(p.ao, 0)) - 1 AS intraday_1d,
    POW(LN(SAFE_DIVIDE(p.ah, NULLIF(p.al, 0))), 2) AS hl_sq,
    -- execution-aligned forward labels: open(t+1) -> open(t+1+h)
    SAFE_DIVIDE(LEAD(p.ao, 2)  OVER w, LEAD(p.ao, 1) OVER w) - 1 AS fwd_ret_1d,
    SAFE_DIVIDE(LEAD(p.ao, 6)  OVER w, LEAD(p.ao, 1) OVER w) - 1 AS fwd_ret_5d,
    SAFE_DIVIDE(LEAD(p.ao, 11) OVER w, LEAD(p.ao, 1) OVER w) - 1 AS fwd_ret_10d
  FROM px p
  LEFT JOIN mkt m USING (date)
  WINDOW w AS (PARTITION BY p.symbol ORDER BY p.date)
),
-- Stage 2: rolling aggregates over stage-1 columns
ts AS (
  SELECT
    date, symbol, raw_close, dollar_vol,
    ret_1d, ret_5d, ret_10d, ret_21d, ret_63d, ret_126d, mom_12m_ex1m,
    fwd_ret_1d, fwd_ret_5d, fwd_ret_10d,
    STDDEV(ret_1d) OVER w21 * SQRT(252) AS vol_21d,
    STDDEV(ret_1d) OVER w63 * SQRT(252) AS vol_63d,
    SQRT(AVG(hl_sq) OVER w21 / (4 * LN(2))) * SQRT(252) AS parkinson_21d,
    SAFE_DIVIDE(AVG(volume) OVER w5, NULLIF(AVG(volume) OVER w63, 0)) AS volume_ratio,
    AVG(SAFE_DIVIDE(ABS(ret_1d), NULLIF(dollar_vol, 0))) OVER w21 * 1e9 AS amihud_21d,
    AVG(gap_1d) OVER w5 AS gap_mean_5d,
    AVG(intraday_1d) OVER w5 AS intraday_mean_5d,
    SAFE_DIVIDE(ac, NULLIF(MAX(ac) OVER w252, 0)) AS high_52w_prox,
    SAFE_DIVIDE(ac, NULLIF(AVG(ac) OVER w50, 0)) - 1 AS sma50_dist,
    SAFE_DIVIDE(ac, NULLIF(AVG(ac) OVER w200, 0)) - 1 AS sma200_dist,
    -- skewness from raw moments: (m3 - 3*m1*m2 + 2*m1^3) / (m2 - m1^2)^1.5
    SAFE_DIVIDE(
      AVG(POW(ret_1d, 3)) OVER w63
        - 3 * AVG(ret_1d) OVER w63 * AVG(POW(ret_1d, 2)) OVER w63
        + 2 * POW(AVG(ret_1d) OVER w63, 3),
      NULLIF(POW(AVG(POW(ret_1d, 2)) OVER w63 - POW(AVG(ret_1d) OVER w63, 2), 1.5), 0)
    ) AS skew_63d,
    SAFE_DIVIDE(COVAR_SAMP(ret_1d, mkt_ret) OVER w63,
                NULLIF(VAR_SAMP(mkt_ret) OVER w63, 0)) AS beta_63d,
    LN(NULLIF(AVG(dollar_vol) OVER w63, 0)) AS log_adv,
    AVG(dollar_vol) OVER w63 AS adv63,
    COUNT(ret_1d) OVER w252 AS obs_252
  FROM r
  WINDOW
    w5   AS (PARTITION BY symbol ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
    w21  AS (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND CURRENT ROW),
    w50  AS (PARTITION BY symbol ORDER BY date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW),
    w63  AS (PARTITION BY symbol ORDER BY date ROWS BETWEEN 62 PRECEDING AND CURRENT ROW),
    w200 AS (PARTITION BY symbol ORDER BY date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW),
    w252 AS (PARTITION BY symbol ORDER BY date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW)
),
universe AS (
  SELECT *,
    ROW_NUMBER() OVER (PARTITION BY date ORDER BY adv63 DESC) AS liq_rank
  FROM ts
  WHERE raw_close >= {MIN_PRICE}
    AND adv63 >= {MIN_ADV_USD}
    AND obs_252 >= 200          -- require ~1y of history for long features
    AND mom_12m_ex1m IS NOT NULL
)
SELECT
  date, symbol, raw_close, dollar_vol, adv63, liq_rank,
  {", ".join(CS_FEATURES)},
  {zcols},
  EXTRACT(DAYOFWEEK FROM date) AS dow,
  EXTRACT(MONTH FROM date) AS month,
  fwd_ret_1d, fwd_ret_5d, fwd_ret_10d
FROM universe
WHERE liq_rank <= {UNIVERSE_TOP_N}
"""


def build():
    print("Materializing features_daily ...")
    client().query(final_sql()).result()
    stats = client().query(
        f"SELECT COUNT(*) n, COUNT(DISTINCT symbol) syms, MIN(date) lo, MAX(date) hi "
        f"FROM `{T_FEATURES}`").result()
    for row in stats:
        print(f"features_daily: {row.n:,} rows, {row.syms:,} symbols, "
              f"{row.lo} → {row.hi}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--build", action="store_true")
    p.add_argument("--print-sql", action="store_true")
    args = p.parse_args()
    if args.print_sql:
        print(final_sql())
    elif args.build:
        build()
    else:
        p.print_help()
        sys.exit(1)
