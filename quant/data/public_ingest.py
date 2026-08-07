"""Freie öffentliche Datenquellen → BigQuery (DATA_ROADMAP #1, #2, #8, F&G).

    python3 -m quant.data.public_ingest --borrow-snap    # IBKR + Alpaca (Cron!)
    python3 -m quant.data.public_ingest --fred           # ~15 Regime-Serien
    python3 -m quant.data.public_ingest --finra          # Short-Volume 2009+
    python3 -m quant.data.public_ingest --fng            # Fear&Greed komplett
"""

import argparse
import datetime as dt
import io
import sys
import time

import pandas as pd
import requests
from google.cloud import bigquery

from quant.config import (ALPACA_KEY_PAPER, ALPACA_PAPER_BASE,
                          ALPACA_SECRET_PAPER, BQ_DATASET, FRED_KEY,
                          GCP_PROJECT)
from quant.data.bq import ensure_table, load_df, scalar

T_BORROW = f"{GCP_PROJECT}.{BQ_DATASET}.borrow_snapshots"
T_FRED = f"{GCP_PROJECT}.{BQ_DATASET}.fred_series"
T_FINRA = f"{GCP_PROJECT}.{BQ_DATASET}.finra_short_volume"
T_FNG = f"{GCP_PROJECT}.{BQ_DATASET}.crypto_fear_greed"

AH = {"APCA-API-KEY-ID": ALPACA_KEY_PAPER,
      "APCA-API-SECRET-KEY": ALPACA_SECRET_PAPER}

