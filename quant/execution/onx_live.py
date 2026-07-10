"""ONX sleeve live executor — 3x-ETF overnight anomaly (V2 trend-gated).

    python3 -m quant.execution.onx_live --decide [--dry-run]   # ~15:45 ET
    python3 -m quant.execution.onx_live --enter  [--dry-run]   # ~15:50 ET
    python3 -m quant.execution.onx_live --exit   [--dry-run]   # ~09:15 ET

Flow (matches the validated backtest exactly):
  decide: recompute the 3x-bull universe trend gates (yesterday close vs 50d
          SMA on daily bars), pick the TOP_N gated names by dollar volume
          (liquidity cap for whole-share sizing at small equity), write the
          plan to Firestore.
  enter:  place `cls` (market-on-close) buys per plan — fills at the official
          closing auction, which is the backtest's entry price.
  exit:   place `opg` (market-on-open) sells for every ONX position — fills
          at the opening auction, the backtest's exit price.

Risk: drawdown ladder scales or halts sizing; every order passes
risk.check_order; everything logs to Telegram. Whole shares only (auction
orders reject fractional).
"""

import argparse
import sys

import numpy as np

from quant.execution import broker, ledger, risk
from quant.execution.telegram import notify
from quant.research.exotic_sleeves import alpaca_daily
from quant.research.overnight_universe import discover_universe
from quant.config import BOT_TICKERS

SLEEVE = "onx"
SLEEVE_ALLOC = 0.30   # of account equity
TOP_N = 8             # liquidity cap: EW across the N most-liquid gated names


def decide(dry_run: bool):
    univ = [s for s in discover_universe() if s not in BOT_TICKERS]
    gated = []
    for s in univ:
        try:
            df = alpaca_daily(s, "2024-01-01")
            if len(df) < 60:
                continue
            sma50 = df["c"].rolling(50).mean().iloc[-1]
            last = df["c"].iloc[-1]
            adv = float((df["c"] * df["v"]).tail(20).mean())
            if last > sma50:
                gated.append((s, adv))
        except Exception as e:  # noqa: BLE001
            print(f"{s}: data error ({e}) — fail-closed, excluded")
    gated.sort(key=lambda x: -x[1])
    picks = [s for s, _ in gated[:TOP_N]]

    acct = broker.account()
    equity = float(acct["equity"])
    scale = risk.drawdown_scale(equity)
    budget = equity * SLEEVE_ALLOC * scale
    plan = {"picks": picks, "budget": round(budget, 2), "scale": scale,
            "n_gated": len(gated)}
    print(f"plan: {plan}")
    if not dry_run:
        ledger.set_sleeve(SLEEVE, {**ledger.get_sleeve(SLEEVE), "plan": plan})
    notify(f"ONX decide: {len(gated)} gated, picks {picks}, "
           f"budget ${budget:,.0f} (scale {scale})"
           + (" [DRY RUN]" if dry_run else ""))


def enter(dry_run: bool):
    state = ledger.get_sleeve(SLEEVE)
    plan = state.get("plan") or {}
    picks, budget = plan.get("picks") or [], float(plan.get("budget") or 0)
    if not picks or budget <= 0:
        notify("ONX enter: no plan/budget — standing down (fail-closed)")
        return
    acct = broker.account()
    equity = float(acct["equity"])
    prices = broker.latest_prices(picks)
    per_name = budget / len(picks)
    placed, gross = [], 0.0
    for i, s in enumerate(picks):
        px = prices.get(s)
        if not px:
            notify(f"ONX enter: no price for {s} — skipped (fail-closed)")
            continue
        qty = int(per_name // px)
        if qty < 1:
            continue
        notional = qty * px
        ok, why = risk.check_order(s, notional, SLEEVE, equity, gross + notional)
        if not ok:
            notify(f"ONX enter: {s} blocked — {why}")
            continue
        if dry_run:
            print(f"[dry] cls BUY {qty} {s} ≈ ${notional:,.0f}")
        else:
            broker.submit_order(s, qty, "buy", "cls", SLEEVE, i)
        gross += notional
        placed.append({"symbol": s, "qty": qty, "approx": round(notional, 2)})
    if not dry_run:
        ledger.set_sleeve(SLEEVE, {**state, "open_orders": placed})
    notify(f"ONX enter: {len(placed)} cls buys, gross ≈ ${gross:,.0f}"
           + (" [DRY RUN]" if dry_run else ""))


def exit_(dry_run: bool):
    state = ledger.get_sleeve(SLEEVE)
    held = state.get("positions") or {}
    if not held:
        # Reconcile from broker fills of yesterday's cls orders.
        held = {}
        pos = broker.positions()
        for o in state.get("open_orders") or []:
            s = o["symbol"]
            if s in pos and pos[s] > 0:
                held[s] = min(o["qty"], int(pos[s]))
    if not held:
        notify("ONX exit: nothing held")
        return
    for i, (s, qty) in enumerate(held.items()):
        if dry_run:
            print(f"[dry] opg SELL {qty} {s}")
        else:
            broker.submit_order(s, qty, "sell", "opg", SLEEVE, i)
    if not dry_run:
        ledger.set_sleeve(SLEEVE, {**state, "positions": {}, "open_orders": []})
    notify(f"ONX exit: opg sells for {list(held)}"
           + (" [DRY RUN]" if dry_run else ""))


def reconcile():
    """After the close: record fills into the ledger as positions."""
    state = ledger.get_sleeve(SLEEVE)
    fills = {}
    for o in broker.orders_today(SLEEVE):
        if o["side"] == "buy" and o["status"] == "filled":
            fills[o["symbol"]] = fills.get(o["symbol"], 0) + int(float(o["filled_qty"]))
    ledger.set_sleeve(SLEEVE, {**state, "positions": fills})
    notify(f"ONX reconcile: positions {fills}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--decide", action="store_true")
    p.add_argument("--enter", action="store_true")
    p.add_argument("--exit", dest="exit_", action="store_true")
    p.add_argument("--reconcile", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.decide:
        decide(args.dry_run)
    elif args.enter:
        enter(args.dry_run)
    elif args.exit_:
        exit_(args.dry_run)
    elif args.reconcile:
        reconcile()
    else:
        p.print_help()
        sys.exit(1)
