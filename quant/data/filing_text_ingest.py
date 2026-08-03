"""SEC 10-K MD&A-Volltext → FinBERT-Tonalität → BigQuery.

    python3 -m quant.data.filing_text_ingest --pilot                 # 1 Quartal, kein BQ-Write
    python3 -m quant.data.filing_text_ingest --backfill --start-year 2015

Testet, ob unser eigenes FinBERT-Modell (main.py, bisher nur für Markt-News
verwendet) auf Einzeltitel-Ebene ein Signal trägt — mit voller
Universums-Abdeckung statt des Large-Cap-Bias von Drittanbieter-News (siehe
kill_registry.yaml, Motivation für diesen Ansatz). Jede Firma reicht ihr
10-K unabhängig von Presseabdeckung ein.

TRICK: `form.idx` liefert pro Filing einen Pfad auf die VOLLSTÄNDIGE
Submission (`{accession}.txt`, alle Dokumente inkl. Exhibits als SGML-
Blöcke aneinandergehängt). Ein Regex isoliert daraus NUR den
`<TYPE>10-K</TYPE>`-Block — der Rest (XBRL, Exhibits, Bilder) wird nie
geparst. Ein Request pro Filing statt zwei (Index-JSON + Primärdokument).

MD&A-Extraktion: Item-7-Überschrift bis Item-7A/8 — HEURISTIK, kein
strukturiertes XBRL-Tag. Filings enthalten die Item-7-Zeichenkette meist
ZWEIMAL (Inhaltsverzeichnis + echte Überschrift); wir nehmen das LETZTE
Vorkommen vor dem ersten Item-7A/8 danach. Filings ohne klar erkennbare
MD&A-Sektion (Formatvarianten, gescannte Bilder) werden übersprungen und
gezählt, nicht stillschweigend als leer gewertet.
"""

import argparse
import re
import sys
import time

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
from google.cloud import bigquery

from quant.config import BQ_DATASET, GCP_PROJECT
from quant.data.bq import ensure_table, load_df, query
from quant.data.sec_13d_ingest import cik_to_ticker

T_FILING_SENT = f"{GCP_PROJECT}.{BQ_DATASET}.filing_mdna_sentiment"
UA = {"User-Agent": "Carl Johannes carl.johannes.mail@gmail.com"}
FORM_RE_10K = re.compile(r"^10-K(?:/A)?\s")
LINE_RE_10K = re.compile(
    r"^(10-K(?:/A)?)\s+(.+?)\s+(\d{4,10})\s+(\d{4}-\d{2}-\d{2})\s+(\S+)\s*$")
REQUEST_SLEEP = 0.15  # SEC Fair-Use, identisch zu sec_13d_ingest
CHUNK_CHARS = 2000
MAX_CHUNKS = 15  # ~30k Zeichen Deckel pro Filing — Kosten-Deckel, siehe Docstring

SCHEMA = [
    bigquery.SchemaField("date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("cik", "INT64"),
    bigquery.SchemaField("accession", "STRING"),
    bigquery.SchemaField("form", "STRING"),
    bigquery.SchemaField("sentiment_avg", "FLOAT64"),
    bigquery.SchemaField("sentiment_conf_avg", "FLOAT64"),
    bigquery.SchemaField("n_chunks", "INT64"),
    bigquery.SchemaField("mdna_chars", "INT64"),
]

_finbert_pipeline = None


def _get_finbert():
    global _finbert_pipeline
    if _finbert_pipeline is None:
        import torch
        from transformers import pipeline
        device = "mps" if torch.backends.mps.is_available() else -1
        _finbert_pipeline = pipeline(
            "sentiment-analysis", model="ProsusAI/finbert",
            tokenizer="ProsusAI/finbert", device=device)
    return _finbert_pipeline


def quarter_filings_10k(year: int, qtr: int) -> pd.DataFrame:
    """10-K/10-K/A-Zeilen eines Quartals aus dem komprimierten Formularindex.

    Eigene Zeilen-Regex statt sec_13d_ingest.quarter_filings — dessen innere
    Regex ist hart auf "SC|SCHEDULE 13D" verdrahtet und matcht "10-K" nie.
    """
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
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith("form.idx"))
        txt = z.read(name).decode("latin-1")
    rows = []
    for line in txt.split("\n"):
        if not FORM_RE_10K.match(line):
            continue
        m = LINE_RE_10K.match(line)
        if not m:
            continue
        form, company, cik, date, path = m.groups()
        acc = path.rsplit("/", 1)[-1].replace(".txt", "")
        rows.append({"form": form.strip(), "company": company.strip(),
                     "cik": int(cik), "date": date, "accession": acc})
    return pd.DataFrame(rows)


def fetch_mdna_text(cik: int, accession: str) -> str | None:
    """Volle Submission holen, 10-K-DOCUMENT-Block isolieren, MD&A extrahieren."""
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}.txt"
    r = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=60)
            r.raise_for_status()
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                print(f"    fetch failed {accession}: {e}")
                return None
            time.sleep(3 * (attempt + 1))
    txt = r.text
    m = re.search(r"<DOCUMENT>\s*<TYPE>10-K.*?</DOCUMENT>", txt, re.S)
    if not m:
        return None
    soup = BeautifulSoup(m.group(0), "lxml")
    plain = soup.get_text(" ", strip=True)

    starts = [mm.start() for mm in
              re.finditer(r"Item\s+7\.?\s+Management", plain, re.I)]
    if not starts:
        return None
    start = starts[-1]
    ends = [mm.start() for mm in
            re.finditer(r"Item\s+(?:7A|8)\.?\s", plain[start:], re.I)]
    if not ends:
        return None
    end = start + ends[0]
    mdna = plain[start:end]
    return mdna if len(mdna) >= 500 else None


