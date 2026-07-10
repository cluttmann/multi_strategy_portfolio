"""Alpaca SIP minute bars → BigQuery.

    python3 -m quant.data.minute_ingest --backfill-imom   # 27 IMOM ETFs, 2016→now
    python3 -m quant.data.minute_ingest --symbols QQQ,IWM --start 2020-01-01

Used by the IMOM sleeve (full history for its fixed ETF universe) and later
lazily for GAP/CAT candidate days. SIP feed on historical endpoints is free;
only the most recent 15 minutes are embargoed, which never matters here.
"""

import argparse
import datetime as dt
import sys
import time

import pandas as pd
import requests
from google.cloud import bigquery

from quant.config import ALPACA_KEY_PAPER, ALPACA_SECRET_PAPER, T_MINUTE
from quant.data.bq import ensure_table, load_df, scalar

# The fixed IMOM universe from DESIGN.md §1 Sleeve 1.
IMOM_ETFS = [
    "QQQ", "IWM", "DIA",
    "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
    "GLD", "SLV", "EEM", "EFA", "FXI", "EWZ", "HYG", "LQD", "IEF",
    "GDX", "XOP", "KRE", "SMH", "XBI",
]

MINUTE_SCHEMA = [
    bigquery.SchemaField("ts", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("open", "FLOAT64"),
    bigquery.SchemaField("high", "FLOAT64"),
    bigquery.SchemaField("low", "FLOAT64"),
    bigquery.SchemaField("close", "FLOAT64"),
    bigquery.SchemaField("volume", "INT64"),
    bigquery.SchemaField("trade_count", "INT64"),
    bigquery.SchemaField("vwap", "FLOAT64"),
]

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY_PAPER,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_PAPER,
}
URL = "https://data.alpaca.markets/v2/stocks/{sym}/bars"


def fetch_symbol(symbol: str, start: str, end: str | None, flush_rows: int = 500_000):
    """Stream one symbol's minute bars into BQ in chunks."""
    params = {
        "timeframe": "1Min", "feed": "sip", "adjustment": "all",
        "start": f"{start}T00:00:00Z",
        "limit": 10_000,
    }
    # Free plan forbids an explicit `end` inside the recent-SIP window
    # (mind UTC vs local dates); omitting `end` lets Alpaca clamp it.
    if end:
        params["end"] = f"{end}T23:59:59Z"
    buf, total = [], 0
    while True:
        r = requests.get(URL.format(sym=symbol), headers=HEADERS,
                         params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(5)
            continue
        r.raise_for_status()
        j = r.json()
        for b in j.get("bars") or []:
            buf.append({
                "ts": b["t"], "symbol": symbol, "open": b["o"], "high": b["h"],
                "low": b["l"], "close": b["c"], "volume": b["v"],
                "trade_count": b.get("n"), "vwap": b.get("vw"),
            })
        token = j.get("next_page_token")
        if len(buf) >= flush_rows or (not token and buf):
            df = pd.DataFrame(buf)
            df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
            load_df(T_MINUTE, df, schema=MINUTE_SCHEMA)
            total += len(buf)
            buf = []
        if not token:
            break
        params["page_token"] = token
        time.sleep(0.31)
    print(f"{symbol}: {total:,} bars", flush=True)
    return total


def backfill(symbols: list[str], start: str, end: str | None = None):
    ensure_table(T_MINUTE, MINUTE_SCHEMA, partition_field="ts",
                 clustering=["symbol"])
    # Free-plan SIP rule: an explicit `end` inside the recent window 403s
    # (mind UTC vs local dates). Leave end=None and Alpaca clamps it safely.
    horizon = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=3)
    grand = 0
    for sym in symbols:
        max_ts = scalar(
            f"SELECT MAX(ts) FROM `{T_MINUTE}` WHERE symbol = '{sym}'")
        sym_start = start
        if max_ts is not None:
            if max_ts.date() >= horizon:
                print(f"{sym}: already current ({max_ts.date()}), skipping")
                continue
            sym_start = (max_ts + dt.timedelta(minutes=1)).date().isoformat()
        grand += fetch_symbol(sym, sym_start, end)
    print(f"DONE. {grand:,} bars total.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--backfill-imom", action="store_true")
    p.add_argument("--symbols")
    p.add_argument("--start", default="2016-01-01")
    p.add_argument("--end")
    args = p.parse_args()
    if args.backfill_imom:
        backfill(IMOM_ETFS, args.start, args.end)
    elif args.symbols:
        backfill(args.symbols.split(","), args.start, args.end)
    else:
        p.print_help()
        sys.exit(1)
