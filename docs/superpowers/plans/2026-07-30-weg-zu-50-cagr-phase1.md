# Weg zu 50% CAGR — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the three Phase-1 deliverables from
`docs/superpowers/specs/2026-07-30-weg-zu-50-cagr-design.md` that need no
Discovery-Pipeline gates: an execution-cost diagnosis, the MERGARB data
pipeline + backtest study, and a pre-registered variant grid for OPTPREM.

**Architecture:** Each deliverable is a standalone script/module following
the repo's existing research-script convention (no pytest suite exists in
`quant/` — verification is "run the module, read the printed diagnostic,
compare against a pre-registered threshold"). Every task splits pure,
synthetic-data-testable logic from I/O (BigQuery/SEC/Alpaca), so the pure
logic gets a real red→green test cycle without hitting the network.

**Tech Stack:** Python 3.11, pandas/numpy, `google-cloud-bigquery`
(`quant/data/bq.py` helpers: `query`, `load_df`, `ensure_table`), `requests`
against SEC EDGAR and FRED.

## Global Constraints

- No new dependencies — everything needed (`pandas`, `numpy`, `requests`,
  `google-cloud-bigquery`, `scipy`) is already in
  `quant/cloud/requirements-cloud.txt`.
- BigQuery project/dataset: `trading-436516.quant` (`GCP_PROJECT`,
  `BQ_DATASET` in `quant/config.py`).
- SEC EDGAR requests need `User-Agent` headers with a real contact (see
  `UA` constant in `quant/data/sec_13d_ingest.py`) and a `time.sleep(0.15)`
  between quarter-index fetches ("SEC-Fair-Use", same file).
- This plan does **not** touch the Discovery Pipeline, `hypothesis_queue.yaml`,
  `promoted.yaml`, or any live-execution code — Phase 2 of the spec covers
  that, only after these results exist.
- All money/return figures stay gross/pre-tax, matching `quant/FINDINGS.md`
  convention.

---

## File Structure

- **Modify** `quant/ops/cost_monitor.py` — add the size/liquidity slippage
  breakdown (Task 1).
- **Create** `quant/data/merger_ingest.py` — SEC EDGAR merger-filing ingester,
  mirrors `quant/data/sec_13d_ingest.py` (Task 2).
- **Create** `quant/research/mergarb_study.py` — deal-level backtest +
  `returns()`/`live_weights()` entry points for the eventual Discovery run
  (Task 3).
- **Modify** `quant/research/options_phase_a.py` — pre-registered 12-variant
  grid, logged to `trials_registry` (Task 4).

---

### Task 1: Exekutionskosten-Diagnose (`quant/ops/cost_monitor.py`)

**Files:**
- Modify: `quant/ops/cost_monitor.py`

**Interfaces:**
- Consumes: existing `attach_benchmarks`, `T_COSTS`, `GCP_PROJECT`,
  `BQ_DATASET`, `query` (already imported in the file).
- Produces: `attach_adv(m: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame`,
  `bucket_slippage_by_participation(m: pd.DataFrame) -> pd.DataFrame`,
  `diagnose_verdict(m: pd.DataFrame) -> tuple[float, str]`,
  `diagnose(days: int = 30) -> pd.DataFrame`, new CLI flag `--diagnose`.

- [ ] **Step 1: Write the failing tests for the pure bucketing/verdict logic**

Run `python3 -c` with this inline script (no file needed — this project has
no pytest suite; verification is a deterministic assertion script):

```python
python3 -c "
import numpy as np, pandas as pd
from quant.ops.cost_monitor import (attach_adv, bucket_slippage_by_participation,
                                     diagnose_verdict)

# attach_adv: participation_pct = qty / adv20 (20d volume mean, shifted 1 day
# so the fill day itself never leaks into its own ADV).
bars = pd.DataFrame({
    'date': pd.date_range('2026-01-01', periods=25, freq='D').tolist() * 1,
    'symbol': ['AAA'] * 25, 'volume': [1000] * 25})
fills = pd.DataFrame({'symbol': ['AAA'], 'fill_date': [pd.Timestamp('2026-01-25')],
                      'qty': [50.0]})
out = attach_adv(fills, bars)
assert abs(out['adv20'].iloc[0] - 1000.0) < 1e-6, out['adv20'].iloc[0]
assert abs(out['participation_pct'].iloc[0] - 5.0) < 1e-6, out['participation_pct'].iloc[0]

# bucket_slippage_by_participation: known buckets, known averages
m = pd.DataFrame({'participation_pct': [0.5, 3.0, 7.0, 20.0, 0.6, 3.5],
                  'slippage_bps': [1.0, 2.0, 3.0, 4.0, 1.2, 2.2]})
b = bucket_slippage_by_participation(m)
assert set(b['bucket']) == {'<1%', '1-5%'}, b['bucket'].tolist()  # buckets with n<3 dropped
row = b[b['bucket'] == '<1%'].iloc[0]
assert abs(row['avg_slippage_bps'] - 1.1) < 1e-6, row['avg_slippage_bps']

# diagnose_verdict: three synthetic cases
rng = np.random.default_rng(0)
size_dep = pd.DataFrame({'participation_pct': np.linspace(0.1, 20, 200),
                         'slippage_bps': np.linspace(0.1, 20, 200) * 2
                                          + rng.normal(0, 0.5, 200)})
corr, verdict = diagnose_verdict(size_dep)
assert corr > 0.3, corr
assert verdict == 'GROESSENABHAENGIG', verdict

flat = pd.DataFrame({'participation_pct': rng.uniform(0, 20, 200),
                     'slippage_bps': rng.normal(0, 0.3, 200)})
corr, verdict = diagnose_verdict(flat)
assert verdict == 'KEIN_HANDLUNGSBEDARF', verdict

noisy = pd.DataFrame({'participation_pct': rng.uniform(0, 20, 200),
                      'slippage_bps': rng.normal(8, 5, 200)})
corr, verdict = diagnose_verdict(noisy)
assert abs(corr) <= 0.3, corr
assert verdict == 'ROUTING_ODER_MESSFEHLER', verdict
print('OK')
"
```

