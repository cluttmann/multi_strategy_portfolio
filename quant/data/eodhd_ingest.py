"""EODHD → BigQuery ingestion.

Entry points:
  python3 -m quant.data.eodhd_ingest --backfill-symbols 2000-01-01  # full history
  python3 -m quant.data.eodhd_ingest --update                # yesterday only
  python3 -m quant.data.eodhd_ingest --symbols               # symbols dimension

QUOTA ECONOMICS (learned the hard way): the bulk endpoint costs 100 API calls
per request, so a multi-year bulk backfill burns the 100k daily quota in ~950
days of data. The per-symbol endpoint costs 1 call and returns the symbol's
ENTIRE history — the full backfill is ~30k calls. Bulk is only used for the
daily incremental update (1 bulk call/day = 100 quota units, negligible).

Survivorship: the symbol map includes delisted names, and per-symbol history
works for them, so the cross-section stays survivorship-bias-free.
adjusted_close is adjusted-to-present at fetch time; a single-day full
backfill therefore has a perfectly consistent adjustment basis. A future
split makes stored adjusted_close stale for that symbol — detect via
close/adjusted_close ratio jumps and re-fetch that symbol.
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import io
import os
import sys
import time

import pandas as pd
import requests
from google.cloud import bigquery

from quant.config import EODHD_TOKEN, LISTED_EXCHANGES, STAGING_DIR, T_EOD, T_SYMBOLS
from quant.data.bq import ensure_table, load_df, scalar

EOD_SCHEMA = [
    bigquery.SchemaField("date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("exchange", "STRING"),
    bigquery.SchemaField("open", "FLOAT64"),
    bigquery.SchemaField("high", "FLOAT64"),
    bigquery.SchemaField("low", "FLOAT64"),
    bigquery.SchemaField("close", "FLOAT64"),
    bigquery.SchemaField("adjusted_close", "FLOAT64"),
    bigquery.SchemaField("volume", "INT64"),
]

SYMBOLS_SCHEMA = [
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("name", "STRING"),
    bigquery.SchemaField("exchange", "STRING"),
    bigquery.SchemaField("type", "STRING"),
    bigquery.SchemaField("isin", "STRING"),
    bigquery.SchemaField("delisted", "BOOL"),
    bigquery.SchemaField("as_of", "DATE"),
]

SESSION = requests.Session()

# Instrument types worth keeping. Funds/preferreds/warrants/units are noise
# for an equity stat-arb universe; ETFs stay for hedging and sector features.
KEEP_TYPES = {"Common Stock", "ETF"}

_symbol_exchange_map: dict[str, str] | None = None


def symbol_exchange_map() -> dict[str, str]:
    """symbol → exchange for listed Common Stock/ETF, active AND delisted.

    The bulk EOD endpoint reports every venue as 'US', so venue filtering has
    to happen via the symbol lists. Including delisted names keeps the
    historical cross-section survivorship-bias-free.
    """
    global _symbol_exchange_map
    if _symbol_exchange_map is not None:
        return _symbol_exchange_map
    m: dict[str, str] = {}
    for delisted in (0, 1):
        url = (f"https://eodhd.com/api/exchange-symbol-list/US"
               f"?delisted={delisted}&api_token={EODHD_TOKEN}&fmt=csv")
        r = SESSION.get(url, timeout=120)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        df = df[df["Exchange"].isin(LISTED_EXCHANGES)]
        # Delisted rows sometimes lack Type; keep them if the venue matches.
        df = df[df["Type"].isin(KEEP_TYPES) | df["Type"].isna()]
        for code, ex in zip(df["Code"], df["Exchange"]):
            m.setdefault(str(code), ex)
    _symbol_exchange_map = m
    print(f"symbol/exchange map: {len(m):,} listed symbols (incl. delisted)")
    return m


def fetch_bulk_day(date_str: str, retries: int = 3) -> pd.DataFrame | None:
    """Fetch the whole US market for one date. Returns None on holidays."""
    url = (
        f"https://eodhd.com/api/eod-bulk-last-day/US"
        f"?date={date_str}&api_token={EODHD_TOKEN}&fmt=csv"
    )
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=120)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:100]}")
            if len(r.text) < 200:  # holiday / no data
                return None
            df = pd.read_csv(io.StringIO(r.text))
            break
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)

    df = df.rename(columns={"Code": "symbol", "Date": "date", "Open": "open",
                            "High": "high", "Low": "low", "Close": "close",
                            "Adjusted_close": "adjusted_close", "Volume": "volume"})
    exmap = symbol_exchange_map()
    df["symbol"] = df["symbol"].astype(str)
    df = df[df["symbol"].isin(exmap)]
    df["exchange"] = df["symbol"].map(exmap)
    keep = ["date", "symbol", "exchange", "open", "high", "low", "close",
            "adjusted_close", "volume"]
    df = df[keep]
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    for c in ["open", "high", "low", "close", "adjusted_close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close", "adjusted_close"])
    return df


def fetch_symbol_history(symbol: str, exchange: str, start: str,
                         retries: int = 3) -> pd.DataFrame | None:
    """Full history for one symbol — costs exactly 1 API quota unit.

    Returns None for symbols with no data (some delisted shells).
    Blocks through daily-quota exhaustion (HTTP 402) in 10-min sleeps so a
    backfill launched before the midnight-UTC reset starts by itself.
    """
    url = (f"https://eodhd.com/api/eod/{symbol}.US"
           f"?from={start}&api_token={EODHD_TOKEN}&fmt=csv")
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=60)
            if r.status_code == 404:
                return None
            if r.status_code == 402:  # daily quota — wait for the reset
                time.sleep(600)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:80]}")
            if len(r.text) < 60:
                return None
            df = pd.read_csv(io.StringIO(r.text))
            break
        except RuntimeError:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f"{symbol}: quota never reset")

    df = df.rename(columns={"Date": "date", "Open": "open", "High": "high",
                            "Low": "low", "Close": "close",
                            "Adjusted_close": "adjusted_close",
                            "Volume": "volume"})
    if "date" not in df.columns or df.empty:
        return None
    df["symbol"] = symbol
    df["exchange"] = exchange
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    for c in ["open", "high", "low", "close", "adjusted_close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "close", "adjusted_close"])
    return df[["date", "symbol", "exchange", "open", "high", "low", "close",
               "adjusted_close", "volume"]]


def backfill_symbols(start: str, workers: int = 12, truncate: bool = False,
                     flush_rows: int = 2_000_000):
    """Full-history backfill via per-symbol calls (~30k quota units total)."""
    from quant.data.bq import client, query

    ensure_table(T_EOD, EOD_SCHEMA, partition_field="date", clustering=["symbol"])
    if truncate:
        client().query(f"TRUNCATE TABLE `{T_EOD}`").result()
        print("truncated eod_bars")

    exmap = symbol_exchange_map()
    done = set()
    try:
        done = set(query(f"SELECT DISTINCT symbol FROM `{T_EOD}`")["symbol"])
    except Exception:
        pass
    todo = sorted(s for s in exmap if s not in done)
    print(f"{len(todo):,} symbols to fetch ({len(done):,} already loaded)")

    buf: list[pd.DataFrame] = []
    buf_rows = total = fetched = empty = 0
    t0 = time.time()

    def flush():
        nonlocal buf, buf_rows, total
        if not buf:
            return
        chunk = pd.concat(buf, ignore_index=True)
        load_df(T_EOD, chunk, schema=EOD_SCHEMA)
        total += len(chunk)
        buf, buf_rows = [], 0
        rate = fetched / max(time.time() - t0, 1) * 60
        print(f"loaded {total:,} rows | {fetched:,}/{len(todo):,} symbols "
              f"({empty:,} empty) | {rate:.0f} sym/min", flush=True)

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_symbol_history, s, exmap[s], start): s
                for s in todo}
        for fut in cf.as_completed(futs):
            fetched += 1
            df = fut.result()
            if df is None or df.empty:
                empty += 1
                continue
            buf.append(df)
            buf_rows += len(df)
            if buf_rows >= flush_rows:
                flush()
    flush()
    print(f"DONE. {total:,} rows from {fetched:,} symbols ({empty:,} empty).")


def backfill(start: str, end: str | None = None, workers: int = 8):
    ensure_table(T_EOD, EOD_SCHEMA, partition_field="date", clustering=["symbol"])
    os.makedirs(STAGING_DIR, exist_ok=True)

    end_d = dt.date.fromisoformat(end) if end else dt.date.today() - dt.timedelta(days=1)
    start_d = dt.date.fromisoformat(start)

    # Resume: skip anything already in BQ.
    max_loaded = scalar(f"SELECT MAX(date) FROM `{T_EOD}`")
    if max_loaded:
        start_d = max(start_d, max_loaded + dt.timedelta(days=1))
        print(f"Resuming after {max_loaded}")
    if start_d > end_d:
        print("Nothing to do.")
        return

    days = [d for d in pd.date_range(start_d, end_d) if d.weekday() < 5]
    print(f"Backfilling {len(days)} weekdays {start_d} → {end_d}")

    # Process in quarter-sized chunks: fetch parallel, load each chunk to BQ.
    chunks: dict[str, list] = {}
    for d in days:
        chunks.setdefault(f"{d.year}Q{(d.month - 1) // 3 + 1}", []).append(d)

    total_rows = 0
    for chunk_key in sorted(chunks):
        chunk_days = chunks[chunk_key]
        frames = []
        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_bulk_day, d.strftime("%Y-%m-%d")): d for d in chunk_days}
            for fut in cf.as_completed(futs):
                df = fut.result()
                if df is not None and len(df):
                    frames.append(df)
        if not frames:
            print(f"{chunk_key}: no data (all holidays?)")
            continue
        chunk_df = pd.concat(frames, ignore_index=True)
        load_df(T_EOD, chunk_df, schema=EOD_SCHEMA)
        total_rows += len(chunk_df)
        print(f"{chunk_key}: {len(frames)} days, {len(chunk_df):,} rows "
              f"({time.time() - t0:.0f}s) — total {total_rows:,}", flush=True)
    print(f"DONE. Loaded {total_rows:,} rows.")


def update(date_str: str | None = None):
    """Incremental single-day load (idempotent via delete+insert)."""
    from quant.data.bq import client

    d = date_str or (dt.date.today() - dt.timedelta(days=1)).isoformat()
    df = fetch_bulk_day(d)
    if df is None or not len(df):
        print(f"{d}: no data (holiday/weekend)")
        return
    client().query(f"DELETE FROM `{T_EOD}` WHERE date = '{d}'").result()
    load_df(T_EOD, df, schema=EOD_SCHEMA)
    print(f"{d}: loaded {len(df):,} rows")


def load_symbols():
    """Symbols dimension: active + delisted, replaces the table."""
    frames = []
    for delisted in (0, 1):
        url = (f"https://eodhd.com/api/exchange-symbol-list/US"
               f"?delisted={delisted}&api_token={EODHD_TOKEN}&fmt=csv")
        r = SESSION.get(url, timeout=120)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = [c.lower() for c in df.columns]
        df = df.rename(columns={"code": "symbol"})
        df["delisted"] = bool(delisted)
        frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    out = out[["symbol", "name", "exchange", "type", "isin", "delisted"]]
    out["symbol"] = out["symbol"].astype(str)
    for c in ["name", "exchange", "type", "isin"]:
        out[c] = out[c].astype(str).replace("nan", None)
    out["as_of"] = dt.date.today()
    ensure_table(T_SYMBOLS, SYMBOLS_SCHEMA)
    load_df(T_SYMBOLS, out, schema=SYMBOLS_SCHEMA, write="WRITE_TRUNCATE")
    print(f"symbols: {len(out):,} rows ({out['delisted'].sum():,} delisted)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--backfill-symbols", metavar="START_DATE",
                   help="per-symbol full-history backfill (1 quota unit/symbol)")
    p.add_argument("--truncate", action="store_true",
                   help="wipe eod_bars before --backfill-symbols")
    p.add_argument("--backfill", metavar="START_DATE",
                   help="bulk-per-day backfill (100 quota units/day!)")
    p.add_argument("--end")
    p.add_argument("--update", action="store_true")
    p.add_argument("--date")
    p.add_argument("--symbols", action="store_true")
    p.add_argument("--workers", type=int, default=12)
    args = p.parse_args()
    if args.symbols:
        load_symbols()
    if args.backfill_symbols:
        backfill_symbols(args.backfill_symbols, args.workers, args.truncate)
    elif args.backfill:
        backfill(args.backfill, args.end, args.workers)
    elif args.update:
        update(args.date)
    if not (args.symbols or args.backfill or args.backfill_symbols or args.update):
        p.print_help()
        sys.exit(1)
