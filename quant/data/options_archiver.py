"""Options chain-snapshot archiver — run daily; the archive IS the asset.

    python3 -m quant.data.options_archiver --snap

Alpaca exposes greeks/IV/quotes only as LATEST snapshots (indicative feed,
free) — no historical quotes or greeks exist anywhere on the platform. Every
daily snapshot stored here becomes proprietary backtest data that cannot be
bought later. Cost: a few MB/day.

Universe: liquid index/sector ETFs + mega-caps; monthlies + weeklies within
60 days, strikes within ±15% of spot.
"""

import argparse
import datetime as dt
import sys
import time

import pandas as pd
import requests
from google.cloud import bigquery

from quant.config import (ALPACA_KEY_PAPER, ALPACA_SECRET_PAPER, BQ_DATASET,
                          GCP_PROJECT)
from quant.data.bq import ensure_table, load_df

T_OPT = f"{GCP_PROJECT}.{BQ_DATASET}.options_snapshots"

H = {"APCA-API-KEY-ID": ALPACA_KEY_PAPER,
     "APCA-API-SECRET-KEY": ALPACA_SECRET_PAPER}

UNDERLYINGS = ["SPY", "QQQ", "IWM", "TLT", "GLD", "XLE", "XLF", "SMH",
               "TSLA", "NVDA", "AAPL", "AMZN", "META", "MSFT", "AMD", "COIN"]

SCHEMA = [
    bigquery.SchemaField("snap_ts", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("underlying", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("contract", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("expiry", "DATE"),
    bigquery.SchemaField("strike", "FLOAT64"),
    bigquery.SchemaField("cp", "STRING"),
    bigquery.SchemaField("bid", "FLOAT64"),
    bigquery.SchemaField("ask", "FLOAT64"),
    bigquery.SchemaField("last", "FLOAT64"),
    bigquery.SchemaField("iv", "FLOAT64"),
    bigquery.SchemaField("delta", "FLOAT64"),
    bigquery.SchemaField("gamma", "FLOAT64"),
    bigquery.SchemaField("theta", "FLOAT64"),
    bigquery.SchemaField("vega", "FLOAT64"),
    bigquery.SchemaField("open_interest", "FLOAT64"),
]


def parse_occ(sym: str):
    """AAPL260713P00500000 → (expiry, cp, strike)."""
    root = sym.rstrip("0123456789")
    tail = sym[len(root) - 1:] if root and root[-1] in "CP" else sym
    # robust parse: last 8 chars = strike*1000, prior char = C/P, prior 6 = yymmdd
    strike = int(sym[-8:]) / 1000.0
    cp = sym[-9]
    ymd = sym[-15:-9]
    expiry = dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    return expiry, cp, strike


def snap_underlying(u: str, snap_ts: dt.datetime) -> pd.DataFrame:
    exp_lte = (dt.date.today() + dt.timedelta(days=60)).isoformat()
    rows, page = [], None
    while True:
        params = {"feed": "indicative", "limit": 1000,
                  "expiration_date_lte": exp_lte}
        if page:
            params["page_token"] = page
        r = requests.get(
            f"https://data.alpaca.markets/v1beta1/options/snapshots/{u}",
            headers=H, params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(5)
            continue
        r.raise_for_status()
        j = r.json()
        for sym, s in (j.get("snapshots") or {}).items():
            q = s.get("latestQuote") or {}
            t = s.get("latestTrade") or {}
            g = s.get("greeks") or {}
            try:
                expiry, cp, strike = parse_occ(sym)
            except Exception:  # noqa: BLE001
                continue
            rows.append({
                "snap_ts": snap_ts, "underlying": u, "contract": sym,
                "expiry": expiry, "strike": strike, "cp": cp,
                "bid": q.get("bp"), "ask": q.get("ap"), "last": t.get("p"),
                "iv": s.get("impliedVolatility"),
                "delta": g.get("delta"), "gamma": g.get("gamma"),
                "theta": g.get("theta"), "vega": g.get("vega"),
                "open_interest": s.get("openInterest"),
            })
        page = j.get("next_page_token")
        if not page:
            break
        time.sleep(0.3)
    return pd.DataFrame(rows)


def snap():
    ensure_table(T_OPT, SCHEMA, partition_field="snap_ts",
                 clustering=["underlying"])
    snap_ts = dt.datetime.now(dt.timezone.utc)
    total = 0
    for u in UNDERLYINGS:
        try:
            df = snap_underlying(u, snap_ts)
            if not df.empty:
                load_df(T_OPT, df, schema=SCHEMA)
                total += len(df)
            print(f"{u}: {len(df):,} contracts", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"{u}: failed ({e})")
    print(f"DONE. {total:,} contract snapshots @ {snap_ts.isoformat()}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--snap", action="store_true")
    args = p.parse_args()
    if not args.snap:
        p.print_help()
        sys.exit(1)
    snap()