- [ ] **Step 2: Run it to verify it fails**

Run the script above. Expected: `ImportError: cannot import name 'attach_adv'`
(the functions do not exist yet).

- [ ] **Step 3: Implement the minimal code**

Add to `quant/ops/cost_monitor.py`, directly after `attach_benchmarks`:

```python
def attach_adv(m: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """Fügt adv20 (20-Tage-Volumenschnitt VOR dem Fill-Tag, `shift(1)` damit
    der Fill-Tag selbst nie in seine eigene ADV einfließt) und
    participation_pct (Ordergröße als % davon) hinzu. `bars` sind rohe
    eod_bars-Zeilen (date, symbol, volume) — getrennt von der BQ-Abfrage,
    damit diese Funktion mit synthetischen Daten testbar ist."""
    m = m.copy()
    if m.empty or bars.empty:
        m["adv20"] = np.nan
        m["participation_pct"] = np.nan
        return m
    b = bars.copy()
    b["date"] = pd.to_datetime(b["date"])
    b = b.sort_values(["symbol", "date"])
    b["adv20"] = b.groupby("symbol")["volume"].transform(
        lambda s: s.rolling(20, min_periods=1).mean().shift(1))
    adv_map = b.set_index(["symbol", "date"])["adv20"]
    fill_dates = pd.to_datetime(m["fill_date"])
    m["adv20"] = [adv_map.get((s, d), np.nan)
                 for s, d in zip(m["symbol"], fill_dates)]
    m["participation_pct"] = (m["qty"] / m["adv20"] * 100).replace(
        [np.inf, -np.inf], np.nan)
    return m


def bucket_slippage_by_participation(m: pd.DataFrame) -> pd.DataFrame:
    """Bucket-Tabelle (bucket, n, avg_slippage_bps, avg_participation_pct).
    Buckets mit n<3 werden verworfen — sonst dominiert ein einzelner Fill
    den Bucket-Mittelwert."""
    bins = [0, 1, 5, 10, np.inf]
    labels = ["<1%", "1-5%", "5-10%", ">10%"]
    m = m.copy()
    m["adv_bucket"] = pd.cut(m["participation_pct"], bins=bins, labels=labels)
    rows = []
    for b in labels:
        g = m[m["adv_bucket"] == b]
        if len(g) < 3:
            continue
        rows.append({"bucket": b, "n": len(g),
                     "avg_slippage_bps": float(g["slippage_bps"].mean()),
                     "avg_participation_pct": float(g["participation_pct"].mean())})
    return pd.DataFrame(rows)


def diagnose_verdict(m: pd.DataFrame) -> tuple[float, str]:
    """Unterscheidet die drei Hypothesen aus dem Design-Spec
    (docs/superpowers/specs/2026-07-30-weg-zu-50-cagr-design.md, Hebel 1):
    GROESSENABHAENGIG (|r|>0.3 zwischen %ADV und Slippage) → Order-Cap;
    KEIN_HANDLUNGSBEDARF (Slippage klein und flach, std<2bp) → nichts tun;
    ROUTING_ODER_MESSFEHLER (Slippage groß, aber unkorreliert mit Größe) →
    Alpacas opg/cls-Routing bzw. den Benchmark-Zeitpunkt prüfen."""
    corr = float(m["participation_pct"].corr(m["slippage_bps"]))
    if abs(corr) > 0.3:
        return corr, "GROESSENABHAENGIG"
    if float(m["slippage_bps"].std()) < 2.0:
        return corr, "KEIN_HANDLUNGSBEDARF"
    return corr, "ROUTING_ODER_MESSFEHLER"
```

- [ ] **Step 4: Run the test script again to verify it passes**

Run the exact script from Step 1 (adjust the two `'GROESSENABHAENGIG'`
assertions — they were written against the final function, so no change
needed). Expected output: `OK`.

- [ ] **Step 5: Wire the I/O layer + CLI flag**

Add to `quant/ops/cost_monitor.py`, after the functions from Step 3:

