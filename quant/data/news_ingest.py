"""Alpaca (Benzinga) news → BigQuery.

    python3 -m quant.data.news_ingest --backfill 2016-01-01
    python3 -m quant.data.news_ingest --update            # since last stored item

Benzinga history reaches back to ~2016 and is the same feed exposed by the
real-time news websocket, so backtests and live trading see one distribution.
Full-feed pagination at 50 items/request under the 200 req/min limit means a
complete backfill takes hours — run it in the background and let the resume
logic (max created_at in BQ) pick up where it left off.
"""

import argparse
import datetime as dt
import sys
import time

import pandas as pd
import requests
from google.cloud import bigquery

from quant.config import ALPACA_KEY_PAPER, ALPACA_SECRET_PAPER, T_NEWS
from quant.data.bq import ensure_table, load_df, scalar

NEWS_SCHEMA = [
    bigquery.SchemaField("id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("created_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
    bigquery.SchemaField("headline", "STRING"),
    bigquery.SchemaField("summary", "STRING"),
    bigquery.SchemaField("author", "STRING"),
    bigquery.SchemaField("source", "STRING"),
    bigquery.SchemaField("symbols", "STRING", mode="REPEATED"),
    bigquery.SchemaField("url", "STRING"),
]

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY_PAPER,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_PAPER,
}
URL = "https://data.alpaca.markets/v1beta1/news"


def _to_df(items: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame([{
        "id": it["id"],
        "created_at": it["created_at"],
        "updated_at": it.get("updated_at"),
        "headline": it.get("headline"),
        "summary": it.get("summary"),
        "author": it.get("author"),
        "source": it.get("source"),
        "symbols": it.get("symbols") or [],
        "url": it.get("url"),
    } for it in items])
    for c in ("created_at", "updated_at"):
        df[c] = pd.to_datetime(df[c], utc=True, format="ISO8601")
    return df


def backfill(start: str, end: str | None = None, flush_every: int = 20_000):
    ensure_table(T_NEWS, NEWS_SCHEMA, partition_field="created_at",
                 clustering=["source"])

    # Resume from the newest stored item.
    max_ts = scalar(f"SELECT MAX(created_at) FROM `{T_NEWS}`")
    if max_ts is not None:
        start_ts = (max_ts + dt.timedelta(seconds=1)).isoformat()
        print(f"Resuming after {max_ts}")
    else:
        start_ts = f"{start}T00:00:00Z"
    end_ts = f"{end}T00:00:00Z" if end else dt.datetime.now(dt.timezone.utc).isoformat()

    params = {"start": start_ts, "end": end_ts, "limit": 50, "sort": "asc",
              "include_content": "false"}
    buf: list[dict] = []
    total = 0
    while True:
        r = requests.get(URL, headers=HEADERS, params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(10)
            continue
        r.raise_for_status()
        j = r.json()
        items = j.get("news") or []
        buf.extend(items)
        token = j.get("next_page_token")
        if len(buf) >= flush_every or (not token and buf):
            df = _to_df(buf)
            load_df(T_NEWS, df, schema=NEWS_SCHEMA)
            total += len(buf)
            print(f"loaded {total:,} items, through {df['created_at'].max()}",
                  flush=True)
            buf = []
        if not token:
            break
        params["page_token"] = token
        time.sleep(0.31)  # ~190 req/min, under the 200/min limit
    print(f"DONE. {total:,} news items.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--backfill", metavar="START_DATE")
    p.add_argument("--end")
    p.add_argument("--update", action="store_true")
    args = p.parse_args()
    if args.backfill:
        backfill(args.backfill, args.end)
    elif args.update:
        backfill("2016-01-01")  # resume logic makes this incremental
    else:
        p.print_help()
        sys.exit(1)
