"""Binance Vision (freie S3-Dumps) → BigQuery: Krypto-Historie 2017+.

    python3 -m quant.data.binance_ingest --klines     # Spot-Tageskerzen
    python3 -m quant.data.binance_ingest --funding    # Perp-Funding-Rates

Verlängert die CTREND-Datenbasis von Alpaca-2021+ auf 2017+ (inkl.
2018-Bär und 2020-Crash) und liefert Funding-Rates als Carry-Feature.
Statisches S3, keine Limits, ToS sauber.
"""

import argparse
import datetime as dt
import io
import sys
import zipfile

import pandas as pd
import requests
from google.cloud import bigquery

from quant.config import BQ_DATASET, GCP_PROJECT
from quant.data.bq import ensure_table, load_df

T_KLINES = f"{GCP_PROJECT}.{BQ_DATASET}.binance_daily"
T_FUNDING = f"{GCP_PROJECT}.{BQ_DATASET}.binance_funding"

BASE = "https://data.binance.vision/data"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "AVAXUSDT",
           "LTCUSDT", "DOGEUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT"]

KLINE_SCHEMA = [
    bigquery.SchemaField("date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("open", "FLOAT64"),
    bigquery.SchemaField("high", "FLOAT64"),
    bigquery.SchemaField("low", "FLOAT64"),
    bigquery.SchemaField("close", "FLOAT64"),
    bigquery.SchemaField("volume", "FLOAT64"),
    bigquery.SchemaField("quote_volume", "FLOAT64"),
    bigquery.SchemaField("trades", "INT64"),
]

FUNDING_SCHEMA = [
    bigquery.SchemaField("ts", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("funding_rate", "FLOAT64"),
]


def _get(url: str, tries: int = 4):
    """Download mit Retry — Binance resettet Verbindungen bei vielen Requests."""
    import time
    for i in range(tries):
        try:
            return requests.get(url, timeout=60)
        except requests.exceptions.RequestException:
            if i == tries - 1:
                return None
            time.sleep(2 ** i)
    return None


def _months(start: str):
    d = dt.date.fromisoformat(start).replace(day=1)
    today = dt.date.today().replace(day=1)
    while d < today:
        yield f"{d:%Y-%m}"
        d = (d + dt.timedelta(days=32)).replace(day=1)


def _fix_ts(v: float) -> pd.Timestamp:
    # ms bis 2024, µs ab 2025 in manchen Dumps
    return pd.Timestamp(int(v), unit="us" if v > 1e14 else "ms", tz="UTC")


def _daily_files(sym: str, kind: str = "klines"):
    """Tagesdateien des LAUFENDEN Monats — Binance publiziert Monatsdumps erst
    nach Monatsende, sonst hängt die Tabelle immer am Vormonatsende."""
    today = dt.date.today()
    first = today.replace(day=1)
    d = first
    while d < today:
        yield f"{d:%Y-%m-%d}"
        d += dt.timedelta(days=1)


def klines(start="2017-08-01"):
    ensure_table(T_KLINES, KLINE_SCHEMA, partition_field="date",
                 clustering=["symbol"])
    frames, misses = [], 0
    for sym in SYMBOLS:
        rows = 0
        for m in _months(start):
            url = f"{BASE}/spot/monthly/klines/{sym}/1d/{sym}-1d-{m}.zip"
            r = _get(url)
            if r is None or r.status_code != 200:
                misses += 1
                continue
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                raw = z.read(z.namelist()[0]).decode()
            df = pd.read_csv(io.StringIO(raw), header=None)
            if isinstance(df.iloc[0, 0], str):  # Header-Zeile ab 2025-Dumps
                df = df.iloc[1:].reset_index(drop=True)
            df = df.iloc[:, :9]
            df.columns = ["open_time", "open", "high", "low", "close",
                          "volume", "close_time", "quote_volume", "trades"]
            df["date"] = [_fix_ts(float(v)).date() for v in df["open_time"]]
            df["symbol"] = sym
            for c in ["open", "high", "low", "close", "volume",
                      "quote_volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df["trades"] = pd.to_numeric(df["trades"],
                                         errors="coerce").astype("Int64")
            frames.append(df[["date", "symbol", "open", "high", "low",
                              "close", "volume", "quote_volume", "trades"]])
            rows += len(df)
        # laufender Monat aus Tagesdateien
        for day in _daily_files(sym):
            url = f"{BASE}/spot/daily/klines/{sym}/1d/{sym}-1d-{day}.zip"
            r = _get(url)
            if r is None or r.status_code != 200:
                continue
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                raw = z.read(z.namelist()[0]).decode()
            df = pd.read_csv(io.StringIO(raw), header=None)
            if isinstance(df.iloc[0, 0], str):
                df = df.iloc[1:].reset_index(drop=True)
            df = df.iloc[:, :9]
            df.columns = ["open_time", "open", "high", "low", "close",
                          "volume", "close_time", "quote_volume", "trades"]
            df["date"] = [_fix_ts(float(v)).date() for v in df["open_time"]]
            df["symbol"] = sym
            for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df["trades"] = pd.to_numeric(df["trades"],
                                         errors="coerce").astype("Int64")
            frames.append(df[["date", "symbol", "open", "high", "low",
                              "close", "volume", "quote_volume", "trades"]])
            rows += len(df)
        print(f"{sym}: {rows:,} Tage", flush=True)
    out = pd.concat(frames, ignore_index=True)
    load_df(T_KLINES, out, schema=KLINE_SCHEMA, write="WRITE_TRUNCATE")
    print(f"binance_daily: {len(out):,} Zeilen ({misses} Monats-Lücken/404)")


def funding(start="2020-09-01"):
    ensure_table(T_FUNDING, FUNDING_SCHEMA, partition_field="ts",
                 clustering=["symbol"])
    frames = []
    for sym in SYMBOLS:
        rows = 0
        for m in _months(start):
            url = (f"{BASE}/futures/um/monthly/fundingRate/{sym}/"
                   f"{sym}-fundingRate-{m}.zip")
            r = _get(url)
            if r is None or r.status_code != 200:
                continue
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                raw = z.read(z.namelist()[0]).decode()
            df = pd.read_csv(io.StringIO(raw))
            tscol = "calc_time" if "calc_time" in df.columns \
                else df.columns[0]
            ratecol = "last_funding_rate" if "last_funding_rate" in df.columns \
                else df.columns[-1]
            out = pd.DataFrame({
                "ts": [_fix_ts(float(v)) for v in df[tscol]],
                "symbol": sym,
                "funding_rate": pd.to_numeric(df[ratecol], errors="coerce"),
            })
            frames.append(out)
            rows += len(out)
        print(f"{sym} funding: {rows:,}", flush=True)
    out = pd.concat(frames, ignore_index=True)
    load_df(T_FUNDING, out, schema=FUNDING_SCHEMA, write="WRITE_TRUNCATE")
    print(f"binance_funding: {len(out):,} Zeilen")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--klines", action="store_true")
    p.add_argument("--funding", action="store_true")
    args = p.parse_args()
    if not (args.klines or args.funding):
        p.print_help()
        sys.exit(1)
    if args.klines:
        klines()
    if args.funding:
        funding()