```python
def diagnose(days: int = 30) -> pd.DataFrame:
    hist = query(f"""
      SELECT sleeve, symbol, side, tif, slippage_bps, notional, fill_date, qty
      FROM `{T_COSTS}`
      WHERE fill_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY)
        AND notional >= {MIN_NOTIONAL_FOR_SLIPPAGE}""")
    if hist.empty:
        print(f"keine Fills >= ${MIN_NOTIONAL_FOR_SLIPPAGE:.0f} in {days} Tagen "
              "— --check zuerst laufen lassen, um fill_costs zu füllen")
        return hist
    syms = ", ".join(repr(s) for s in hist["symbol"].unique())
    lo = (pd.to_datetime(hist["fill_date"]).min() - pd.Timedelta(days=45)).date()
    hi = pd.to_datetime(hist["fill_date"]).max().date()
    bars = query(f"""
      SELECT date, symbol, volume FROM `{GCP_PROJECT}.{BQ_DATASET}.eod_bars`
      WHERE symbol IN ({syms}) AND date BETWEEN '{lo}' AND '{hi}'""")
    m = attach_adv(hist, bars).dropna(subset=["participation_pct"])
    if len(m) < 10:
        print(f"nur {len(m)} Fills mit ADV-Zuordnung — zu wenig für eine "
              "belastbare Diagnose")
        return m
    b = bucket_slippage_by_participation(m)
    print(f"{'Bucket':8s} {'n':>5s} {'Ø Slippage':>12s} {'Ø %ADV':>8s}")
    for _, r in b.iterrows():
        print(f"{r['bucket']:8s} {int(r['n']):5d} "
              f"{r['avg_slippage_bps']:10.1f}bp {r['avg_participation_pct']:7.1f}%")
    corr, verdict = diagnose_verdict(m)
    print(f"\nKorrelation %ADV ↔ Slippage: r={corr:+.2f} → {verdict}")
    if verdict == "GROESSENABHAENGIG":
        print("  Order-Cap als %ADV empfehlen (Hebel 1a).")
    elif verdict == "ROUTING_ODER_MESSFEHLER":
        print("  Slippage groß, aber unabhängig von der Größe — prüfe Alpacas "
              "opg/cls-Routing gegen die primäre Börse, oder den "
              "Benchmark-Zeitpunkt in attach_benchmarks() (Hebel 1b/1c).")
    return m
```

Modify the `if __name__ == "__main__":` block at the bottom of the file:

```python
if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true")
    p.add_argument("--diagnose", action="store_true")
    p.add_argument("--days", type=int, default=5)
    p.add_argument("--no-alert", action="store_true")
    a = p.parse_args()
    if a.diagnose:
        diagnose(days=max(a.days, 30))
    elif a.check:
        check(days=a.days, alert=not a.no_alert)
    else:
        p.print_help()
        sys.exit(1)
```

- [ ] **Step 6: Run against real data**

```bash
python3 -m quant.ops.cost_monitor --diagnose --days 30
```

Expected: either a bucket table + a `GROESSENABHAENGIG` /
`KEIN_HANDLUNGSBEDARF` / `ROUTING_ODER_MESSFEHLER` verdict, or an honest
"zu wenig Fills" message if the burn-in window doesn't have 10+ qualifying
fills yet — do not treat a small-sample message as a bug.

- [ ] **Step 7: Commit**

```bash
git add quant/ops/cost_monitor.py
git commit -m "quant: Exekutionskosten-Diagnose — Slippage nach %ADV aufschlüsseln

Unterscheidet größenabhängigen Impact von einem Routing-Artefakt oder
Benchmark-Messfehler (Design-Spec 2026-07-30, Hebel 1). Kein neuer Sleeve,
keine Discovery-Gates — nur Diagnose an bestehendem, validiertem Code."
```

---

### Task 2: SEC-EDGAR-Merger-Ingester (`quant/data/merger_ingest.py`)

**Files:**
- Create: `quant/data/merger_ingest.py`

**Interfaces:**
- Consumes: `quant.data.bq.{ensure_table,load_df,query}`,
  `quant.config.{BQ_DATASET,GCP_PROJECT}` (same imports as
  `sec_13d_ingest.py`).
- Produces: BQ tables `quant.merger_filings` (raw index rows) and
  `quant.merger_deals` (extracted deal terms); functions
  `classify_consideration(text: str) -> str`,
  `extract_cash_price(text: str) -> float | None`,
  `quarter_filings(year: int, qtr: int) -> pd.DataFrame`,
  `cik_to_ticker() -> dict[int, str]` (reuse pattern from
  `sec_13d_ingest.cik_to_ticker`), `fetch_filing_text(cik: int, accession: str) -> str`,
  `build_deals(filings: pd.DataFrame) -> pd.DataFrame`,
  `collect(...)`, `backfill(...)`, `refresh()`, `pilot()`.

- [ ] **Step 1: Write the failing tests for the pure extraction logic**

