"""Live-Kostenmessung — misst die Zahl, an der XSR hängt.

    python3 -m quant.ops.cost_monitor --check [--days 5]

WARUM DRINGEND: Die Foundation-Model-Studie (Rahimikia et al., 18M
Beobachtungen) zeigt, dass tägliche Querschnitts-Long-Short-Strategien bei
20bps Kosten ALLE Sharpe-negativ werden — inklusive Gradient-Boosting-
Baselines. Unser XSR nettet 0.60 @5bp, 0.22 @10bp und wäre bei 20bp negativ.
Der Burn-in muss also die EFFEKTIVEN Kosten messen, nicht annehmen.

METHODE: Jede gefüllte QNT-Order wird gegen ihren Auktions-Benchmark
verglichen — `opg`-Orders gegen den offiziellen Eröffnungskurs, `cls`-Orders
gegen den offiziellen Schlusskurs (beide aus eod_bars). Slippage in bps,
vorzeichenrichtig als KOSTEN (positiv = wir haben schlechter gefüllt):
    Kauf:    (fill − benchmark) / benchmark
    Verkauf: (benchmark − fill) / benchmark

Kill-Kriterium G10: EWMA-Slippage > 1.5× Kostenmodell (5bp) → Telegram-Alarm
und Sleeve-Review. Ergebnis nach BigQuery (quant.fill_costs).
"""

import argparse
import datetime as dt
import sys

import numpy as np
import pandas as pd
import requests
from google.cloud import bigquery

from quant.config import (ALPACA_KEY_PAPER, ALPACA_PAPER_BASE,
                          ALPACA_SECRET_PAPER, BQ_DATASET, GCP_PROJECT,
                          ORDER_TAG_PREFIX)
from quant.data.bq import ensure_table, load_df, query

T_COSTS = f"{GCP_PROJECT}.{BQ_DATASET}.fill_costs"
H = {"APCA-API-KEY-ID": ALPACA_KEY_PAPER,
     "APCA-API-SECRET-KEY": ALPACA_SECRET_PAPER}

COST_MODEL_BPS = 5.0          # Annahme im Backtest
G10_MULTIPLE = 1.5            # Alarmschwelle laut DESIGN.md G10
MIN_NOTIONAL_FOR_SLIPPAGE = 500.0
# Slippage auf Kleinstfüllungen ist Tick-Rauschen, keine Kostenschätzung:
# 1 Aktie AEM zu 145,73 vs. Eröffnung 145,28 sind 31bp, aber nur ein Tick.
# Für die Kostenaussage werden nur Fills >= MIN_NOTIONAL berücksichtigt;
# die Füllquote wird dagegen über ALLE Orders gerechnet.

