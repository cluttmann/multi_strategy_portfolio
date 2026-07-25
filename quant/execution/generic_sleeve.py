"""Generischer Executor — handelt jede Spec aus dem Sleeve-Register.

    python3 -m quant.execution.generic_sleeve --all [--dry-run]
    python3 -m quant.execution.generic_sleeve --sleeve dtrd

Damit ist "validiert" gleichbedeutend mit "handelbar": ein neuer Sleeve
braucht nur einen Eintrag in `quant.sleeves.registry`, keinen neuen Code.
Alle Schutzmechanismen (Handelstag-Guard, Burn-in-Skalierung, Risk-Gates,
Broker-Wahrheit statt Ledger, Telegram) gelten automatisch.
"""

import argparse
import sys

from quant.config import BOT_TICKERS
from quant.execution import broker, ledger, risk
from quant.execution.guard import guard_or_exit
from quant.execution.telegram import notify
from quant.sleeves.registry import REGISTRY, rebalance_today


def rebalance(name: str, dry_run: bool = False):
    spec = REGISTRY[name]
    burn = guard_or_exit(name)
    if not rebalance_today(spec):
        print(f"{name}: heute kein Rebalance-Tag ({spec.freq})")
        return
    acct = broker.account()
    equity = float(acct["equity"])
    scale = risk.drawdown_scale(equity) * burn
    budget = equity * spec.alloc * scale

    weights, why = spec.signal()
    weights = {s: w for s, w in weights.items() if s not in BOT_TICKERS}

    state = ledger.get_sleeve(name)
    # Broker-Positionen sind die Wahrheit (Auktions-Teilfüllungen erscheinen
    # als "expired" und landen sonst nie im Ledger → Verdopplungsgefahr).
    actual = broker.positions()
    known = set(state.get("symbol_universe") or []) | set(weights) | \
        set((state.get("positions") or {}).keys())
    held = {s: int(q) for s, q in actual.items() if s in known and q != 0}

    syms = sorted(set(weights) | set(held))
    prices = broker.latest_prices(syms) if syms else {}
    orders, gross = [], 0.0
    for s in syms:
        px = prices.get(s) or 0.0
        tgt = int((budget * weights.get(s, 0.0)) // px) if px > 0 else 0
        delta = tgt - held.get(s, 0)
        if delta == 0:
            continue
        notional = abs(delta) * px
        ok, msg = risk.check_order(s, notional, name, equity, gross + notional)
        if not ok:
            notify(f"{name.upper()}: {s} blockiert — {msg}")
            continue
        orders.append((s, delta, tgt))
        gross += notional

    for i, (s, delta, tgt) in enumerate(orders):
        side = "buy" if delta > 0 else "sell"
        if dry_run:
            print(f"[dry] {spec.tif} {side} {abs(delta)} {s} (Ziel {tgt})")
        else:
            broker.submit_order(s, abs(delta), side, spec.tif, name, i)

    if not dry_run:
        new_pos = {s: t for s, _, t in orders if t > 0}
        keep = {s: q for s, q in held.items()
                if s not in {o[0] for o in orders} and q > 0}
        ledger.set_sleeve(name, {**state, "positions": {**keep, **new_pos},
                                 "last_reason": why,
                                 "symbol_universe": sorted(known)})
    notify(f"{name.upper()}: {why} | {len(orders)} {spec.tif}-Orders, "
           f"Budget ${budget:,.0f}" + (" [DRY RUN]" if dry_run else ""))
    print(f"{name}: {why} | {len(orders)} Orders, Budget ${budget:,.0f}")


def reconcile(name: str):
    state = ledger.get_sleeve(name)
    held = dict(state.get("positions") or {})
    for s, q in broker.sleeve_fills_today(name).items():
        held[s] = held.get(s, 0) + q
    actual = broker.positions()
    held = {s: int(actual[s]) for s in held if actual.get(s, 0) != 0}
    ledger.set_sleeve(name, {**state, "positions": held})
    notify(f"{name.upper()} reconcile: {held or 'flat'}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--sleeve", choices=list(REGISTRY))
    p.add_argument("--all", action="store_true")
    p.add_argument("--reconcile", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    names = list(REGISTRY) if a.all else ([a.sleeve] if a.sleeve else [])
    if not names:
        p.print_help(); sys.exit(1)
    for n in names:
        try:
            reconcile(n) if a.reconcile else rebalance(n, a.dry_run)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"{n}: FEHLER {e}")