```python
python3 -c "
from quant.data.merger_ingest import classify_consideration, extract_cash_price

cash_text = 'Each share of Common Stock will be converted into the right to receive \$45.50 per share in cash, without interest.'
assert classify_consideration(cash_text) == 'cash'
assert abs(extract_cash_price(cash_text) - 45.50) < 1e-6

stock_text = 'Each share of Target Common Stock will be exchanged for 0.6250 shares of Acquiror Common Stock.'
assert classify_consideration(stock_text) == 'stock'
assert extract_cash_price(stock_text) is None

mixed_text = 'Stockholders may elect to receive \$30.00 per share in cash or 0.40 shares of Acquiror Common Stock for each share held.'
assert classify_consideration(mixed_text) == 'mixed'

unknown_text = 'This is a routine 8-K filing regarding an unrelated matter.'
assert classify_consideration(unknown_text) == 'unknown'
assert extract_cash_price(unknown_text) is None
print('OK')
"
```

- [ ] **Step 2: Run it to verify it fails**

Expected: `ModuleNotFoundError: No module named 'quant.data.merger_ingest'`.

- [ ] **Step 3: Write the minimal implementation**

Create `quant/data/merger_ingest.py`:

```python
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
    r"shares?\s+of\s+.{0,40}common\s+stock", re.IGNORECASE)


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
```

- [ ] **Step 4: Run the test script again to verify it passes**

Run the exact script from Step 1. Expected: `OK`.

- [ ] **Step 5: Run the pilot against real SEC data**

```bash
python3 -m quant.data.merger_ingest --pilot
```

Expected: a nonzero filing count for every year 2019–2026 (if 2025/2026
show 0, the script prints the label-update warning — investigate the actual
`form.idx` lines for those quarters before proceeding, do not silently
continue), plus a printed count of extracted cash deals. This does not
write to BigQuery.

- [ ] **Step 6: Commit**

```bash
git add quant/data/merger_ingest.py
git commit -m "quant: SEC-Merger-Ingester (425/DEFM14A/SC TO-T/SC TO-I/SC 14D9)

Ingestiert Merger-Filings nach dem Muster von sec_13d_ingest.py und
extrahiert Cash-Deal-Preise per Regex aus der frühesten Announce-Filing je
CIK. Baut quant.merger_filings + quant.merger_deals. Teil von Hebel 2
(Design-Spec 2026-07-30) — noch nicht durch die Discovery-Gates."
```

---

### Task 3: MERGARB-Backtest (`quant/research/mergarb_study.py`)

**Files:**
- Create: `quant/research/mergarb_study.py`

**Interfaces:**
- Consumes: `quant.data.bq.query`, `quant.data.merger_ingest.T_DEALS` (BQ
  table name string), `quant.research.exotic_sleeves.fred` (VIX series,
  signature `fred(series: str, start="2015-01-01") -> pd.Series`).
- Produces: `deal_return_series(announce_date, terminal_date, prices: pd.Series) -> pd.Series`,
  `resolve_terminal_date(prices: pd.Series, announce_date, max_horizon_days: int = 270) -> tuple[pd.Timestamp | None, str]`,
  `returns(**params) -> pd.Series` (Discovery-Pipeline entry point —
  matches the signature `discovery.evaluate()` expects, see
  `quant/research/discovery.py:211-220`), `live_weights() -> tuple[dict, str]`
  (G8 entry point, matches `quant/research/discovery.py:296-310`),
  `check_predictions() -> dict`.

- [ ] **Step 1: Write the failing tests for the pure resolution/return logic**

```python
python3 -c "
import pandas as pd
from quant.research.mergarb_study import resolve_terminal_date, deal_return_series

# Case 1: symbol stops trading (delisted) 40 days after announcement → 'closed'
idx = pd.date_range('2026-01-01', periods=40, freq='B')
prices = pd.Series([100.0] * 40, index=idx)  # last observation = delisting day
d, status = resolve_terminal_date(prices, idx[0], max_horizon_days=270)
assert status == 'closed', status
assert d == idx[-1], (d, idx[-1])

# Case 2: still trading, price near the deal price at the horizon → 'open'
idx2 = pd.date_range('2026-01-01', periods=300, freq='B')
prices2 = pd.Series([100.0] * 300, index=idx2)
d2, status2 = resolve_terminal_date(prices2, idx2[0], max_horizon_days=270)
assert status2 == 'open', status2

# Case 3: still trading past the horizon, price has dropped away → 'break'.
# Wide margin around the ~193-business-day horizon boundary (270 calendar
# days ≈ 193 business days at 5/7): flat for 250 business days (comfortably
# past the boundary), THEN drops — so the horizon-window tail is provably
# still flat and the drop is provably outside it.
idx3 = pd.date_range('2026-01-01', periods=400, freq='B')
prices3 = pd.Series([100.0] * 250 + [70.0] * 150, index=idx3)
d3, status3 = resolve_terminal_date(prices3, idx3[0], max_horizon_days=270)
assert status3 == 'break', status3

# deal_return_series: simple daily returns from announce to terminal, inclusive
rets = deal_return_series(idx[0], idx[10], prices.loc[idx[0]:idx[10]])
assert len(rets) == 10, len(rets)  # 11 prices -> 10 daily returns
assert abs(rets.sum()) < 1e-9  # flat price -> zero return
print('OK')
"
```

- [ ] **Step 2: Run it to verify it fails**

Expected: `ModuleNotFoundError: No module named 'quant.research.mergarb_study'`.

- [ ] **Step 3: Write the minimal implementation**