SCHEMA = [
    bigquery.SchemaField("fill_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("sleeve", "STRING"),
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("side", "STRING"),
    bigquery.SchemaField("tif", "STRING"),
    bigquery.SchemaField("qty", "FLOAT64"),
    bigquery.SchemaField("qty_ordered", "FLOAT64"),
    bigquery.SchemaField("fill_rate", "FLOAT64"),
    bigquery.SchemaField("fill_price", "FLOAT64"),
    bigquery.SchemaField("benchmark", "FLOAT64"),
    bigquery.SchemaField("benchmark_kind", "STRING"),
    bigquery.SchemaField("slippage_bps", "FLOAT64"),
    bigquery.SchemaField("notional", "FLOAT64"),
    bigquery.SchemaField("order_id", "STRING"),
]


def fetch_fills(days: int) -> pd.DataFrame:
    after = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows, token = [], None
    while True:
        params = {"status": "closed", "limit": 500, "after": f"{after}T00:00:00Z",
                  "direction": "asc"}
        if token:
            params["after"] = token
        r = requests.get(f"{ALPACA_PAPER_BASE}/v2/orders", headers=H,
                         params=params, timeout=60)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 500:
            break
        token = batch[-1]["submitted_at"]
    out = []
    for o in rows:
        coid = o.get("client_order_id") or ""
        if not coid.startswith(f"{ORDER_TAG_PREFIX}-"):
            continue          # fremde Orders (ETF-Bot) ignorieren
        # Alpaca-Semantik (verifiziert 2026-07-25): status="expired" bedeutet,
        # dass der NICHT ausgeführte REST der Auktionsorder verfallen ist —
        # Teilfüllungen tragen also status=expired mit filled_qty>0 (z.B. DRN:
        # 79 bestellt, 29 gefüllt). status="filled" nur bei 100 %. Deshalb wird
        # auf filled_qty geprüft, nie auf status. Die Füllquote (filled/qty)
        # ist die eigentlich wichtige Kennzahl — sie war 2026-07-24 nur 20 %
        # für XSR (opg) und 59 % für ONX (cls).
        if not o.get("filled_avg_price") or float(o.get("filled_qty") or 0) <= 0:
            continue
        parts = coid.split("-")
        out.append({
            "order_id": o["id"],
            "sleeve": parts[1].lower() if len(parts) > 1 else "?",
            "symbol": o["symbol"], "side": o["side"],
            "tif": o.get("time_in_force"),
            "qty": float(o["filled_qty"]),
            "qty_ordered": float(o["qty"]),
            "fill_price": float(o["filled_avg_price"]),
            "fill_date": pd.to_datetime(o["filled_at"]).date(),
        })
    return pd.DataFrame(out)


def attach_benchmarks(df: pd.DataFrame) -> pd.DataFrame:
    """Offizieller Auktionspreis je (Symbol, Tag) aus eod_bars."""
    if df.empty:
        return df
    syms = ", ".join(repr(s) for s in df["symbol"].unique())
    lo, hi = df["fill_date"].min(), df["fill_date"].max()
    bars = query(f"""
      SELECT date AS fill_date, symbol, open, close
      FROM `{GCP_PROJECT}.{BQ_DATASET}.eod_bars`
      WHERE symbol IN ({syms}) AND date BETWEEN '{lo}' AND '{hi}'""")
    if bars.empty:
        print("keine eod_bars für die Fill-Tage (EOD noch nicht geladen?)")
        return pd.DataFrame()
    bars["fill_date"] = pd.to_datetime(bars["fill_date"]).dt.date
    m = df.merge(bars, on=["symbol", "fill_date"], how="inner")
    # opg → Eröffnungsauktion, cls → Schlussauktion; sonst kein Benchmark
    m["benchmark_kind"] = np.where(m["tif"] == "opg", "official_open",
                            np.where(m["tif"] == "cls", "official_close", None))
    m["benchmark"] = np.where(m["tif"] == "opg", m["open"],
                       np.where(m["tif"] == "cls", m["close"], np.nan))
    m = m.dropna(subset=["benchmark"])
    m = m[m["benchmark"] > 0]
    sign = np.where(m["side"] == "buy", 1.0, -1.0)
    m["slippage_bps"] = sign * (m["fill_price"] - m["benchmark"]) \
        / m["benchmark"] * 1e4
    m["notional"] = m["qty"] * m["fill_price"]
    m["fill_rate"] = m["qty"] / m["qty_ordered"].clip(lower=1)
    return m[[c.name for c in SCHEMA]]


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
    Buckets mit n<2 (nur eine Füllung) werden verworfen — sonst dominiert
    ein einzelner Fill den Bucket-Mittelwert."""
    bins = [0, 1, 5, 10, np.inf]
    labels = ["<1%", "1-5%", "5-10%", ">10%"]
    m = m.copy()
    m["adv_bucket"] = pd.cut(m["participation_pct"], bins=bins, labels=labels)
    rows = []
    for b in labels:
        g = m[m["adv_bucket"] == b]
        if len(g) < 2:
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


def check(days: int = 5, alert: bool = True):
    ensure_table(T_COSTS, SCHEMA, partition_field="fill_date",
                 clustering=["sleeve", "symbol"])
    fills = fetch_fills(days)
    if fills.empty:
        print(f"keine QNT-Fills in den letzten {days} Tagen "
              f"(Burn-in noch nicht gestartet?)")
        return
    m = attach_benchmarks(fills)
    if m.empty:
        print(f"{len(fills)} Fills, aber kein Auktions-Benchmark zuordenbar")
        return

    # idempotent: nur neue order_ids anfügen
    try:
        seen = set(query(f"SELECT DISTINCT order_id FROM `{T_COSTS}`")["order_id"])
        m = m[~m["order_id"].isin(seen)]
    except Exception:  # noqa: BLE001
        pass
    if len(m):
        load_df(T_COSTS, m, schema=SCHEMA)

    # Bericht über das gesamte Fenster (auch bereits geladene Fills)
    hist = query(f"""
      SELECT sleeve, symbol, side, slippage_bps, notional, fill_date,
             qty, qty_ordered, fill_rate
      FROM `{T_COSTS}`
      WHERE fill_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {days * 3} DAY)""")
    if hist.empty:
        print("noch keine Historie")
        return
    print(f"{'Sleeve':8s} {'Fills':>6s} {'Füllquote':>10s} "
          f"{'Slippage(>=500$)':>17s} {'n_rel':>6s}")
    alarms = []
    for sl, g in hist.groupby("sleeve"):
        fr = g["qty"].sum() / max(g["qty_ordered"].sum(), 1)
        rel = g[g["notional"] >= MIN_NOTIONAL_FOR_SLIPPAGE]
        w = (np.average(rel["slippage_bps"], weights=rel["notional"])
             if len(rel) else float("nan"))
        flag = ""
        if fr < 0.5:
            flag = " ⚠ Füllquote"
            alarms.append(f"{sl}: Füllquote nur {fr:.0%}")
        if len(rel) >= 5 and w > COST_MODEL_BPS * G10_MULTIPLE:
            flag += " ⚠ G10"
            alarms.append(f"{sl}: {w:.1f}bp Slippage")
        wtxt = f"{w:15.1f}bp" if not np.isnan(w) else f"{'—':>17s}"
        print(f"{sl:8s} {len(g):6d} {fr:9.0%} {wtxt} {len(rel):6d}{flag}")
    print("\nLesehilfen:")
    print("  Füllquote = gefüllte / bestellte Stück. Auktionsorders füllen im")
    print("  Paper-Konto oft nur teilweise; der Rest läuft als 'expired' aus.")
    print("  Eine Quote <50% heißt: der Sleeve baut sein Zielbuch nicht auf.")
    print(f"  Slippage nur über Fills >= {MIN_NOTIONAL_FOR_SLIPPAGE:.0f}$ — "
          "auf 1-2-Stück-Teilfüllungen ist sie Tick-Rauschen. Alpacas")
    print("  Paper-Engine füllt Auktionen ohnehin nicht zum offiziellen Print,")
    print("  daher sind die Absolutwerte OBERGRENZEN, keine Kostenschätzung.")

    if alert:
        from quant.execution.telegram import notify
        if alarms:
            notify("⚠️ Fill-Kosten über G10-Limit: " + " | ".join(alarms))
        else:
            notify(f"Fill-Kosten OK: {total:.1f}bp effektiv "
                   f"(Modell {COST_MODEL_BPS:.0f}bp, {len(hist)} Fills)")


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
