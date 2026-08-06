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
import datetime as dt
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
    from quant.execution.guard import guard_or_exit
    burn = guard_or_exit(SLEEVE)
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
    scale = risk.drawdown_scale(equity) * burn
    budget = equity * SLEEVE_ALLOC * scale
    # `stand` ist das Frische-Siegel, das enter() prüft — ohne es kann enter()
    # einen Altplan nicht von einem heutigen unterscheiden (s. Kommentar dort).
    plan = {"picks": picks, "budget": round(budget, 2), "scale": scale,
            "n_gated": len(gated), "burn_in": burn,
            "stand": str(dt.date.today())}
    print(f"plan: {plan}")
    if not dry_run:
        ledger.set_sleeve(SLEEVE, {**ledger.get_sleeve(SLEEVE), "plan": plan})
    notify(f"ONX decide: {len(gated)} gated, picks {picks}, "
           f"budget ${budget:,.0f} (scale {scale})"
           + (" [DRY RUN]" if dry_run else ""))


def enter(dry_run: bool):
    # PAUSE-GATE — fehlte bis 2026-08-06 und war ein echter Kapitalfehler.
    # decide() prüft die Pause, enter() prüfte sie NICHT. Folge: seit der
    # ONX-Pause am 2026-07-25 hat decide() den Plan nie mehr aktualisiert,
    # während enter() JEDEN Handelstag denselben veralteten plan.picks
    # nachgekauft hat (LABU/UDOW/FAS/DPST/YINN/DFEN/DRN/CURE — 3x gehebelte
    # Bull-ETFs). Zusammen mit dem reconcile()-Bug unten wuchs so eine
    # unverwaltete Long-Position von ~25 % der Equity in einem PAUSIERTEN
    # Sleeve. Ein pausierter Sleeve darf niemals neue Positionen eröffnen.
    from quant.execution.guard import guard_or_exit
    guard_or_exit(SLEEVE)
    state = ledger.get_sleeve(SLEEVE)
    plan = state.get("plan") or {}
    picks, budget = plan.get("picks") or [], float(plan.get("budget") or 0)
    if not picks or budget <= 0:
        notify("ONX enter: no plan/budget — standing down (fail-closed)")
        return
    # Ein Plan, den decide() heute nicht geschrieben hat, ist ein Altbestand.
    # Auf veralteten Picks zu handeln ist schlimmer als nicht zu handeln.
    if str(plan.get("stand") or "") != str(dt.date.today()):
        notify(f"ONX enter: Plan ist nicht von heute "
               f"(stand={plan.get('stand')!r}) — standing down (fail-closed)")
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
    # BROKER-WAHRHEIT als letzte Instanz (Fund 2026-08-06). Vorher galt: was
    # das Ledger nicht kennt, wird nicht verkauft — und weil reconcile() den
    # Positionsstand täglich auf {} überschrieb, blieben 8 ONX-Positionen
    # (~25 % der Equity) dauerhaft unverkäuflich im Konto liegen. Ein
    # Ausstiegspfad darf sich NIE allein auf eigene Buchführung verlassen:
    # was der Broker in ONX-eigenen Symbolen hält, muss auch geschlossen
    # werden können. Quelle der Symbolliste ist der letzte bekannte Plan
    # plus das Ledger — kein Blankoscheck auf fremde Positionen.
    owned = set(held) | set((state.get("plan") or {}).get("picks") or [])
    try:
        actual = broker.positions()
    except Exception as e:  # noqa: BLE001 — ohne Broker-Wahrheit nicht raten
        actual = {}
        notify(f"ONX exit: Broker-Positionen nicht lesbar ({e})")
    for s in owned:
        q = int(actual.get(s, 0))
        if q > 0 and q != int(held.get(s, 0)):
            if s not in held:
                notify(f"ONX exit: {s} x{q} beim Broker, aber nicht im "
                       f"Ledger — wird trotzdem geschlossen (Drift)")
            held[s] = q
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
    """After the close: record fills into the ledger as positions.

    KORRIGIERT 2026-08-06: die alte Version baute `positions` AUSSCHLIESSLICH
    aus den HEUTIGEN Fills. An jedem Tag ohne ONX-Fill (also jedem Tag seit
    der Pause am 2026-07-25) schrieb sie damit `positions: {}` und löschte den
    Bestandsnachweis — worauf exit_() "nothing held" meldete und nie verkaufte.
    Ergebnis: ~25 % der Equity in unverwalteten 3x-Bull-ETFs. Jetzt identisch
    zum Muster in generic_sleeve.reconcile(): Bestand + heutige Fills, dann
    gegen die Broker-Wahrheit validiert.
    """
    state = ledger.get_sleeve(SLEEVE)
    held = {s: int(q) for s, q in (state.get("positions") or {}).items()}
    for s, q in broker.sleeve_fills_today(SLEEVE).items():
        held[s] = held.get(s, 0) + int(q)
    # Plan-Picks mitführen, damit ein zuvor verlorener Bestand wieder
    # eingefangen wird statt für immer unsichtbar zu bleiben.
    for s in (state.get("plan") or {}).get("picks") or []:
        held.setdefault(s, 0)
    actual = broker.positions()
    pos = {s: int(actual[s]) for s in held
           if int(actual.get(s, 0) or 0) > 0}
    ledger.set_sleeve(SLEEVE, {**state, "positions": pos})
    notify(f"ONX reconcile: {pos or 'flat'}")


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