Create `quant/research/mergarb_study.py`:

```python
"""MERGARB-Backtest — Merger-Arbitrage auf angekündigte Cash-Übernahmen.

    python3 -m quant.research.mergarb_study --run

Familie MERGARB in quant/research/hypothesis_queue.yaml. Phase 1
(docs/superpowers/specs/2026-07-30-weg-zu-50-cagr-design.md): nur Cash-Deals
(Aktientausch braucht ein Hedge-Bein gegen den Käufer — Phase 2). Terminal-
Datum ist ein Proxy: eine Aktie, die aus eod_bars verschwindet, gilt als
GESCHLOSSEN (Delisting = Closing); eine Aktie, die über den Horizont hinaus
weiterhandelt UND spürbar vom Angebotspreis abgedriftet ist, gilt als
GEBROCHEN. Das ist eine Näherung (kein 8-K-Terminierungs-Scan) — bewusst
offengelegt wie die Delta-Proxy-Notiz in options_phase_a.py.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.config import BQ_DATASET, GCP_PROJECT
from quant.data.bq import query
from quant.data.merger_ingest import T_DEALS
from quant.research.exotic_sleeves import fred

MAX_HORIZON_DAYS = 270
BREAK_DRIFT_PCT = 0.15   # Abweichung vom letzten Preis, ab der ein noch
                         # offener, über-den-Horizont-hinaus laufender Deal
                         # als gebrochen gilt statt als weiter offen


def resolve_terminal_date(prices: pd.Series, announce_date,
                          max_horizon_days: int = MAX_HORIZON_DAYS
                          ) -> tuple[pd.Timestamp | None, str]:
    """prices: tägliche Kursreihe des Ziels AB dem Ankündigungstag (Index =
    Handelstage, wie sie in eod_bars vorliegen — kein künstliches Auffüllen).
    Liefert (Terminaldatum, Status) mit Status ∈ {closed, break, open}."""
    prices = prices.dropna().sort_index()
    if prices.empty:
        return None, "open"
    ann = pd.Timestamp(announce_date)
    horizon_end = ann + pd.Timedelta(days=max_horizon_days)
    last_obs = prices.index[-1]
    # Symbol handelt nicht mehr, aber der allgemeine Handelskalender läuft
    # weiter (der Aufrufer übergibt nur Kurse bis "heute") → Delisting = Close.
    today = pd.Timestamp.today().normalize()
    trading_gap = (today - last_obs).days
    if trading_gap > 10 and last_obs < horizon_end:
        return last_obs, "closed"
    if last_obs <= horizon_end:
        return None, "open"
    tail = prices.loc[ann:horizon_end]
    if tail.empty:
        return None, "open"
    drift = abs(prices.iloc[-1] / tail.iloc[-1] - 1)
    if drift > BREAK_DRIFT_PCT:
        return horizon_end, "break"
    return None, "open"


def deal_return_series(announce_date, terminal_date,
                       prices: pd.Series) -> pd.Series:
    """Tägliche einfache Renditen von announce_date bis terminal_date
    (inklusive), aus der Kursreihe des Ziels."""
    p = prices.dropna().sort_index()
    p = p.loc[pd.Timestamp(announce_date):pd.Timestamp(terminal_date)]
    if len(p) < 2:
        return pd.Series(dtype=float)
    return p.pct_change().dropna()


def _load_price_history(symbols: list[str], min_date) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    syms = ", ".join(repr(s) for s in symbols)
    df = query(f"""
      SELECT date, symbol, adjusted_close AS px
      FROM `{GCP_PROJECT}.{BQ_DATASET}.eod_bars`
      WHERE symbol IN ({syms}) AND date >= '{min_date}' AND adjusted_close > 0
      ORDER BY symbol, date""")
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_deals(cash_only: bool = True) -> pd.DataFrame:
    df = query(f"SELECT * FROM `{T_DEALS}`")
    if df.empty:
        return df
    if cash_only:
        df = df[(df["consideration_type"] == "cash")
                & df["deal_price_cash"].notna()]
    return df.reset_index(drop=True)


def _resolved_deals() -> pd.DataFrame:
    """Deals + ihr Terminalstatus, je aus eod_bars nachgerechnet."""
    deals = load_deals(cash_only=True)
    if deals.empty:
        return deals
    prices = _load_price_history(list(deals["symbol"].unique()),
                                 deals["announce_date"].min())
    if prices.empty:
        return pd.DataFrame()
    by_sym = {s: g.set_index("date")["px"] for s, g in prices.groupby("symbol")}
    rows = []
    for _, d in deals.iterrows():
        px = by_sym.get(d["symbol"])
        if px is None:
            continue
        term, status = resolve_terminal_date(px, d["announce_date"])
        rows.append({**d.to_dict(), "terminal_date": term, "status": status,
                     "announce_px": px.loc[pd.Timestamp(d["announce_date"]):]
                                      .iloc[0] if len(px.loc[pd.Timestamp(
                                          d["announce_date"]):]) else np.nan})
    return pd.DataFrame(rows)


def returns(**params) -> pd.Series:
    """Discovery-Pipeline-Entry-Point (discovery.evaluate erwartet
    `fn(**params) -> pd.Series`). Nur Tage, an denen mindestens ein Deal
    offen ist — auf Tagen ohne Position künstlich 0 einzutragen würde die
    annualisierte Sharpe gegen ein Buch verwässern, das ungenutztes Kapital
    tatsächlich reinvestiert (siehe Design-Spec, Hebel 2)."""
    resolved = _resolved_deals()
    if resolved.empty:
        return pd.Series(dtype=float)
    closed = resolved[resolved["status"].isin(["closed", "break"])]
    if closed.empty:
        return pd.Series(dtype=float)
    prices = _load_price_history(list(closed["symbol"].unique()),
                                 closed["announce_date"].min())
    by_sym = {s: g.set_index("date")["px"] for s, g in prices.groupby("symbol")}
    per_deal = []
    for _, d in closed.iterrows():
        px = by_sym.get(d["symbol"])
        if px is None or pd.isna(d["terminal_date"]):
            continue
        r = deal_return_series(d["announce_date"], d["terminal_date"], px)
        if len(r):
            per_deal.append(r)
    if not per_deal:
        return pd.Series(dtype=float)
    wide = pd.concat(per_deal, axis=1)
    return wide.mean(axis=1, skipna=True).dropna()


def live_weights() -> tuple[dict, str]:
    """G8-Entry-Point: aktuell offene Cash-Deals, gleichgewichtet, gross<=1.0."""
    resolved = _resolved_deals()
    if resolved.empty:
        return {}, "keine Deal-Daten (fail-closed)"
    open_deals = resolved[resolved["status"] == "open"]
    if open_deals.empty:
        return {}, "kein offener Cash-Deal → flat"
    w = 1.0 / len(open_deals)
    weights = {s: w for s in open_deals["symbol"].unique()}
    return weights, f"{len(weights)} offene Cash-Deals, EW"


def check_predictions() -> dict:
    """Die drei vorregistrierten Vorhersagen aus hypothesis_queue.yaml
    (id: MERGARB). (b) Cash>Aktientausch ist in Phase 1 NICHT prüfbar (keine
    Aktientausch-Preisextraktion) — wird als 'nicht_pruefbar' ausgewiesen,
    nicht stillschweigend übersprungen."""
    resolved = _resolved_deals()
    out = {"a_monotonie": None, "b_cash_vs_stock": "nicht_pruefbar_phase1",
          "c_vix_bruchrate": None}
    closed = resolved[resolved["status"].isin(["closed", "break"])].copy()
    if len(closed) >= 5:
        closed["spread"] = (closed["deal_price_cash"] / closed["announce_px"]
                            - 1)
        closed["holding_ret"] = np.where(
            closed["status"] == "closed",
            closed["deal_price_cash"] / closed["announce_px"] - 1, np.nan)
        closed["decile"] = pd.qcut(closed["spread"], min(5, closed["spread"]
                                                          .nunique()),
                                   duplicates="drop")
        by_decile = closed.groupby("decile")["holding_ret"].mean()
        out["a_monotonie"] = by_decile.to_dict()
        out["a_monoton"] = bool(by_decile.is_monotonic_increasing)
    if len(closed) >= 5:
        vix = fred("VIXCLS", start="2015-01-01")
        vix_at_announce = vix.reindex(pd.to_datetime(closed["announce_date"]),
                                      method="ffill")
        closed["vix"] = vix_at_announce.values
        high = closed[closed["vix"] > 25]
        low = closed[closed["vix"] <= 25]
        out["c_vix_bruchrate"] = {
            "hoch_vix": float(high["status"].eq("break").mean()) if len(high) else None,
            "niedrig_vix": float(low["status"].eq("break").mean()) if len(low) else None,
        }
    return out


def run():
    deals = load_deals(cash_only=False)
    print(f"{len(deals):,} Deals in quant.merger_deals "
          f"({(deals['consideration_type'] == 'cash').sum() if len(deals) else 0} "
          "Cash)")
    r = returns()
    if r.empty:
        print("keine auswertbare Rendite-Serie (zu wenige resolved Deals)")
        return
    ann = 252
    sharpe = r.mean() / r.std() * np.sqrt(ann) if r.std() > 0 else 0.0
    print(f"Sharpe (nur Tage mit offenem Deal): {sharpe:.2f}, n={len(r)}")
    preds = check_predictions()
    print("Vorhersagen:", preds)
    w, why = live_weights()
    print(f"live_weights: {why} ({len(w)} Positionen)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    a = p.parse_args()
    if not a.run:
        p.print_help()
        sys.exit(1)
    run()
```

