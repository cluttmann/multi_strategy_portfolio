"""Blue Ocean ATS (overnight session) bars → BigQuery.

    python3 -m quant.data.boats_ingest --backfill

The 24/5 overnight session (8:00 PM–4:00 AM ET, Sun–Fri) launched ~Feb 2026;
BOATS historical bars exist from ~2026-01. This is a brand-new venue — the
data volume is tiny (thin session) but it is the complete history of the
session, and our Benzinga news corpus covers the same window.

Universe: the ONX 3x-ETF universe + large liquid names (screener most-actives
snapshot + a fixed mega-cap list) — overnight liquidity concentrates in
exactly these.
"""

import argparse
import sys
import time

import pandas as pd
import requests
from google.cloud import bigquery

from quant.config import (ALPACA_KEY_PAPER, ALPACA_SECRET_PAPER, BQ_DATASET,
                          GCP_PROJECT)
from quant.data.bq import ensure_table, load_df, scalar

T_BOATS = f"{GCP_PROJECT}.{BQ_DATASET}.boats_bars"

H = {"APCA-API-KEY-ID": ALPACA_KEY_PAPER,
     "APCA-API-SECRET-KEY": ALPACA_SECRET_PAPER}

MEGA = ["TSLA", "NVDA", "AAPL", "AMZN", "META", "MSFT", "GOOGL", "AMD",
        "PLTR", "COIN", "MSTR", "SMCI", "AVGO", "NFLX", "HOOD", "SOFI",
        "RIVN", "LCID", "NIO", "MARA", "RIOT", "GME", "AMC", "BABA",
        "INTC", "F", "T", "BAC", "XOM", "CVX"]
ONX_UNIVERSE = ["SOXL", "SOXS", "TNA", "TZA", "TQQQ", "SQQQ", "TECL", "FAS",
                "LABU", "UDOW", "DFEN", "DPST", "URTY", "MIDU", "NAIL",
                "RETL", "CURE", "DRN", "WEBL", "HIBL", "UTSL", "EDC",
                "YINN", "IWM", "QQQ", "DIA", "GLD", "SLV", "XLF", "XLE"]

SCHEMA = [
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


def most_actives(top=60) -> list[str]:
    try:
        r = requests.get(
            "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives",
            headers=H, params={"by": "volume", "top": top}, timeout=30)
        r.raise_for_status()
        return [a["symbol"] for a in r.json().get("most_actives", [])]
    except Exception:  # noqa: BLE001
        return []


def fetch_symbol(symbol: str, start: str) -> int:
    params = {"timeframe": "1Min", "feed": "boats", "limit": 10_000,
              "start": f"{start}T00:00:00Z"}
    buf, total = [], 0
    while True:
        try:
            r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                             headers=H, params=params, timeout=60)
        except requests.exceptions.ConnectionError:
            time.sleep(20)
            continue
        if r.status_code == 429:
            time.sleep(10)
            continue
        if r.status_code != 200:
            print(f"{symbol}: HTTP {r.status_code} {r.text[:60]}")
            return total
        j = r.json()
        for b in j.get("bars") or []:
            buf.append({"ts": b["t"], "symbol": symbol, "open": b["o"],
                        "high": b["h"], "low": b["l"], "close": b["c"],
                        "volume": b["v"], "trade_count": b.get("n"),
                        "vwap": b.get("vw")})
        tok = j.get("next_page_token")
        if not tok:
            break
        params["page_token"] = tok
        time.sleep(0.35)
    if buf:
        df = pd.DataFrame(buf)
        df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
        load_df(T_BOATS, df, schema=SCHEMA)
        total = len(buf)
    print(f"{symbol}: {total:,} boats bars", flush=True)
    return total


def backfill(start="2026-01-01"):
    ensure_table(T_BOATS, SCHEMA, partition_field="ts", clustering=["symbol"])
    universe = list(dict.fromkeys(ONX_UNIVERSE + MEGA + most_actives()))
    print(f"universe: {len(universe)} symbols")
    done = set()
    try:
        from quant.data.bq import query
        done = set(query(f"SELECT DISTINCT symbol FROM `{T_BOATS}`")["symbol"])
    except Exception:  # noqa: BLE001
        pass
    grand = 0
    for s in universe:
        if s in done:
            continue
        grand += fetch_symbol(s, start)
    print(f"DONE. {grand:,} bars.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--start", default="2026-01-01")
    args = p.parse_args()
    if not args.backfill:
        p.print_help()
        sys.exit(1)
    backfill(args.start)
