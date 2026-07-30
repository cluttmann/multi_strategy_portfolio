"""SEC-Merger-Filings (425/DEFM14A/SC TO-T/SC TO-I/SC 14D9) → BigQuery.

    python3 -m quant.data.merger_ingest --pilot     # 2019-2024, ohne BQ-Write
    python3 -m quant.data.merger_ingest --backfill  # 2007-heute → BQ

Zwei Tabellen: `quant.merger_filings` (roher Index, ein Eintrag pro Filing —
genau das Muster von quant/data/sec_13d_ingest.py) und `quant.merger_deals`
(extrahierte Deal-Terms, nur für die früheste 425/SC-TO-Filing je CIK — die
kündigt den Deal typischerweise mit dem Angebotspreis im Fließtext an).

ACHTUNG (Lektion aus sec_13d_ingest.py): die SEC hat Formularlabels schon
einmal umgestellt (SC 13D → SCHEDULE 13D, Dez. 2024) und ein reiner
Alt-Label-Filter lief danach monatelang grün mit 0 Treffern. `pilot()` prüft
deshalb explizit Jahr-für-Jahr-Zählungen bis zum aktuellen Jahr — bricht die
Zählung 2025/2026 auf 0 ein, ist das der Verdacht auf ein Label-Update.
"""

import argparse
import io
import re
import sys
import time
import zipfile

import pandas as pd
import requests
from google.cloud import bigquery

from quant.config import BQ_DATASET, GCP_PROJECT
from quant.data.bq import ensure_table, load_df, query

T_FILINGS = f"{GCP_PROJECT}.{BQ_DATASET}.merger_filings"
T_DEALS = f"{GCP_PROJECT}.{BQ_DATASET}.merger_deals"
UA = {"User-Agent": "Carl Johannes carl.johannes.mail@gmail.com"}

FORM_RE = re.compile(
    r"^(425|DEFM14A|SC 14D9(?:/A)?|SC TO-T(?:/A)?|SC TO-I(?:/A)?)\s")
# Diese vier Formulare zeigen den frühesten, deal-spezifischen Preis im
# Fließtext an — DEFM14A kommt meist Wochen später mit demselben Preis.
ANNOUNCE_FORMS = {"425", "SC TO-T", "SC TO-I"}