- [ ] **Step 4: Run the test script again to verify it passes**

Run the exact script from Step 1. Expected: `OK`.

- [ ] **Step 5: Run against real data (requires Task 2's `--backfill` to have populated `quant.merger_deals` first)**

```bash
python3 -m quant.data.merger_ingest --backfill
python3 -m quant.research.mergarb_study --run
```

Expected: a printed deal count, a Sharpe number (or an honest
"keine auswertbare Rendite-Serie" if too few deals resolved), the
prediction dict (with `b_cash_vs_stock` explicitly `nicht_pruefbar_phase1`),
and the current `live_weights()` output. Do not proceed to Task 4 assuming
a particular Sharpe value — read what actually prints.

- [ ] **Step 6: Commit**

```bash
git add quant/research/mergarb_study.py
git commit -m "quant: MERGARB-Backtest — returns()/live_weights() für die Discovery-Pipeline

Terminal-Datum per Delisting-/Drift-Proxy aus eod_bars (kein 8-K-Scan, offen
ausgewiesen). Vorhersage (b) Cash>Aktientausch ist in Phase 1 nicht prüfbar
und wird als solche ausgewiesen. Noch nicht in hypothesis_queue.yaml verdrahtet
oder durch G0-G8 geschickt — das ist Phase 2."
```

---

### Task 4: OPTPREM-Variantenraster (`quant/research/options_phase_a.py`)

**Files:**
- Modify: `quant/research/options_phase_a.py`

**Interfaces:**
- Consumes: existing `alpaca_daily`, `fred`, `occ`, `bar_close`, `H`,
  `UNDERLYINGS`; `quant.research.trials_registry.log_trial`
  (signature `log_trial(family: str, returns: pd.Series, variant: str = "", verdict: str = "", notes: str = "", ann: int = 252, config: dict | None = None) -> dict`).
- Produces: `simulate(otm_short: float, width: float, vix_filter: bool) -> pd.DataFrame`
  (pure refactor of the current `run()` body), `weekly_returns(df: pd.DataFrame) -> pd.Series`,
  `OTM_GRID`, `WIDTH_GRID`, `VIX_FILTER_GRID`, `run_all_variants()`.

- [ ] **Step 1: Write the failing test for `weekly_returns`**

```python
python3 -c "
import pandas as pd
from quant.research.options_phase_a import weekly_returns

df = pd.DataFrame({
    'date': pd.to_datetime(['2026-01-05', '2026-01-12', '2026-01-19']),
    'ret_on_risk': [0.05, -0.10, 0.03]})
r = weekly_returns(df)
assert list(r.index) == list(df['date']), r.index
assert abs(r.iloc[1] - (-0.10)) < 1e-9
print('OK')
"
```

- [ ] **Step 2: Run it to verify it fails**

Expected: `ImportError: cannot import name 'weekly_returns'`.

- [ ] **Step 3: Refactor `run()` into `simulate()` + add the variant grid**

Replace the existing `run()` function in
`quant/research/options_phase_a.py` (currently lines 62–124) with:

```python
OTM_GRID = [0.015, 0.02, 0.03]
WIDTH_GRID = [0.01, 0.02]
VIX_FILTER_GRID = [True, False]


def weekly_returns(df: pd.DataFrame) -> pd.Series:
    """ret_on_risk-Spalte als Rendite-Serie, indiziert auf das Wochendatum —
    der gemeinsame Nenner, den trials_registry.log_trial erwartet."""
    s = df.set_index("date")["ret_on_risk"].sort_index()
    s.index = pd.to_datetime(s.index)
    return s


def simulate(otm_short: float = OTM_SHORT, width: float = WIDTH,
            vix_filter: bool = False) -> pd.DataFrame:
    """Reine Backtest-Funktion für EINE Variante — liefert die Wochenzeilen,
    schreibt nichts. `run_all_variants()` und `run()` sind die I/O-Wrapper."""
    vix = fred("VIXCLS", start="2024-01-01")
    results = []
    for u in UNDERLYINGS:
        px = alpaca_daily(u, "2024-02-01")["c"]
        px.index = pd.to_datetime(px.index)
        mondays = [d for d in px.index if d.weekday() == 0]
        for d in mondays:
            spot = px[d]
            expiry = d + pd.Timedelta(days=(4 - d.weekday()) % 7)
            if expiry <= d:
                expiry += pd.Timedelta(days=7)
            if expiry not in px.index:
                continue
            v = vix.reindex([d]).ffill().iloc[-1]
            if vix_filter and v >= 25:
                continue
            k_short = round(spot * (1 - otm_short))
            k_long = round(spot * (1 - otm_short - width))
            cs = occ(u, expiry, "P", k_short)
            cl = occ(u, expiry, "P", k_long)
            p_short = bar_close(cs, d)
            p_long = bar_close(cl, d)
            if p_short is None or p_long is None:
                continue
            credit = (p_short - p_long) * (1 - CREDIT_HAIRCUT) - 0.04
            if credit <= 0:
                continue
            settle = px[expiry]
            payoff = -max(k_short - settle, 0) + max(k_long - settle, 0)
            pnl = credit + payoff
            width_usd = k_short - k_long
            results.append({"u": u, "date": d, "vix": v, "credit": credit,
                            "pnl": pnl, "max_loss": width_usd - credit,
                            "ret_on_risk": pnl / (width_usd - credit)})
    return pd.DataFrame(results)


def run_all_variants():
    """Das vorregistrierte 12-Varianten-Raster ({OTM 1.5/2/3%} x
    {Breite 1/2%} x {VIX-Filter an/aus}) — jede Variante wird bei
    trials_registry protokolliert, BEVOR irgendeine für die Beförderung
    ausgewählt wird. Das ist die Lektion aus dem G5-Vorfall (XSR sprang
    zwischen DSR 0.996/0.611, je nachdem ob der Modell-Zoo mitzählte, weil
    das Variantenraster nicht vorher fixiert war)."""
    from quant.research.trials_registry import log_trial
    logged = []
    for otm in OTM_GRID:
        for width in WIDTH_GRID:
            for vf in VIX_FILTER_GRID:
                label = f"otm{otm}_w{width}_vix{'on' if vf else 'off'}"
                df = simulate(otm, width, vf)
                if len(df) < 20:
                    print(f"{label}: nur {len(df)} Wochen — überspringe")
                    continue
                r = weekly_returns(df)
                d = log_trial(family="OPTPREM", returns=r, variant=label,
                             ann=52, config={"otm": otm, "width": width,
                                             "vix_filter": vf})
                logged.append({"variant": label, **d})
    return pd.DataFrame(logged)


def run():
    """Unveränderter Einzellauf mit den bisherigen Default-Parametern —
    behält die ursprüngliche `--run`-Semantik für Ad-hoc-Checks."""
    df = simulate(OTM_SHORT, WIDTH, vix_filter=False)
    if df.empty:
        print("no fills — options bar data too sparse for these strikes")
        return
    print(f"\n{len(df)} weekly spreads with data")

    def block(label, sub):
        if len(sub) < 10:
            return
        wins = (sub.pnl > 0).mean()
        rr = sub.ret_on_risk
        ann = rr.mean() * 52
        print(f"{label:32s} n={len(sub):4d}  win={wins:4.0%}  "
              f"avg P&L/risk={rr.mean()*100:+5.1f}%  ann≈{ann*100:+6.0f}% "
              f"worst wk={rr.min()*100:+.0f}%")

    block("all weeks", df)
    for u in UNDERLYINGS:
        block(f"  {u} only", df[df.u == u])
    df.to_parquet("quant/_staging/options_phase_a.parquet")
```

Modify the `if __name__ == "__main__":` block at the bottom:

```python
if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    p.add_argument("--variants", action="store_true")
    args = p.parse_args()
    if args.variants:
        out = run_all_variants()
        print(out[["variant", "sharpe_net", "cagr_net", "dsr"]]
              .to_string(index=False))
    elif args.run:
        run()
    else:
        p.print_help()
        sys.exit(1)
```

- [ ] **Step 4: Run the test script again to verify it passes**

Run the exact script from Step 1. Expected: `OK`.

- [ ] **Step 5: Run the full variant grid against real data**

```bash
python3 -m quant.research.options_phase_a --variants
```

Expected: up to 12 rows (fewer if some variants have <20 weeks of fills —
that prints its own skip message, not a crash), each logged into
`quant.trials_registry` under `family="OPTPREM"`. Confirm with:

```bash
python3 -m quant.research.trials_registry --report
```

`OPTPREM` should now appear in the family list with its own within-family
Sharpe variance — this is what makes the eventual G5 (DSR) computation for
whichever variant gets carried into Phase 2 honest instead of discretionary.

- [ ] **Step 6: Commit**

```bash
git add quant/research/options_phase_a.py
git commit -m "quant: OPTPREM-Variantenraster vorregistrieren (12 Varianten)

{OTM 1.5/2/3%} x {Breite 1/2%} x {VIX-Filter an/aus}, jede Variante bei
trials_registry protokolliert (family=OPTPREM) — sd_trials entsteht jetzt
aus echten, vorregistrierten Läufen statt nachträglicher Auswahl. Design-Spec
2026-07-30, Hebel 3. Noch keine Discovery-Gates, keine Ausführungsschicht."
```

---

## Self-Review

**Spec coverage:** Hebel 1 (Diagnose vor Fix) → Task 1. Hebel 2 (MERGARB
Daten + Signal + R9-Benchmark-Vorbereitung) → Tasks 2+3 (die
Small-/Mid-Cap-Benchmark-Anwendung selbst ist eine G7/Phase-2-Aktivität in
`discovery.py`, hier nur die Rohdaten + Renditeserie). Hebel 3 OPTPREM
(vorregistriertes Raster) → Task 4. OPTCONV und die Ausführungsschicht sind
laut Spec bewusst NICHT Teil von Phase 1 — kein Task dafür, korrekt.

**Placeholder scan:** keine TBD/TODO; jeder Code-Block ist vollständig,
keine "ähnlich wie oben"-Verweise.

**Type consistency:** `returns(**params) -> pd.Series` und
`live_weights() -> tuple[dict, str]` in Task 3 matchen exakt die Signaturen,
die `discovery.evaluate()` bei `cand["implementierung"]` bzw.
`cand["live_signal"]` aufruft (siehe `quant/research/discovery.py:211-220`
und `:298-304`) — wichtig, weil Phase 2 diese Funktionen unverändert in
`hypothesis_queue.yaml` einträgt.

**Scope:** vier unabhängige Tasks, jeder für sich lauffähig und wörtlich
das, was die Spec für Phase 1 vorsieht — keine Vorgriffe auf G0-G8, die
Options-Ausführungsschicht oder OPTCONV.