def score_mdna(text: str) -> tuple[float, float, int]:
    """FinBERT-Score über die ersten MAX_CHUNKS Blöcke à CHUNK_CHARS Zeichen.
    Rückgabe: (signed_avg, confidence_avg, n_chunks) — identisch zur
    Vorzeichenkonvention in main.py.score_news_sentiment.
    """
    chunks = [text[i:i + CHUNK_CHARS]
              for i in range(0, len(text), CHUNK_CHARS)][:MAX_CHUNKS]
    pipe = _get_finbert()
    out = pipe(chunks, batch_size=16, truncation=True, max_length=512)
    signed = []
    confs = []
    for o in out:
        conf = float(o["score"])
        sign = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}[o["label"]]
        signed.append(sign * conf)
        confs.append(conf)
    return float(np.mean(signed)), float(np.mean(confs)), len(chunks)


def already_loaded_accessions() -> set[str]:
    try:
        df = query(f"SELECT DISTINCT accession FROM `{T_FILING_SENT}`")
        return set(df["accession"])
    except Exception:  # noqa: BLE001
        return set()


def process_quarter(year: int, qtr: int, universe: set[str],
                     tmap: dict[int, str], seen: set[str],
                     dry_run: bool = False) -> pd.DataFrame:
    df = quarter_filings_10k(year, qtr)
    if df.empty:
        print(f"  {year}Q{qtr}: keine 10-K-Zeilen im Index")
        return pd.DataFrame()
    df["symbol"] = df["cik"].map(tmap)
    df = df.dropna(subset=["symbol"])
    df = df[df["symbol"].isin(universe)]
    df = df.drop_duplicates(["accession", "symbol"])
    df = df[~df["accession"].isin(seen)]
    print(f"  {year}Q{qtr}: {len(df):,} 10-K-Filings in unserem Universum "
          f"(nach Dedupe)")

    rows = []
    skipped_no_mdna = 0
    for _, r in df.iterrows():
        text = fetch_mdna_text(r["cik"], r["accession"])
        time.sleep(REQUEST_SLEEP)
        if text is None:
            skipped_no_mdna += 1
            continue
        sig, conf, nchunks = score_mdna(text)
        rows.append({"date": r["date"], "symbol": r["symbol"], "cik": r["cik"],
                     "accession": r["accession"], "form": r["form"],
                     "sentiment_avg": sig, "sentiment_conf_avg": conf,
                     "n_chunks": nchunks, "mdna_chars": len(text)})
    print(f"    → {len(rows):,} gescort, {skipped_no_mdna:,} ohne "
          f"erkennbare MD&A-Sektion übersprungen")
    out = pd.DataFrame(rows)
    if not out.empty and not dry_run:
        ensure_table(T_FILING_SENT, SCHEMA, partition_field="date",
                    clustering=["symbol"])
        load_df(T_FILING_SENT, out, schema=SCHEMA)
    return out


def backfill(start_year: int, end_year: int | None = None):
    import datetime as dt
    today = dt.date.today()
    end_year = end_year or today.year
    end_qtr = (today.month - 1) // 3 + 1

    print("Universum aus features_daily_v2 laden ...")
    uni = query("SELECT DISTINCT symbol FROM `trading-436516.quant.features_daily_v2`")
    universe = set(uni["symbol"])
    print(f"  {len(universe):,} Symbole im Universum")

    tmap = cik_to_ticker()
    seen = already_loaded_accessions()
    print(f"  {len(seen):,} Accessions bereits geladen (Resume)")

    total = 0
    for y in range(start_year, end_year + 1):
        for q in range(1, 5):
            if y == end_year and q > end_qtr:
                break
            out = process_quarter(y, q, universe, tmap, seen)
            seen |= set(out.get("accession", []))
            total += len(out)
    print(f"\nGesamt: {total:,} Filings gescort → {T_FILING_SENT}")


def pilot():
    print("Universum aus features_daily_v2 laden ...")
    uni = query("SELECT DISTINCT symbol FROM `trading-436516.quant.features_daily_v2`")
    universe = set(uni["symbol"])
    tmap = cik_to_ticker()
    out = process_quarter(2019, 1, universe, tmap, set(), dry_run=True)
    print(out.head(10))
    if not out.empty:
        print(f"\nsentiment_avg Verteilung:\n{out['sentiment_avg'].describe()}")
        out.to_parquet("quant/research/_filing_sentiment_pilot.parquet")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--pilot", action="store_true")
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--start-year", type=int, default=2015)
    p.add_argument("--end-year", type=int, default=None)
    a = p.parse_args()
    if a.pilot:
        pilot()
    elif a.backfill:
        backfill(a.start_year, a.end_year)
    else:
        p.print_help()
        sys.exit(1)