FILINGS_SCHEMA = [
    bigquery.SchemaField("date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("cik", "INT64"),
    bigquery.SchemaField("form", "STRING"),
    bigquery.SchemaField("accession", "STRING"),
    bigquery.SchemaField("company", "STRING"),
]

DEALS_SCHEMA = [
    bigquery.SchemaField("announce_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("cik", "INT64"),
    bigquery.SchemaField("source_form", "STRING"),
    bigquery.SchemaField("source_accession", "STRING"),
    bigquery.SchemaField("company", "STRING"),
    bigquery.SchemaField("consideration_type", "STRING"),
    bigquery.SchemaField("deal_price_cash", "FLOAT64"),
]

CASH_RE = re.compile(
    r"\$\s?(\d{1,4}(?:\.\d{2})?)\s+per\s+share\s+in\s+cash", re.IGNORECASE)
STOCK_HINT_RE = re.compile(
    r"shares\s+of\s+.{0,40}common\s+stock", re.IGNORECASE)
# ACHTUNG: literal "shares" (Plural), NICHT "shares?" — die optionale
# Pluralisierung matcht sonst Boilerplate wie "each SHARE of Common Stock
# will be converted..." (die Beschreibung der gewandelten Aktie selbst, kein
# Stock-Konsiderations-Signal) und stuft praktisch jeden reinen Cash-Deal
# fälschlich als "mixed" ein. Gefunden beim ersten Testlauf (Step 1/Step 4)
# von merger_ingest.py: der cash_text-Testfall aus dem Brief schlug mit der
# ursprünglichen "shares?"-Regex fehl (classify_consideration -> "mixed"
# statt "cash"), weil er "Each share of Common Stock will be converted..."
# enthält.


def classify_consideration(text: str) -> str:
    """cash | stock | mixed | unknown — reine Textklassifikation, kein
    Netzwerkzugriff, damit sie ohne SEC-Fetch testbar ist."""
    has_cash = bool(CASH_RE.search(text))
    has_stock = bool(STOCK_HINT_RE.search(text))
    if has_cash and has_stock:
        return "mixed"
    if has_cash:
        return "cash"
    if has_stock:
        return "stock"
    return "unknown"


def extract_cash_price(text: str) -> float | None:
    m = CASH_RE.search(text)
    return float(m.group(1)) if m else None


def cik_to_ticker() -> dict[int, str]:
    r = requests.get("https://www.sec.gov/files/company_tickers.json",
                     headers=UA, timeout=60)
    r.raise_for_status()
    out: dict[int, str] = {}
    for v in r.json().values():
        cik, t = int(v["cik_str"]), str(v["ticker"]).upper()
        out.setdefault(cik, t)
    return out


def quarter_filings(year: int, qtr: int) -> pd.DataFrame:
    """Merger-Filing-Zeilen eines Quartals aus dem komprimierten Formularindex."""
    url = (f"https://www.sec.gov/Archives/edgar/full-index/"
           f"{year}/QTR{qtr}/form.zip")
    for attempt in range(4):
        try:
            r = requests.get(url, headers=UA, timeout=180)
            if r.status_code == 404:
                return pd.DataFrame()
            r.raise_for_status()
            break
        except Exception:  # noqa: BLE001
            if attempt == 3:
                return pd.DataFrame()
            time.sleep(3 * (attempt + 1))
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith("form.idx"))
        txt = z.read(name).decode("latin-1")
    rows = []
    for line in txt.split("\n"):
        if not FORM_RE.match(line):
            continue
        # Formularfeld ist entweder ein Token ("425", "DEFM14A") oder
        # "SC "+ein weiteres Token ("SC 14D9", "SC 14D9/A", "SC TO-T/A", ...)
        m = re.match(r"^((?:SC\s+\S+|425|DEFM14A))\s+(.+?)\s+(\d{4,10})\s+"
                     r"(\d{4}-\d{2}-\d{2})\s+(\S+)\s*$", line)
        if not m:
            continue
        form, company, cik, date, path = m.groups()
        acc = path.rsplit("/", 1)[-1].replace(".txt", "")
        rows.append({"form": form, "company": company.strip(),
                     "cik": int(cik), "date": date, "accession": acc,
                     "path": path})
    return pd.DataFrame(rows)


def collect(start_year=2007, end_year=None, end_qtr=None) -> pd.DataFrame:
    import datetime as dt
    today = dt.date.today()
    end_year = end_year or today.year
    end_qtr = end_qtr or (today.month - 1) // 3 + 1
    tmap = cik_to_ticker()
    print(f"CIK→Ticker: {len(tmap):,} Einträge")
    out = []
    for y in range(start_year, end_year + 1):
        for q in range(1, 5):
            if y == end_year and q > end_qtr:
                break
            df = quarter_filings(y, q)
            if df.empty:
                continue
            df["symbol"] = df["cik"].map(tmap)
            hit = df.dropna(subset=["symbol"]).drop_duplicates(
                ["accession", "symbol"])
            out.append(hit)
            print(f"  {y}Q{q}: {len(df):5,} Merger-Zeilen → {len(hit):4,} mit "
                  f"Ticker", flush=True)
            time.sleep(0.15)
    if not out:
        return pd.DataFrame()
    all_df = pd.concat(out, ignore_index=True)
    all_df["date"] = pd.to_datetime(all_df["date"]).dt.date
    return all_df[["date", "symbol", "cik", "form", "accession", "company",
                   "path"]]


def fetch_filing_text(cik: int, path: str) -> str:
    url = f"https://www.sec.gov/Archives/{path}"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=60)
            r.raise_for_status()
            return r.text
        except Exception:  # noqa: BLE001
            if attempt == 2:
                return ""
            time.sleep(2 * (attempt + 1))
    return ""


def build_deals(filings: pd.DataFrame) -> pd.DataFrame:
    """Für jede (CIK, Symbol)-Gruppe die früheste ANNOUNCE_FORMS-Zeile nehmen,
    den Volltext holen, Konsideration + Cash-Preis extrahieren. Ein Fetch pro
    Deal, nicht pro Filing — SEC-Fair-Use, siehe UA-Kommentar oben."""
    cand = filings[filings["form"].isin(ANNOUNCE_FORMS)].copy()
    if cand.empty:
        return pd.DataFrame(columns=[c.name for c in DEALS_SCHEMA])
    cand = cand.sort_values("date").drop_duplicates(["cik", "symbol"],
                                                     keep="first")
    rows = []
    for _, r in cand.iterrows():
        text = fetch_filing_text(int(r["cik"]), r["path"])
        if not text:
            continue
        cons = classify_consideration(text)
        price = extract_cash_price(text) if cons in ("cash", "mixed") else None
        rows.append({"announce_date": r["date"], "symbol": r["symbol"],
                     "cik": int(r["cik"]), "source_form": r["form"],
                     "source_accession": r["accession"],
                     "company": r["company"], "consideration_type": cons,
                     "deal_price_cash": price})
        time.sleep(0.15)
    return pd.DataFrame(rows)


