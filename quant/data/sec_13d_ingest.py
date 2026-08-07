"""SEC Schedule 13D (Aktivisten-Beteiligungen > 5 %) → BigQuery.

    python3 -m quant.data.sec_13d_ingest --pilot     # 2019-2024, ohne BQ-Write
    python3 -m quant.data.sec_13d_ingest --backfill  # 2007-heute → BQ

TRICK statt 15.000 Header-Requests: In `form.idx` steht jedes 13D zweimal —
einmal unter dem FILER (Aktivist) und einmal unter der SUBJECT COMPANY
(Zielunternehmen). Der Aktivist ist fast immer eine LLC/LP/Privatperson ohne
Börsenticker, das Ziel hat einen. Ein Join gegen SECs `company_tickers.json`
trennt beide ohne einen einzigen Zusatz-Request. Ausnahme: börsennotierte
Aktivisten-Holdings (Icahn/IEP, Loeb, Biglari) → Ausschlussliste unten.
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

T_13D = f"{GCP_PROJECT}.{BQ_DATASET}.sec_13d_filings"
UA = {"User-Agent": "Carl Johannes carl.johannes.mail@gmail.com"}

# Die SEC hat mit der XML-Pflicht (Dez. 2024) das Formularlabel von "SC 13D"
# auf "SCHEDULE 13D" umgestellt. Wer nur auf das alte Label filtert, sieht ab
# 2025 NULL Events und merkt es nicht — der Ingester läuft grün weiter.
# Gefunden 2026-07-25 beim Pilot (2025/2026 hatten 0 initiale 13D).
FORM_RE = re.compile(r"^(?:SC|SCHEDULE) 13D(?:/A)?\s")

SCHEMA = [
    bigquery.SchemaField("date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("cik", "INT64"),
    bigquery.SchemaField("form", "STRING"),      # SC 13D | SC 13D/A
    bigquery.SchemaField("accession", "STRING"),
    bigquery.SchemaField("company", "STRING"),
]

# Börsennotierte Aktivisten — hier ist der Ticker der FILER, nicht das Ziel
FILER_TICKERS = {"IEP", "BH", "BHVN", "LUKE", "SWK", "GBL", "BRK.A", "BRK.B",
                 "LAZ", "APO", "BX", "KKR", "CG", "ARES", "OWL"}


def cik_to_ticker() -> dict[int, str]:
    r = requests.get("https://www.sec.gov/files/company_tickers.json",
                     headers=UA, timeout=60)
    r.raise_for_status()
    out: dict[int, str] = {}
    for v in r.json().values():
        cik, t = int(v["cik_str"]), str(v["ticker"]).upper()
        if t in FILER_TICKERS:
            continue
        out.setdefault(cik, t)      # erster Eintrag = Primärticker
    return out


def quarter_filings(year: int, qtr: int) -> pd.DataFrame:
    """SC-13D-Zeilen eines Quartals aus dem komprimierten Formularindex."""
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
        # Fixed-width, aber Firmennamen enthalten Leerzeichen → per Regex
        m = re.match(r"^((?:SC|SCHEDULE) 13D(?:/A)?)\s+(.+?)\s+(\d{4,10})\s+"
                     r"(\d{4}-\d{2}-\d{2})\s+(\S+)\s*$", line)
        if not m:
            continue
        form, company, cik, date, path = m.groups()
        acc = path.rsplit("/", 1)[-1].replace(".txt", "")
        # auf das kanonische Label normalisieren, damit alte und neue
        # Filings in einer Tabelle vergleichbar bleiben
        rows.append({"form": form.replace("SCHEDULE", "SC"),
                     "company": company.strip(),
                     "cik": int(cik), "date": date, "accession": acc})
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
            hit = df.dropna(subset=["symbol"])
            # eine Zeile je (Accession, Symbol) — Aktivisten-Zeilen fallen weg
            hit = hit.drop_duplicates(["accession", "symbol"])
            out.append(hit)
            print(f"  {y}Q{q}: {len(df):5,} 13D-Zeilen → {len(hit):4,} mit "
                  f"Ticker ({hit['form'].eq('SC 13D').sum():4,} initial)",
                  flush=True)
            time.sleep(0.15)          # SEC-Fair-Use
    if not out:
        return pd.DataFrame()
    all_df = pd.concat(out, ignore_index=True)
    all_df["date"] = pd.to_datetime(all_df["date"]).dt.date
    return all_df[["date", "symbol", "cik", "form", "accession", "company"]]


def backfill(start_year=2007):
    df = collect(start_year)
    print(f"\n{len(df):,} 13D-Filings, {df['symbol'].nunique():,} Symbole, "
          f"{df['date'].min()} → {df['date'].max()}")
    ensure_table(T_13D, SCHEMA, partition_field="date", clustering=["symbol"])
    load_df(T_13D, df, schema=SCHEMA, write="WRITE_TRUNCATE")
    print(f"→ {T_13D}")


def refresh():
    """Nur das laufende (und vorige) Quartal nachladen — für den Tagesloop.

    Ein Voll-Backfill lädt 79 form.zip-Archive; täglich wäre das Verschwendung
    und SEC-unfreundlich. Zwei Quartale genügen, weil 13D binnen 5 Werktagen
    einzureichen sind. Dedupliziert gegen die vorhandenen Accessions.
    """
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
        frames.append(df.dropna(subset=["symbol"])
                        .drop_duplicates(["accession", "symbol"]))
    if not frames:
        print("13D-Refresh: keine Zeilen")
        return
    new = pd.concat(frames, ignore_index=True)
    new["date"] = pd.to_datetime(new["date"]).dt.date
    ensure_table(T_13D, SCHEMA, partition_field="date", clustering=["symbol"])
    try:
        have = query(f"SELECT DISTINCT accession, symbol FROM `{T_13D}` "
                     f"WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 200 DAY)")
        seen = set(zip(have["accession"], have["symbol"]))
        new = new[~new.apply(lambda r: (r["accession"], r["symbol"]) in seen,
                             axis=1)]
    except Exception:  # noqa: BLE001
        pass
    if new.empty:
        print("13D-Refresh: nichts Neues")
        return
    load_df(T_13D, new[["date", "symbol", "cik", "form", "accession",
                        "company"]], schema=SCHEMA)
    print(f"13D-Refresh: {len(new):,} neue Zeilen "
          f"({new['form'].eq('SC 13D').sum()} initial)")


def pilot():
    df = collect(2019)
    init = df[df["form"] == "SC 13D"]
    print(f"\nPILOT: {len(df):,} Filings total, {len(init):,} initiale 13D, "
          f"{init['symbol'].nunique():,} Symbole")
    yr = pd.to_datetime(init["date"]).dt.year.value_counts().sort_index()
    print("Initiale 13D pro Jahr: " + "  ".join(f"{k}:{v}" for k, v in yr.items()))
    out = "quant/research/_13d_pilot.parquet"
    init.to_parquet(out)
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
