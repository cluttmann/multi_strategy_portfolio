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
from collections import Counter

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
# Diese drei Formulare zeigen den frühesten, deal-spezifischen Preis im
# Fließtext an UND ihr Filing-Datum IST der Ankündigungstermin (425/SC TO-*
# werden binnen Tagen nach Vertragsunterzeichnung fällig).
ANNOUNCE_FORMS = {"425", "SC TO-T", "SC TO-I"}
# DEFM14A ist bei reinen Cash-Deals ohne Aktienkomponente das EINZIGE
# SEC-Filing (kein Registrierungspflicht -> kein 425) — bestätigt an
# Nuance/Activision/Twitter, s. Task-2-Report Fix-Runde 2. Sein Filing-
# Datum ist aber KEIN Ankündigungsdatum: DEFM14A folgt dem tatsächlichen
# Announce um 35-92 Tage (empirisch an denselben drei Fällen). Für DEFM14A-
# Kandidaten wird der Preis/Ticker trotzdem aus dem DEFM14A-Volltext
# extrahiert, aber announce_date kommt aus dem frühesten Item-1.01-8-K
# davor (find_8k_announce_date) — s. dort.
KEEP_WITHOUT_TICKER_FORMS = ANNOUNCE_FORMS | {"DEFM14A"}

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

# --- Point-in-time Ticker-Aufloesung (Fix-Runde 2026-07-30) ----------------
# cik_to_ticker() joint gegen SECs AKTUELLES company_tickers.json und kennt
# daher nur noch gelistete Ticker. Bei einer abgeschlossenen Cash-Uebernahme
# wird der Ziel-Ticker delistet und faellt aus dieser Datei komplett heraus
# (bestaetigt leer fuer Splunk/Nuance/Activision/Twitter, s. Task-2-Report,
# Finding B) — die SEC-Submissions-API liefert fuer dieselben CIKs ebenfalls
# ein leeres `tickers`-Array (auch nur aktuell), und die XBRL-Company-Concept-
# API (dei:TradingSymbol) gibt fuer alle vier 404 zurueck. Einzige Quelle, die
# tatsaechlich funktioniert: der Ticker steht im Filing-Cover selbst — entweder
# als SEC-Cover-Tabellenzelle ("Trading Symbol" | <TICKER> | Boerse) in 425/
# SC TO-T/SC TO-I, oder als Fliesstext ("...under the symbol \"XXXX.\"") in
# DEFM14A/Proxys. build_deals() holt den Volltext ohnehin schon — kein
# Zusatz-Fetch noetig.
COVER_TABLE_TICKER_RE = re.compile(
    r"(?i:Trading\s+Symbol).{0,600}?<B>\s*([A-Z]{1,6})\s*</B>", re.DOTALL)
COVER_NARRATIVE_TICKER_RE = re.compile(
    r"under\s+the\s+symbol\s+.{0,20}?([A-Z]{2,6})\b(?=[.\"'&])", re.IGNORECASE)


