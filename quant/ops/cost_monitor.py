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

SCHEMA = [
    bigquery.SchemaField("fill_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("sleeve", "STRING"),
    bigquery.SchemaField("symbol", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("side", "STRING"),
    bigquery.SchemaField("tif", "STRING"),
    bigquery.SchemaField("qty", "FLOAT64"),
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
        if o.get("status") != "filled" or not o.get("filled_avg_price"):
            continue
        parts = coid.split("-")
        out.append({
            "order_id": o["id"],
            "sleeve": parts[1].lower() if len(parts) > 1 else "?",
            "symbol": o["symbol"], "side": o["side"],
            "tif": o.get("time_in_force"),
            "qty": float(o["filled_qty"]),
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
    return m[[c.name for c in SCHEMA]]


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
      SELECT sleeve, symbol, side, slippage_bps, notional, fill_date
      FROM `{T_COSTS}`
      WHERE fill_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {days * 3} DAY)""")
    if hist.empty:
        print("noch keine Historie")
        return
    print(f"{'Sleeve':8s} {'Fills':>6s} {'Slippage (notional-gew.)':>26s} "
          f"{'Median':>8s} {'p90':>8s}")
    alarms = []
    for sl, g in hist.groupby("sleeve"):
        w = np.average(g["slippage_bps"], weights=g["notional"].clip(lower=1))
        med, p90 = g["slippage_bps"].median(), g["slippage_bps"].quantile(0.9)
        flag = ""
        if w > COST_MODEL_BPS * G10_MULTIPLE:
            flag = " ⚠ G10"
            alarms.append(f"{sl}: {w:.1f}bp > {COST_MODEL_BPS * G10_MULTIPLE:.1f}bp Limit")
        print(f"{sl:8s} {len(g):6d} {w:>24.1f}bp {med:7.1f}bp {p90:7.1f}bp{flag}")
    total = np.average(hist["slippage_bps"],
                       weights=hist["notional"].clip(lower=1))
    print(f"\nGESAMT: {total:.1f}bp effektive Slippage vs. "
          f"{COST_MODEL_BPS:.0f}bp Kostenmodell "
          f"({total / COST_MODEL_BPS:.1f}× der Annahme)")
    print("Lesehilfe: Auktionsorders sollten ≈0bp Slippage gegen den "
          "offiziellen Print zeigen. Deutlich positive Werte heißen, dass wir "
          "den Print nicht bekommen — dann greift die Kostenwand.")

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
    p.add_argument("--days", type=int, default=5)
    p.add_argument("--no-alert", action="store_true")
    a = p.parse_args()
    if not a.check:
        p.print_help()
        sys.exit(1)
    check(days=a.days, alert=not a.no_alert)
