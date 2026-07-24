"""Executors für die ETF-Sleeves VOLC und EOMT (Schlussauktion).

    python3 -m quant.execution.etf_sleeves --sleeve volc --rebalance [--dry-run]
    python3 -m quant.execution.etf_sleeves --sleeve eomt --rebalance [--dry-run]
    python3 -m quant.execution.etf_sleeves --sleeve all  --rebalance

Beide Sleeves treffen eine Tagesentscheidung und halten liquide ETFs, deshalb
teilen sie einen Executor: Zielposition berechnen → Delta gegen den Firestore-
Bestand → `cls`-Orders (Market-on-Close, offizieller Auktionsprint, identisch
zum Backtest-Preis).

VOLC (Sharpe 0.64): SVXY long, solange VIX3M/VIX-Contango > 3 %, sonst flat.
  Signal aus dem VORTAGS-Schluss (wie im Backtest per shift(1)) — keine
  Intraday-Schätzung, kein Look-ahead.

EOMT (Sharpe 0.87 gesamt / 0.65 Holdout): EW IEF/TLT/EDV an den letzten 5
  Handelstagen des Monats, sonst flat. Kalender via NYSE (pandas_market_
  calendars), nicht via Kalendertagen — der Effekt hängt am Handelstag.
"""

import argparse
import datetime as dt
import sys

import numpy as np
import pandas as pd

from quant.config import BOT_TICKERS
from quant.execution import broker, ledger, risk
from quant.execution.guard import guard_or_exit
from quant.execution.telegram import notify

# Sleeve-Allokationen (Anteil der Equity), Krypto ist ausgeschlossen
ALLOC = {"volc": 0.15, "eomt": 0.20}
EOMT_SYMBOLS = ["IEF", "TLT", "EDV"]
EOMT_LAST_DAYS = 5          # vorregistriert aus dem Training (k=5)
VOLC_SYMBOL = "SVXY"
VOLC_CONTANGO_MIN = 0.03


def _fred_contango() -> float | None:
    """VIX3M/VIX − 1 aus dem jüngsten verfügbaren FRED-Stand (Vortagsschluss)."""
    from quant.data.bq import query
    df = query("""
      SELECT series, value FROM `trading-436516.quant.fred_series`
      WHERE series IN ('VIXCLS','VIX3MCLS','VXVCLS')
        AND date = (SELECT MAX(date) FROM `trading-436516.quant.fred_series`
                    WHERE series = 'VIXCLS')""")
    if df.empty:
        return None
    v = dict(zip(df["series"], df["value"]))
    vix = v.get("VIXCLS")
    v3m = v.get("VIX3MCLS") or v.get("VXVCLS")
    if not vix or not v3m or vix <= 0:
        return None
    return v3m / vix - 1.0


def _eomt_in_window(today: dt.date | None = None) -> tuple[bool, int]:
    """Ist heute unter den letzten EOMT_LAST_DAYS Handelstagen des Monats?"""
    import pandas_market_calendars as mcal
    today = today or dt.date.today()
    cal = mcal.get_calendar("XNYS")
    first = today.replace(day=1)
    last = (first + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
    sched = cal.schedule(start_date=first.isoformat(), end_date=last.isoformat())
    days = [d.date() for d in sched.index]
    if today not in days:
        return False, 0
    pos_from_end = len(days) - days.index(today)      # 1 = letzter Handelstag
    return pos_from_end <= EOMT_LAST_DAYS, pos_from_end


def target_weights(sleeve: str) -> tuple[dict[str, float], str]:
    """Zielgewichte (Anteil des Sleeve-Budgets) + Begründung."""
    if sleeve == "volc":
        c = _fred_contango()
        if c is None:
            return {}, "kein VIX-Signal (fail-closed → flat)"
        if c > VOLC_CONTANGO_MIN:
            return {VOLC_SYMBOL: 1.0}, f"Contango {c:+.1%} > 3% → long SVXY"
        return {}, f"Contango {c:+.1%} ≤ 3% → flat"
    if sleeve == "eomt":
        inw, pos = _eomt_in_window()
        if not inw:
            return {}, f"Handelstag {pos} von hinten → außerhalb Fenster, flat"
        w = 1.0 / len(EOMT_SYMBOLS)
        return {s: w for s in EOMT_SYMBOLS}, \
            f"T-{pos - 1} vor Monatsende → EW {EOMT_SYMBOLS}"
    raise ValueError(sleeve)


def rebalance(sleeve: str, dry_run: bool):
    burn = guard_or_exit(sleeve)
    acct = broker.account()
    equity = float(acct["equity"])
    scale = risk.drawdown_scale(equity) * burn
    budget = equity * ALLOC[sleeve] * scale

    weights, why = target_weights(sleeve)
    weights = {s: w for s, w in weights.items() if s not in BOT_TICKERS}
    state = ledger.get_sleeve(sleeve)
    held = {k: int(v) for k, v in (state.get("positions") or {}).items()}

    syms = sorted(set(weights) | set(held))
    prices = broker.latest_prices(syms) if syms else {}
    orders, gross = [], 0.0
    for s in syms:
        px = prices.get(s) or 0.0
        tgt_qty = int((budget * weights.get(s, 0.0)) // px) if px > 0 else 0
        delta = tgt_qty - held.get(s, 0)
        if delta == 0:
            continue
        notional = abs(delta) * px
        ok, msg = risk.check_order(s, notional, sleeve, equity, gross + notional)
        if not ok:
            notify(f"{sleeve.upper()}: {s} blockiert — {msg}")
            continue
        orders.append((s, delta, tgt_qty))
        gross += notional

    for i, (s, delta, tgt) in enumerate(orders):
        side = "buy" if delta > 0 else "sell"
        if dry_run:
            print(f"[dry] cls {side} {abs(delta)} {s} (Ziel {tgt})")
        else:
            broker.submit_order(s, abs(delta), side, "cls", sleeve, i)

    if not dry_run:
        new_pos = {s: tgt for s, _, tgt in orders if tgt > 0}
        keep = {s: q for s, q in held.items()
                if s not in {o[0] for o in orders} and q > 0}
        ledger.set_sleeve(sleeve, {**state, "positions": {**keep, **new_pos},
                                   "last_reason": why})
    notify(f"{sleeve.upper()} rebalance: {why} | {len(orders)} cls-Orders, "
           f"Budget ${budget:,.0f} (scale {scale:.2f})"
           + (" [DRY RUN]" if dry_run else ""))
    print(f"{sleeve}: {why} | {len(orders)} Orders, Budget ${budget:,.0f}")


def reconcile(sleeve: str):
    state = ledger.get_sleeve(sleeve)
    held = dict(state.get("positions") or {})
    for o in broker.orders_today(sleeve):
        if o["status"] != "filled":
            continue
        q = int(float(o["filled_qty"]))
        s = o["symbol"]
        held[s] = held.get(s, 0) + (q if o["side"] == "buy" else -q)
    held = {s: q for s, q in held.items() if q > 0}
    ledger.set_sleeve(sleeve, {**state, "positions": held})
    notify(f"{sleeve.upper()} reconcile: {held or 'flat'}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--sleeve", choices=["volc", "eomt", "all"], required=True)
    p.add_argument("--rebalance", action="store_true")
    p.add_argument("--reconcile", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    sleeves = ["volc", "eomt"] if a.sleeve == "all" else [a.sleeve]
    if a.rebalance:
        for s in sleeves:
            rebalance(s, a.dry_run)
    elif a.reconcile:
        for s in sleeves:
            reconcile(s)
    else:
        p.print_help()
        sys.exit(1)