def extract_cover_symbol(text: str) -> str | None:
    """Ticker aus dem Filing-Cover/Fliesstext — Fallback wenn cik_to_ticker()
    nichts liefert (delisteter Ziel-Ticker). Erst die Cover-Tabelle (425/SC
    TO-T/SC TO-I), dann die Narrativ-Formulierung (DEFM14A/Proxy) probieren."""
    m = COVER_TABLE_TICKER_RE.search(text)
    if m:
        return m.group(1).upper()
    m = COVER_NARRATIVE_TICKER_RE.search(text)
    if m:
        return m.group(1).upper()
    return None


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
    """Modalwert aller "$X per share in cash"-Treffer, nicht der erste.

    Gefunden bei Nuance (Fix-Runde 3, Task-2-Report): der erste Treffer im
    Dokument war eine überholte/kontextuelle Zwischensumme ($55.50, einmalig
    im Hintergrundabschnitt), während die tatsächliche, als "Merger
    Consideration" definierte Summe ($56.00) dreimal im Dokument auftaucht.
    Die final vereinbarte Summe wird in Titel/Zusammenfassung/Beschluss-
    sprache typischerweise mehrfach wiederholt; überholte oder beiläufig
    erwähnte Beträge meist nur einmal."""
    matches = CASH_RE.findall(text)
    if not matches:
        return None
    prices = [float(m) for m in matches]
    return Counter(prices).most_common(1)[0][0]


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
            # KEEP_WITHOUT_TICKER_FORMS-Zeilen ohne aktuellen Ticker NICHT
            # wegwerfen — das sind genau die spaeter delisteten Cash-Deal-
            # Ziele (Splunk/Nuance/Activision/Twitter-Fall). build_deals()
            # loest den Ticker fuer diese ueber extract_cover_symbol() aus
            # dem ohnehin geholten Volltext auf. Alle anderen Formulare (die
            # grosse Masse, kein Volltext-Fetch vorgesehen) bleiben wie
            # zuvor auf den guenstigen Ticker-Map-Join beschraenkt.
            is_announce = df["form"].isin(KEEP_WITHOUT_TICKER_FORMS)
            # Dedup nach (accession, cik) — NICHT nach accession allein.
            # EDGAR listet gemeinsame 425/DEFM14A-Filings unter BEIDEN
            # Parteien (Acquirer- UND Target-CIK) mit identischer Accession
            # (bestaetigt: Cisco/Splunk-425 2023-09-21, Accession
            # 0001104659-23-102595 unter CIK 858877 UND CIK 1353283). Ein
            # reiner Accession-Dedup behaelt nur die zuerst im Index
            # auftauchende Partei (meist den Acquirer, der noch einen
            # aktuellen Ticker hat) und wirft die Zielfirmen-Zeile still
            # weg — womit genau der delistete Cash-Ziel-Ticker verloren
            # ginge, den extract_cover_symbol() eigentlich retten soll.
            hit = df[df["symbol"].notna() | is_announce].drop_duplicates(
                ["accession", "cik"])
            out.append(hit)
            n_resolved = hit["symbol"].notna().sum()
            print(f"  {y}Q{q}: {len(df):5,} Merger-Zeilen → {len(hit):4,} "
                  f"Kandidaten ({n_resolved:,} mit Ticker)", flush=True)
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


def find_8k_announce_date(cik: int, before, window_days: int = 250):
    """Ankündigungsdatum für reine DEFM14A-Deals (kein 425 vorhanden) aus
    dem frühesten/nächstgelegenen 8-K mit Item 1.01 ("Entry into a Material
    Definitive Agreement") vor `before` (i.d.R. das DEFM14A-Filingdatum).

    DEFM14As eigenes Filingdatum liegt 35-92 Tage NACH der wahren
    Ankündigung (empirisch an Nuance/Activision/Twitter) — als
    announce_date für ein Merger-Arb-Backtest unbrauchbar. Die SEC-
    Submissions-API (`data.sec.gov/submissions/CIK....json`) taggt jedes
    Filing mit Item-Codes; "1.01" ist der Standard-Code für Vertrags-
    unterzeichnung. Mehrere unabhängige Item-1.01-8-Ks pro CIK sind normal
    (Kreditverträge, Beschäftigungsvereinbarungen etc.) — deshalb NICHT das
    global früheste nehmen, sondern das NÄCHSTGELEGENE vor `before`
    innerhalb `window_days`. Verifiziert an allen drei Fällen: korrektes
    8-K getroffen trotz vorhandener älterer, unabhängiger Item-1.01-8-Ks
    (z.B. Twitter hatte 4 weitere in den 165 Tagen davor), 1 Tag Lag zum
    echten Ankündigungsdatum in allen drei Fällen."""
    import datetime as dt
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
                         headers=UA, timeout=30)
        r.raise_for_status()
    except Exception:  # noqa: BLE001
        return None
    recent = r.json().get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    items = recent.get("items", [])
    if not items:
        return None
    best = None
    for f, d, it in zip(forms, dates, items):
        if f != "8-K" or not it or "1.01" not in it.split(","):
            continue
        dd = dt.date.fromisoformat(d)
        if dd <= before and (before - dd).days <= window_days:
            if best is None or dd > best:
                best = dd
    return best


