"""Polymarket-Makro-Archiv → BigQuery (täglicher Snapshot + Historien).

    python3 -m quant.data.polymarket_ingest --update

Kuratierter Korb equity-relevanter Märkte (Rezession, Fed, Wahlen,
Geopolitik, Zölle): zieht die volle Tageshistorie jedes Markts (CLOB,
public, resolved inklusive) und ersetzt die Tabelle idempotent. Als Teil
der täglichen Ops wächst hier ein Odds-Panel für spätere Regime-Features —
die Studie (polymarket_study.py) zeigte: Odds folgen Aktien (SPY→Odds
corr −0.3), nicht umgekehrt; Nutzen liegt daher bei Regime-/Risiko-Features,
nicht bei Alpha-Signalen.
"""

import argparse
import datetime as dt
import json
import sys

import pandas as pd
import requests
from google.cloud import bigquery

from quant.config import BQ_DATASET, GCP_PROJECT
from quant.data.bq import ensure_table, load_df

T_POLY = f"{GCP_PROJECT}.{BQ_DATASET}.polymarket_daily"

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

SEARCHES = [
    "US recession", "Fed rate", "Fed decision", "government shutdown",
    "tariff", "China Taiwan", "strikes Iran", "Powell", "CPI inflation",
    "presidential election", "debt ceiling", "AI bubble",
]
MIN_VOLUME = 100_000

SCHEMA = [
    bigquery.SchemaField("date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("market_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("question", "STRING"),
    bigquery.SchemaField("price", "FLOAT64"),
    bigquery.SchemaField("volume_usd", "FLOAT64"),
    bigquery.SchemaField("closed", "BOOL"),
    bigquery.SchemaField("fetched_at", "DATE"),
]


def discover() -> list[dict]:
    seen, out = set(), []
    for q in SEARCHES:
        try:
            s = requests.get(f"{GAMMA}/public-search",
                             params={"q": q, "limit_per_type": 10},
                             timeout=30).json()
        except Exception:  # noqa: BLE001
            continue
        for e in s.get("events") or []:
            ms = e.get("markets") or []
            if not ms and e.get("slug"):
                full = requests.get(f"{GAMMA}/events",
                                    params={"slug": e["slug"]},
                                    timeout=30).json()
                ms = full[0].get("markets", []) if full else []
            for m in ms:
                mid = m.get("id")
                vol = float(m.get("volume") or 0)
                if mid and mid not in seen and vol >= MIN_VOLUME:
                    seen.add(mid)
                    out.append(m)
    return out


def update():
    ensure_table(T_POLY, SCHEMA, partition_field="date",
                 clustering=["market_id"])
    markets = discover()
    print(f"{len(markets)} Märkte im Korb (>= ${MIN_VOLUME:,} Volumen)")
    today = dt.date.today()
    frames = []
    for m in markets:
        try:
            tok = json.loads(m["clobTokenIds"])[0]
            h = requests.get(f"{CLOB}/prices-history",
                             params={"market": tok, "interval": "max",
                                     "fidelity": 1440}, timeout=30).json()
            pts = h.get("history") or []
            if not pts:
                continue
            df = pd.DataFrame(pts)
            df["date"] = pd.to_datetime(df["t"], unit="s").dt.date
            df = df.groupby("date", as_index=False)["p"].last()
            df["market_id"] = str(m["id"])
            df["question"] = (m.get("question") or "")[:200]
            df["price"] = df["p"]
            df["volume_usd"] = float(m.get("volume") or 0)
            df["closed"] = bool(m.get("closed"))
            df["fetched_at"] = today
            frames.append(df[["date", "market_id", "question", "price",
                              "volume_usd", "closed", "fetched_at"]])
        except Exception:  # noqa: BLE001
            continue
    if not frames:
        print("keine Daten")
        return
    out = pd.concat(frames, ignore_index=True)
    load_df(T_POLY, out, schema=SCHEMA, write="WRITE_TRUNCATE")
    print(f"polymarket_daily: {len(out):,} Zeilen, "
          f"{out.market_id.nunique()} Märkte, {out.date.min()} → {out.date.max()}")


T_TRADES = f"{GCP_PROJECT}.{BQ_DATASET}.polymarket_trades"
DATA = "https://data-api.polymarket.com"

TRADES_SCHEMA = [
    bigquery.SchemaField("ts", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("market_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("condition_id", "STRING"),
    bigquery.SchemaField("wallet", "STRING"),
    bigquery.SchemaField("pseudonym", "STRING"),
    bigquery.SchemaField("side", "STRING"),
    bigquery.SchemaField("outcome", "STRING"),
    bigquery.SchemaField("price", "FLOAT64"),
    bigquery.SchemaField("size", "FLOAT64"),
    bigquery.SchemaField("tx_hash", "STRING"),
]


def collect_trades():
    """Tägliche Wallet-Fills des Makro-Korbs (API-Cap 3.000/Markt umgeht
    nur die Tiefe — inkrementell gesammelt entsteht die volle Historie)."""
    import time as _t

    ensure_table(T_TRADES, TRADES_SCHEMA, partition_field="ts",
                 clustering=["market_id", "wallet"])
    markets = discover()
    frames = []
    for m in markets:
        cid = m.get("conditionId")
        if not cid or m.get("closed"):
            continue
        try:
            r = requests.get(f"{DATA}/trades",
                             params={"market": cid, "limit": 500,
                                     "takerOnly": "false"}, timeout=30)
            if r.status_code == 429:
                _t.sleep(5)
                continue
            fills = r.json() if r.ok else []
        except Exception:  # noqa: BLE001
            continue
        if not fills:
            continue
        df = pd.DataFrame(fills)
        df["ts"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        df["market_id"] = str(m["id"])
        df = df.rename(columns={"conditionId": "condition_id",
                                "proxyWallet": "wallet",
                                "transactionHash": "tx_hash"})
        keep = ["ts", "market_id", "condition_id", "wallet", "pseudonym",
                "side", "outcome", "price", "size", "tx_hash"]
        frames.append(df[[c for c in keep if c in df.columns]])
        _t.sleep(0.1)
    if not frames:
        print("keine Fills")
        return
    out = pd.concat(frames, ignore_index=True)
    out["price"] = out["price"].astype(float)
    out["size"] = out["size"].astype(float)
    # idempotent: nur neue tx_hash+wallet-Kombis anfügen
    from quant.data.bq import client, query
    try:
        existing = query(
            f"SELECT DISTINCT tx_hash FROM `{T_TRADES}` "
            f"WHERE ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)")
        out = out[~out["tx_hash"].isin(set(existing["tx_hash"]))]
    except Exception:  # noqa: BLE001
        pass
    if len(out):
        load_df(T_TRADES, out, schema=TRADES_SCHEMA)
    print(f"polymarket_trades: +{len(out):,} neue Fills, "
          f"{out['wallet'].nunique() if len(out) else 0} Wallets")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--update", action="store_true")
    p.add_argument("--trades", action="store_true")
    args = p.parse_args()
    if args.update:
        update()
    if args.trades:
        collect_trades()
    if not (args.update or args.trades):
        p.print_help()
        sys.exit(1)
