"""EODHD Fundamentals → BigQuery (requires the Fundamentals Data Feed plan).

    python3 -m quant.data.fundamentals_ingest --backfill   # XSR universe
    python3 -m quant.data.fundamentals_ingest --earnings   # earnings history

Quota economics: the fundamentals endpoint costs ~10 units/symbol; the
~6.2k-symbol XSR universe ≈ 62k units — fits inside one day's 100k budget.

Point-in-time discipline: EODHD fundamentals are RESTATED current values
with per-period date arrays. We store (symbol, period_end, filing-ish date)
rows and downstream feature code must lag by `report_date` + 1 trading day
(or period_end + 90d when report_date is missing — conservative).
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import sys
import time

import pandas as pd
import requests
from google.cloud import bigquery

from quant.config import BQ_DATASET, EODHD_TOKEN, GCP_PROJECT
from quant.data.bq import ensure_table, load_df, query

T_FUND_Q = f"{GCP_PROJECT}.{BQ_DATASET}.fundamentals_quarterly"
T_EARN = f"{GCP_PROJECT}.{BQ_DATASET}.earnings_history"
T_SHARES = f"{GCP_PROJECT}.{BQ_DATASET}.shares_outstanding"

SESSION = requests.Session()

FUND_SCHEMA = [
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("period_end", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("report_date", "DATE"),
    bigquery.SchemaField("revenue", "FLOAT64"),
    bigquery.SchemaField("net_income", "FLOAT64"),
    bigquery.SchemaField("gross_profit", "FLOAT64"),
    bigquery.SchemaField("operating_income", "FLOAT64"),
    bigquery.SchemaField("total_assets", "FLOAT64"),
    bigquery.SchemaField("total_equity", "FLOAT64"),
    bigquery.SchemaField("total_debt", "FLOAT64"),
    bigquery.SchemaField("cash", "FLOAT64"),
    bigquery.SchemaField("operating_cf", "FLOAT64"),
    bigquery.SchemaField("capex", "FLOAT64"),
    bigquery.SchemaField("shares_out", "FLOAT64"),
    bigquery.SchemaField("sector", "STRING"),
    bigquery.SchemaField("industry", "STRING"),
    bigquery.SchemaField("fetched_at", "DATE"),
]

EARN_SCHEMA = [
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("report_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("period_end", "DATE"),
    bigquery.SchemaField("before_after_market", "STRING"),
    bigquery.SchemaField("eps_actual", "FLOAT64"),
    bigquery.SchemaField("eps_estimate", "FLOAT64"),
    bigquery.SchemaField("surprise_pct", "FLOAT64"),
    bigquery.SchemaField("fetched_at", "DATE"),
]


def fetch_fundamentals(symbol: str) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    url = (f"https://eodhd.com/api/fundamentals/{symbol}.US"
           f"?api_token={EODHD_TOKEN}&fmt=json")
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=60)
            if r.status_code == 404:
                return None
            if r.status_code == 402:
                time.sleep(600)
                continue
            if r.status_code == 429:
                time.sleep(45)
                continue
            if r.status_code != 200:
                return None
            j = r.json()
            break
        except Exception:  # noqa: BLE001
            if attempt == 2:
                return None
            time.sleep(2 ** attempt)
    else:
        return None

    gen = j.get("General") or {}
    fin = j.get("Financials") or {}
    earn = j.get("Earnings") or {}
    today = dt.date.today()

    inc = (fin.get("Income_Statement") or {}).get("quarterly") or {}
    bal = (fin.get("Balance_Sheet") or {}).get("quarterly") or {}
    cfl = (fin.get("Cash_Flow") or {}).get("quarterly") or {}
    rows = []
    for pe, i in inc.items():
        b = bal.get(pe) or {}
        c = cfl.get(pe) or {}
        def f(d, k):
            v = d.get(k)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        rows.append({
            "symbol": symbol, "period_end": pe,
            "report_date": i.get("filing_date"),
            "revenue": f(i, "totalRevenue"), "net_income": f(i, "netIncome"),
            "gross_profit": f(i, "grossProfit"),
            "operating_income": f(i, "operatingIncome"),
            "total_assets": f(b, "totalAssets"),
            "total_equity": f(b, "totalStockholderEquity"),
            "total_debt": f(b, "shortLongTermDebtTotal"),
            "cash": f(b, "cashAndEquivalents"),
            "operating_cf": f(c, "totalCashFromOperatingActivities"),
            "capex": f(c, "capitalExpenditures"),
            "shares_out": f(b, "commonStockSharesOutstanding"),
            "sector": gen.get("Sector"), "industry": gen.get("Industry"),
            "fetched_at": today,
        })
    fdf = pd.DataFrame(rows)
    if not fdf.empty:
        for c_ in ["period_end", "report_date", "fetched_at"]:
            fdf[c_] = pd.to_datetime(fdf[c_], errors="coerce").dt.date
        fdf = fdf.dropna(subset=["period_end"])

    hist = (earn.get("History") or {})
    erows = []
    for rd, e in hist.items():
        def g(k):
            v = e.get(k)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        erows.append({
            "symbol": symbol, "report_date": e.get("reportDate") or rd,
            "period_end": e.get("date"),
            "before_after_market": e.get("beforeAfterMarket"),
            "eps_actual": g("epsActual"), "eps_estimate": g("epsEstimate"),
            "surprise_pct": g("surprisePercent"), "fetched_at": today,
        })
    edf = pd.DataFrame(erows)
    if not edf.empty:
        for c_ in ["report_date", "period_end", "fetched_at"]:
            edf[c_] = pd.to_datetime(edf[c_], errors="coerce").dt.date
        edf = edf.dropna(subset=["report_date"])
    return fdf, edf


def universe() -> list[str]:
    from quant.config import T_FEATURES
    df = query(f"SELECT DISTINCT symbol FROM `{T_FEATURES}`")
    return sorted(df["symbol"])


def backfill(workers: int = 8):
    ensure_table(T_FUND_Q, FUND_SCHEMA, partition_field="period_end",
                 clustering=["symbol"])
    ensure_table(T_EARN, EARN_SCHEMA, partition_field="report_date",
                 clustering=["symbol"])
    syms = universe()
    done = set()
    try:
        done = set(query(
            f"SELECT DISTINCT symbol FROM `{T_FUND_Q}`")["symbol"])
    except Exception:  # noqa: BLE001
        pass
    todo = [s for s in syms if s not in done]
    print(f"{len(todo):,} symbols to fetch ({len(done):,} done)")

    fbuf, ebuf, fetched, empty = [], [], 0, 0
    t0 = time.time()

    def flush():
        nonlocal fbuf, ebuf
        if fbuf:
            load_df(T_FUND_Q, pd.concat(fbuf, ignore_index=True),
                    schema=FUND_SCHEMA)
        if ebuf:
            load_df(T_EARN, pd.concat(ebuf, ignore_index=True),
                    schema=EARN_SCHEMA)
        fbuf, ebuf = [], []
        rate = fetched / max(time.time() - t0, 1) * 60
        print(f"{fetched:,}/{len(todo):,} symbols ({empty:,} empty) "
              f"| {rate:.0f}/min", flush=True)

    ex = cf.ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {ex.submit(fetch_fundamentals, s): s for s in todo}
        for fut in cf.as_completed(futs):
            fetched += 1
            res = fut.result()
            if res is None:
                empty += 1
            else:
                fdf, edf = res
                if not fdf.empty:
                    fbuf.append(fdf)
                if not edf.empty:
                    ebuf.append(edf)
            if fetched % 500 == 0:
                flush()
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    flush()
    print("DONE.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args()
    if not args.backfill:
        p.print_help()
        sys.exit(1)
    backfill(args.workers)