def build_deals(filings: pd.DataFrame) -> pd.DataFrame:
    """Für jede CIK die früheste KEEP_WITHOUT_TICKER_FORMS-Zeile nehmen, den
    Volltext holen, Konsideration + Cash-Preis extrahieren. Ein Fetch pro
    Deal, nicht pro Filing — SEC-Fair-Use, siehe UA-Kommentar oben.

    Dedup nach CIK allein (nicht mehr (CIK, Symbol)) — collect() liefert für
    KEEP_WITHOUT_TICKER_FORMS-Zeilen jetzt auch Kandidaten mit symbol=NaN
    (delisteter Ticker, s. Kommentar bei extract_cover_symbol). Für die wird
    der Ticker hier aus dem bereits geholten Volltext nachgetragen; bleibt
    er unauflösbar, wird die Zeile verworfen (kein Zusatz-Fetch, kein
    Rätselraten). Für DEFM14A-Kandidaten (reine Cash-Deals ohne 425, s.
    KEEP_WITHOUT_TICKER_FORMS-Kommentar) wird announce_date NICHT aus der
    DEFM14A-Zeile selbst genommen, sondern aus find_8k_announce_date()."""
    cand = filings[filings["form"].isin(KEEP_WITHOUT_TICKER_FORMS)].copy()
    if cand.empty:
        return pd.DataFrame(columns=[c.name for c in DEALS_SCHEMA])
    cand = cand.sort_values("date").drop_duplicates(["cik"], keep="first")
    rows = []
    for _, r in cand.iterrows():
        text = fetch_filing_text(int(r["cik"]), r["path"])
        if not text:
            continue
        symbol = r["symbol"]
        if pd.isna(symbol):
            symbol = extract_cover_symbol(text)
            if not symbol:
                continue  # nicht identifizierbar — verwerfen, nicht raten
        announce_date = r["date"]
        if r["form"] == "DEFM14A":
            resolved = find_8k_announce_date(int(r["cik"]), r["date"])
            if resolved is not None:
                announce_date = resolved
            # sonst: Fallback auf das DEFM14A-Datum selbst — spät, aber
            # besser als die Zeile komplett zu verwerfen.
        cons = classify_consideration(text)
        price = extract_cash_price(text) if cons in ("cash", "mixed") else None
        rows.append({"announce_date": announce_date, "symbol": symbol,
                     "cik": int(r["cik"]), "source_form": r["form"],
                     "source_accession": r["accession"],
                     "company": r["company"], "consideration_type": cons,
                     "deal_price_cash": price})
        time.sleep(0.15)
    return pd.DataFrame(rows)


def _resolve_from_deals(filings: pd.DataFrame, deals: pd.DataFrame) -> pd.DataFrame:
    """Trägt aus build_deals() aufgelöste Ticker (delistete Cash-Ziele, s.
    extract_cover_symbol) zurück in die filings-Tabelle ein und verwirft
    Zeilen, die auch danach keinen Ticker haben — z.B. ANNOUNCE_FORMS-
    Zeilen, deren Cover-Ticker sich nicht extrahieren ließ. Notwendig, weil
    collect()/refresh() ANNOUNCE_FORMS-Zeilen jetzt mit symbol=NaN
    durchreichen (statt sie sofort zu verwerfen) und T_FILINGS.symbol
    REQUIRED ist."""
    if len(deals):
        resolved = deals.dropna(subset=["symbol"]).drop_duplicates(
            "cik").set_index("cik")["symbol"]
        filings = filings.copy()
        filings["symbol"] = filings["symbol"].fillna(
            filings["cik"].map(resolved))
    return filings.dropna(subset=["symbol"])


def backfill(start_year=2007):
    filings = collect(start_year)
    deals = build_deals(filings)
    filings = _resolve_from_deals(filings, deals)
    print(f"\n{len(filings):,} Merger-Filings, "
          f"{filings['symbol'].nunique():,} Symbole")
    ensure_table(T_FILINGS, FILINGS_SCHEMA, partition_field="date",
                clustering=["symbol"])
    load_df(T_FILINGS, filings[["date", "symbol", "cik", "form", "accession",
                                "company"]],
           schema=FILINGS_SCHEMA, write="WRITE_TRUNCATE")
    print(f"→ {T_FILINGS}")
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
        is_announce = df["form"].isin(KEEP_WITHOUT_TICKER_FORMS)
        # Dedup (accession, cik) — s. ausführlicher Kommentar in collect().
        frames.append(df[df["symbol"].notna() | is_announce].drop_duplicates(
            ["accession", "cik"]))
    if not frames:
        print("Merger-Refresh: keine Zeilen")
        return
    new = pd.concat(frames, ignore_index=True)
    new["date"] = pd.to_datetime(new["date"]).dt.date
    ensure_table(T_FILINGS, FILINGS_SCHEMA, partition_field="date",
                clustering=["symbol"])
    try:
        have = query(f"SELECT DISTINCT accession FROM `{T_FILINGS}` "
                     f"WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 200 DAY)")
        seen = set(have["accession"])
        new = new[~new["accession"].isin(seen)]
    except Exception:  # noqa: BLE001
        pass
    if new.empty:
        print("Merger-Refresh: nichts Neues")
        return
    deals = build_deals(new)
    new = _resolve_from_deals(new, deals)
    if new.empty:
        print("Merger-Refresh: nichts Neues (Ticker nicht auflösbar)")
        return
    load_df(T_FILINGS, new[["date", "symbol", "cik", "form", "accession",
                           "company"]], schema=FILINGS_SCHEMA)
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
