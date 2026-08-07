"""SEC Form 4 Insider-Transaktionen → BigQuery (via edgartools).

    python3 -m quant.data.insider_ingest --smoke        # 3 Symbole
    python3 -m quant.data.insider_ingest --backfill     # XSR-Universum

Netto-Insider-Käufe pro Symbol/Tag als v3-Feature-Block-Kandidat. edgartools
parst jedes Form 4 in ein typisiertes Ownership-Objekt (Käufe/Verkäufe,
Officer-Rolle). Wir aggregieren offene-Markt-Transaktionen (Code P/S) zu
signiertem USD-Volumen je Filing-Datum. Mehrstündiger Lauf → Hintergrund.
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import sys

import pandas as pd
from google.cloud import bigquery

from quant.config import BQ_DATASET, GCP_PROJECT
from quant.data.bq import ensure_table, load_df, query

T_INSIDER = f"{GCP_PROJECT}.{BQ_DATASET}.insider_transactions"

SCHEMA = [
    bigquery.SchemaField("date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("net_value", "FLOAT64"),   # signiert: + Kauf, - Verkauf
    bigquery.SchemaField("n_buys", "INT64"),
    bigquery.SchemaField("n_sells", "INT64"),
    bigquery.SchemaField("n_insiders", "INT64"),
]


def _identity():
    from edgar import set_identity
    set_identity("Carl Johannes carl.johannes.mail@gmail.com")


def exmap_since(sym: str, last: dict[str, str], default="2016-01-01") -> str:
    """Startdatum je Symbol: ab dem letzten geladenen Filing (minus Puffer)."""
    import datetime as _dt
    d = last.get(sym)
    if not d:
        return default
    return (_dt.date.fromisoformat(d) - _dt.timedelta(days=5)).isoformat()


def universe() -> list[str]:
    df = query("""
      SELECT symbol, COUNT(*) n FROM `trading-436516.quant.features_daily_v2`
      WHERE date >= '2016-01-01' GROUP BY symbol HAVING n > 200
      ORDER BY n DESC""")
    return list(df["symbol"])


def fetch_symbol(sym: str, since="2016-01-01") -> pd.DataFrame | None:
    from edgar import Company
    try:
        c = Company(sym)
        fils = c.get_filings(form="4")
        if fils is None or len(fils) == 0:
            return None
    except Exception:  # noqa: BLE001
        return None
    rows = []
    for f in fils:
        try:
            fd = f.filing_date
            if str(fd) < since:
                continue
            o = f.obj()
            s = o.get_ownership_summary()
            nv = float(getattr(s, "net_value", 0) or 0)
            if nv == 0:
                continue  # Options-Grants/Derivate ohne offene-Markt-Wert
            act = str(getattr(s, "primary_activity", "") or "").lower()
            is_buy = nv > 0 or "purchase" in act or "buy" in act
            rows.append({
                "date": pd.to_datetime(fd).date(), "symbol": sym,
                "net_value": nv,
                "n_buys": 1 if is_buy else 0,
                "n_sells": 0 if is_buy else 1,
                "n_insiders": 1,
            })
        except Exception:  # noqa: BLE001
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows)
    return df.groupby(["date", "symbol"], as_index=False).agg(
        net_value=("net_value", "sum"), n_buys=("n_buys", "sum"),
        n_sells=("n_sells", "sum"), n_insiders=("n_insiders", "sum"))


def backfill(smoke=False, workers=6):
    _identity()
    ensure_table(T_INSIDER, SCHEMA, partition_field="date",
                 clustering=["symbol"])
    syms = universe()
    if smoke:
        syms = ["NVDA", "AAPL", "TSLA"]
    # Datums-inkrementell: bereits geladene Symbole werden NICHT übersprungen,
    # sondern ab ihrem letzten Filing nachgeladen (der alte Symbol-Skip ließ
    # die Tabelle dauerhaft veralten — gefunden 2026-07-25, 12 Tage Lücke).
    last_by_sym: dict[str, str] = {}
    try:
        df = query(f"SELECT symbol, MAX(date) AS d FROM `{T_INSIDER}` "
                   f"GROUP BY symbol")
        last_by_sym = {r.symbol: str(r.d) for _, r in df.iterrows()}
    except Exception:  # noqa: BLE001
        pass
    todo = list(syms)
    print(f"{len(todo)} Symbole ({len(last_by_sym)} davon inkrementell)")
    buf, total, fetched = [], 0, 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_symbol, s, exmap_since(s, last_by_sym)): s
                for s in todo}
        for fut in cf.as_completed(futs):
            fetched += 1
            df = fut.result()
            if df is not None and len(df):
                buf.append(df)
            if len(buf) >= 50:
                chunk = pd.concat(buf, ignore_index=True)
                load_df(T_INSIDER, chunk, schema=SCHEMA)
                total += len(chunk); buf = []
                print(f"  {fetched}/{len(todo)} Symbole, {total:,} Zeilen",
                      flush=True)
    if buf:
        chunk = pd.concat(buf, ignore_index=True)
        load_df(T_INSIDER, chunk, schema=SCHEMA)
        total += len(chunk)
    print(f"DONE. {total:,} Insider-Zeilen aus {fetched} Symbolen")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--backfill", action="store_true")
    args = p.parse_args()
    if args.smoke:
        backfill(smoke=True)
    elif args.backfill:
        backfill()
    else:
        p.print_help()
        sys.exit(1)