# --- #8 Borrow-Snapshots (Historie nur durch Sammeln!) -----------------------
BORROW_SCHEMA = [
    bigquery.SchemaField("snap_ts", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("source", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("fee_rate", "FLOAT64"),
    bigquery.SchemaField("available", "INT64"),
    bigquery.SchemaField("borrow_status", "STRING"),
    bigquery.SchemaField("shortable", "BOOL"),
    bigquery.SchemaField("margin_requirement", "FLOAT64"),
]


def borrow_snapshot():
    ensure_table(T_BORROW, BORROW_SCHEMA, partition_field="snap_ts",
                 clustering=["symbol"])
    now = dt.datetime.now(dt.timezone.utc)
    frames = []
    # IBKR shortable stocks (FTP, user shortstock, kein Passwort)
    try:
        from ftplib import FTP
        buf = io.BytesIO()
        ftp = FTP("ftp3.interactivebrokers.com", timeout=60)
        ftp.login("shortstock", "")
        ftp.retrbinary("RETR usa.txt", buf.write)
        ftp.quit()
        txt = buf.getvalue().decode("latin-1")
        rows = []
        for line in txt.splitlines():
            p = line.split("|")
            if len(p) < 8 or p[0].startswith("#"):
                continue
            try:
                rows.append({
                    "snap_ts": now, "source": "ibkr", "symbol": p[0],
                    "fee_rate": float(p[5]) if p[5] not in ("", "NA") else None,
                    "available": int(p[7].replace(">", "")) if p[7] not in ("", "NA") else None,
                    "borrow_status": None, "shortable": None,
                    "margin_requirement": None,
                })
            except (ValueError, IndexError):
                continue
        if rows:
            frames.append(pd.DataFrame(rows))
        print(f"ibkr: {len(rows):,} Symbole")
    except Exception as e:  # noqa: BLE001
        print(f"ibkr FTP fehlgeschlagen: {e}")
    # Alpaca assets: borrow_status + margin requirement
    try:
        r = requests.get(f"{ALPACA_PAPER_BASE}/v2/assets",
                         params={"status": "active",
                                 "asset_class": "us_equity"},
                         headers=AH, timeout=120)
        r.raise_for_status()
        rows = [{
            "snap_ts": now, "source": "alpaca", "symbol": a["symbol"],
            "fee_rate": None, "available": None,
            "borrow_status": a.get("attributes") and
                ("borrow_status" in str(a.get("attributes")) and None) or
                a.get("borrow_status"),
            "shortable": a.get("shortable"),
            "margin_requirement": float(a["maintenance_margin_requirement"])
                if a.get("maintenance_margin_requirement") else None,
        } for a in r.json()]
        frames.append(pd.DataFrame(rows))
        print(f"alpaca: {len(rows):,} Assets")
    except Exception as e:  # noqa: BLE001
        print(f"alpaca assets fehlgeschlagen: {e}")
    if frames:
        load_df(T_BORROW, pd.concat(frames, ignore_index=True),
                schema=BORROW_SCHEMA)
        print("borrow snapshot gespeichert")


# --- #1 FRED-Tiefe ------------------------------------------------------------
FRED_SERIES = [
    "BAMLH0A0HYM2", "BAMLC0A0CM", "NFCI", "STLFSI4", "T10Y2Y", "T10Y3M",
    "DFII10", "T10YIE", "DTWEXBGS", "OVXCLS", "GVZCLS", "EVZCLS",
    "WALCL", "RRPONTSYD", "VIXCLS", "VIX3MCLS", "VXVCLS", "DGS2", "DGS10", "DFF",
]
FRED_SCHEMA = [
    bigquery.SchemaField("series", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("value", "FLOAT64"),
]


def fred_backfill():
    ensure_table(T_FRED, FRED_SCHEMA, partition_field="date",
                 clustering=["series"])
    frames = []
    for s in FRED_SERIES:
        r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                         params={"series_id": s, "api_key": FRED_KEY,
                                 "file_type": "json",
                                 "observation_start": "1999-01-01"},
                         timeout=60).json()
        obs = [{"series": s, "date": o["date"], "value": float(o["value"])}
               for o in r.get("observations", []) if o["value"] != "."]
        frames.append(pd.DataFrame(obs))
        print(f"{s}: {len(obs):,} Beobachtungen")
        time.sleep(0.5)
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"]).dt.date
    load_df(T_FRED, out, schema=FRED_SCHEMA, write="WRITE_TRUNCATE")
    print(f"fred_series: {len(out):,} Zeilen, {len(FRED_SERIES)} Serien")


# --- #2 FINRA Daily Short Sale Volume ------------------------------------------
FINRA_SCHEMA = [
    bigquery.SchemaField("date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("short_volume", "INT64"),
    bigquery.SchemaField("short_exempt_volume", "INT64"),
    bigquery.SchemaField("total_volume", "INT64"),
]


def finra_backfill(start="2009-08-03"):
    ensure_table(T_FINRA, FINRA_SCHEMA, partition_field="date",
                 clustering=["symbol"])
    max_loaded = scalar(f"SELECT MAX(date) FROM `{T_FINRA}`")
    start_d = (max_loaded + dt.timedelta(days=1)) if max_loaded \
        else dt.date.fromisoformat(start)
    end_d = dt.date.today() - dt.timedelta(days=1)
    days = [d for d in pd.date_range(start_d, end_d) if d.weekday() < 5]
    print(f"FINRA: {len(days)} Handelstage {start_d} → {end_d}")
    buf, total = [], 0
    ses = requests.Session()
    for i, d in enumerate(days):
        url = (f"https://cdn.finra.org/equity/regsho/daily/"
               f"CNMSshvol{d:%Y%m%d}.txt")
        try:
            r = ses.get(url, timeout=30)
        except requests.exceptions.ConnectionError:
            time.sleep(10)
            continue
        if r.status_code != 200:
            continue
        df = pd.read_csv(io.StringIO(r.text), sep="|")
        df = df[df["Symbol"].notna()]
        df = df.rename(columns={"Date": "date", "Symbol": "symbol",
                                "ShortVolume": "short_volume",
                                "ShortExemptVolume": "short_exempt_volume",
                                "TotalVolume": "total_volume"})
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d").dt.date
        for c in ["short_volume", "short_exempt_volume", "total_volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")
        buf.append(df[["date", "symbol", "short_volume",
                       "short_exempt_volume", "total_volume"]])
        if len(buf) >= 250:
            chunk = pd.concat(buf, ignore_index=True)
            load_df(T_FINRA, chunk, schema=FINRA_SCHEMA)
            total += len(chunk)
            print(f"  {d.date()}: total {total:,} Zeilen", flush=True)
            buf = []
        time.sleep(0.25)
    if buf:
        chunk = pd.concat(buf, ignore_index=True)
        load_df(T_FINRA, chunk, schema=FINRA_SCHEMA)
        total += len(chunk)
    print(f"FINRA fertig: {total:,} Zeilen")


# --- Fear & Greed ----------------------------------------------------------------
FNG_SCHEMA = [
    bigquery.SchemaField("date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("value", "INT64"),
    bigquery.SchemaField("classification", "STRING"),
]


def fng():
    ensure_table(T_FNG, FNG_SCHEMA)
    r = requests.get("https://api.alternative.me/fng/?limit=0",
                     timeout=60).json()
    rows = [{"date": dt.datetime.fromtimestamp(int(x["timestamp"])).date(),
             "value": int(x["value"]),
             "classification": x["value_classification"]}
            for x in r.get("data", [])]
    load_df(T_FNG, pd.DataFrame(rows), schema=FNG_SCHEMA,
            write="WRITE_TRUNCATE")
    print(f"fear&greed: {len(rows):,} Tage")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--borrow-snap", action="store_true")
    p.add_argument("--fred", action="store_true")
    p.add_argument("--finra", action="store_true")
    p.add_argument("--fng", action="store_true")
    args = p.parse_args()
    if not any(vars(args).values()):
        p.print_help()
        sys.exit(1)
    if args.borrow_snap:
        borrow_snapshot()
    if args.fred:
        fred_backfill()
    if args.fng:
        fng()
    if args.finra:
        finra_backfill()