def backfill(start_year=2007):
    filings = collect(start_year)
    print(f"\n{len(filings):,} Merger-Filings, "
          f"{filings['symbol'].nunique():,} Symbole")
    ensure_table(T_FILINGS, FILINGS_SCHEMA, partition_field="date",
                clustering=["symbol"])
    load_df(T_FILINGS, filings[["date", "symbol", "cik", "form", "accession",
                                "company"]],
           schema=FILINGS_SCHEMA, write="WRITE_TRUNCATE")
    print(f"→ {T_FILINGS}")
    deals = build_deals(filings)
    print(f"{len(deals):,} Deals extrahiert "
          f"({(deals['consideration_type'] == 'cash').sum() if len(deals) else 0} "
          "Cash-Deals)")
    ensure_table(T_DEALS, DEALS_SCHEMA, partition_field="announce_date",
                clustering=["symbol"])
    load_df(T_DEALS, deals, schema=DEALS_SCHEMA, write="WRITE_TRUNCATE")
    print(f"→ {T_DEALS}")


def refresh():
    """Nur das laufende (und vorige) Quartal — für den Tagesloop. Mirrors
    sec_13d_ingest.refresh()."""
    import datetime as dt
    today = dt.date.today()
    q = (today.month - 1) // 3 + 1
    fenster = [(today.year, q)]
    fenster.append((today.year - 1, 4) if q == 1 else (today.year, q - 1))
    tmap = cik_to_ticker()
    frames = []
    for y, qq in fenster:
        df = quarter_filings(y, qq)
        if df.empty:
            continue
        df["symbol"] = df["cik"].map(tmap)
        frames.append(df.dropna(subset=["symbol"]).drop_duplicates(
            ["accession", "symbol"]))
    if not frames:
        print("Merger-Refresh: keine Zeilen")
        return
    new = pd.concat(frames, ignore_index=True)
    new["date"] = pd.to_datetime(new["date"]).dt.date
    ensure_table(T_FILINGS, FILINGS_SCHEMA, partition_field="date",
                clustering=["symbol"])
    try:
        have = query(f"SELECT DISTINCT accession, symbol FROM `{T_FILINGS}` "
                     f"WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 200 DAY)")
        seen = set(zip(have["accession"], have["symbol"]))
        new = new[~new.apply(lambda r: (r["accession"], r["symbol"]) in seen,
                             axis=1)]
    except Exception:  # noqa: BLE001
        pass
    if new.empty:
        print("Merger-Refresh: nichts Neues")
        return
    load_df(T_FILINGS, new[["date", "symbol", "cik", "form", "accession",
                           "company"]], schema=FILINGS_SCHEMA)
    deals = build_deals(new)
    if len(deals):
        ensure_table(T_DEALS, DEALS_SCHEMA, partition_field="announce_date",
                    clustering=["symbol"])
        load_df(T_DEALS, deals, schema=DEALS_SCHEMA)
    print(f"Merger-Refresh: {len(new):,} neue Filings, {len(deals):,} neue Deals")


def pilot():
    df = collect(2019)
    print(f"\nPILOT: {len(df):,} Filings total, "
          f"{df['symbol'].nunique():,} Symbole")
    yr = pd.to_datetime(df["date"]).dt.year.value_counts().sort_index()
    print("Filings pro Jahr: " + "  ".join(f"{k}:{v}" for k, v in yr.items()))
    if yr.get(2025, 0) == 0 or yr.get(2026, 0) == 0:
        print("⚠ 2025 oder 2026 hat 0 Treffer — Verdacht auf SEC-Label-Update, "
              "wie bei sec_13d_ingest.py (SC 13D → SCHEDULE 13D). FORM_RE prüfen.")
    deals = build_deals(df)
    print(f"{len(deals):,} Deals extrahiert, davon "
          f"{(deals['consideration_type'] == 'cash').sum() if len(deals) else 0} "
          "Cash-Deals")
    out = "quant/research/_mergarb_pilot.parquet"
    deals.to_parquet(out)
    print(f"→ {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--pilot", action="store_true")
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--refresh", action="store_true")
    a = p.parse_args()
    if a.pilot:
        pilot()
    elif a.refresh:
        refresh()
    elif a.backfill:
        backfill()
    else:
        p.print_help()
        sys.exit(1)
