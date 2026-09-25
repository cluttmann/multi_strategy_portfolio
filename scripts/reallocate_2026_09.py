"""Einmalige Umschichtung auf AAA 21,25 / World-Trend 21,25 / Mix8 42,5 /
S&P-Trend 15 (2026-09-23).

Jede Sleeve bestimmt ihre Zielzusammensetzung mit genau der Logik, die sie
danach live fuehrt (plan_rotator_weights, world_trend_signals). Ihr Budget ist
Gewicht x Konto-Equity. Gehandelt wird ueber das ganze Konto nur die
Differenz: was nach dem Wechsel wieder gebraucht wird (z.B. QLD, das von Dual
Momentum an Mix8 geht), bleibt liegen. Verkaufen und zurueckkaufen wuerde nur
Gewinne realisieren.

  python3 scripts/reallocate_2026_09.py --env live --dry-run
  python3 scripts/reallocate_2026_09.py --env live --execute --confirm REALLOCATE_2026_09_LIVE

Sicherungen wie im F4-Skript: Token, Marktzeit, keine offenen Orders, Snapshot
vor und nach, atomar geschriebenes Audit, jeder Fill wird strikt abgewartet.
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as bot
from scripts.retire_f4_live import capture_snapshot, get_market_clock, strict_wait_for_fill, write_audit

CONFIRMATION_TOKEN = "REALLOCATE_2026_09_LIVE"
CASH_BUFFER = 0.0        # Carl will 100 % auf Zielallokation. Kursbewegungen zwischen Plan und
                         # Fill ergeben ein paar Dollar Rest oder Mini-Saldo; die Margin bleibt
                         # erlaubt, und der Orchestrator investiert Reste am 1. des Monats.
TOLERANCE = 5.0          # USD; kleinere Differenzen werden nicht gehandelt
RETIRED_DOCS = {"dual_momentum": ("SPUU", "QLD", "EFO", "BND")}


def sleeve_weights(api):
    """Zielgewichte je Sleeve, ermittelt wie im Live-Lauf. Raises bei Datenfehlern."""
    out = {}
    for key, cfg in (("aaa", bot.aaa_config), ("mix8", bot.mix8_config)):
        plan = bot.plan_rotator_weights(api, cfg)
        w = {s: plan["weights"].get(s, 0.0) for s in bot.STRATEGY_SYMBOLS[key]}
        w[cfg["defensive"]] = plan["cash_weight"]
        out[key] = {"weights": w, "plan": plan}
    for cfg in bot.TREND_SLEEVES:
        signals = bot.trend_signals(cfg)
        out[cfg["strategy_key"]] = {"weights": bot.world_trend_target_weights(cfg, signals), "signals": signals}
    return out


def build_targets(equity, sleeves, margin=0.0):
    """Ziel-USD je Ticker: Budget der Sleeve x Gewicht. `margin` ist der vom
    Margin-Gate freigegebene Betrag (0, wenn die Gates zu sind)."""
    investable = equity * (1 - CASH_BUFFER) + margin
    targets = {}
    for key, (allo, _) in bot.SLEEVES.items():
        budget = investable * bot.strategy_allocations[allo]
        sleeves[key]["budget"] = budget
        for sym, w in sleeves[key]["weights"].items():
            targets[sym] = targets.get(sym, 0.0) + budget * w
    return investable, targets


def plan_orders(positions, targets, prices):
    """Verkaeufe und Kaeufe fuer das ganze Konto.

    Reihenfolge der Rechnung: erst die Ganzstueck-Reste der Kaeufe bestimmen
    (NTSD, WLDU) und dem Defensiv-ETF ihrer Sleeve ins Ziel schlagen, DANN die
    Differenzen bilden. Sonst wird ein Defensiv-ETF geschlossen und sein Rest
    als Cash liegen gelassen - oder geschlossen und gleich zurueckgekauft.
    Ein Ticker ohne Ziel wird per Close-Position komplett geschlossen (keine
    Rundungsreste wie bei SSO am 21.09.)."""
    current = {p["symbol"]: float(p["market_value"]) for p in positions}
    shares = {p["symbol"]: float(p["qty"]) for p in positions}
    defensive = {cfg["strategy_key"]: cfg["defensive"]
                 for cfg in [bot.aaa_config, bot.mix8_config] + bot.TREND_SLEEVES}
    owner = {s: k for k, syms in bot.STRATEGY_SYMBOLS.items() for s in syms}
    universe = sorted(set(current) | set(targets))
    effective = {s: targets.get(s, 0.0) for s in universe}
    buys, skipped = [], []
    for sym in universe:                                   # 1) Risiko-Kaeufe, Reste sammeln
        if sym in defensive.values():
            continue
        delta = effective[sym] - current.get(sym, 0.0)
        if delta <= TOLERANCE:
            continue
        qty, note = bot._size_buy_order(sym, delta, prices[sym])
        spent = qty * prices[sym]
        if owner.get(sym) in defensive:
            effective[defensive[owner[sym]]] += delta - spent
        if qty > 0:
            buys.append({"symbol": sym, "qty": qty, "est": spent, "note": note})
        else:
            skipped.append({"symbol": sym, "reason": note})
    sells = []
    for sym in universe:                                   # 2) Verkaeufe gegen das effektive Ziel
        cur, tgt = current.get(sym, 0.0), effective[sym]
        if tgt - cur < -TOLERANCE:
            if tgt <= TOLERANCE:
                sells.append({"symbol": sym, "kind": "close", "qty": shares.get(sym, 0.0), "est": cur})
            else:
                qty = bot._size_sell_order(sym, min(shares[sym], (cur - tgt) / prices[sym]))
                if qty > 0:
                    sells.append({"symbol": sym, "kind": "qty", "qty": qty, "est": qty * prices[sym]})
    for sym in defensive.values():                         # 3) Defensiv-Kaeufe inkl. Resten
        delta = effective.get(sym, 0.0) - current.get(sym, 0.0)
        if delta > TOLERANCE:
            qty, note = bot._size_buy_order(sym, delta, prices[sym])
            if qty > 0:
                buys.append({"symbol": sym, "qty": qty, "est": qty * prices[sym], "note": note})
    return sells, buys, skipped


def _prices(api, symbols):
    return {s: float(bot.get_latest_trade(api, s)) for s in symbols}


def approved_margin(api, env):
    """Margin nach genau der Regel des Orchestrators: check_margin_conditions +
    calculate_monthly_investments. Bei geschlossenem Gate oder Datenfehler 0 -
    konservativ, wie ueberall im Bot."""
    margin_result = bot.check_margin_conditions(api, env=env)
    if not margin_result.get("allowed") or margin_result.get("errors"):
        return 0.0, margin_result
    calc = bot.calculate_monthly_investments(api, margin_result, env)
    return float(calc["margin_approved"]), margin_result


def build_dry_run(api, env="live", use_margin=False):
    snapshot = capture_snapshot(api)
    equity = float(snapshot["account"]["equity"])
    sleeves = sleeve_weights(api)
    margin, margin_result = approved_margin(api, env) if use_margin else (0.0, None)
    investable, targets = build_targets(equity, sleeves, margin)
    symbols = sorted({p["symbol"] for p in snapshot["positions"]} | set(targets))
    prices = _prices(api, symbols)
    sells, buys, skipped = plan_orders(snapshot["positions"], targets, prices)
    return {"env": env, "market_is_open": bool(get_market_clock(api).get("is_open")),
            "margin": margin, "margin_gates": (margin_result or {}).get("gate_results"),
            "pending_orders": snapshot["pending_orders"], "equity": equity, "investable": investable,
            "cash": float(snapshot["account"]["cash"]), "sleeves": sleeves, "targets": targets,
            "prices": prices, "sells": sells, "buys": buys, "skipped": skipped}


def _close_position(api, symbol):
    if api.get("EXECUTION_V2"):
        raise RuntimeError("Retired allocation script cannot close shared positions; use execution ledger")
    response = bot.alpaca_request_with_retry(
        "DELETE", f"{api['BASE_URL']}/v2/positions/{symbol}",
        headers=bot.get_auth_headers(api), label=f"close {symbol}", raise_on_fail=True)
    return response.json()


def _write_sleeve_state(env, sleeves, post_positions):
    now = datetime.datetime.now()
    values = {p["symbol"]: (float(p["qty"]), float(p["market_value"])) for p in post_positions}
    db = bot.get_firestore_client().collection(f"strategy-balances-{env}")
    for key in bot.SLEEVES:
        syms = bot.STRATEGY_SYMBOLS[key]
        total = sum(values.get(s, (0, 0))[1] for s in syms)
        doc = db.document(key)
        before = doc.get().to_dict() if doc.get().exists else {}
        record = {
            "current_positions": {s: values.get(s, (0, 0))[0] for s in syms},
            "current_values": {s: values.get(s, (0, 0))[1] for s in syms},
            "peak_nav": max(float(before.get("peak_nav", 0) or 0), total),
            "reallocated_at": now.isoformat(),
            "last_trade_date": now.strftime("%Y-%m-%d"),
        }
        if "signals" in sleeves[key]:
            sig = sleeves[key]["signals"]
            record["leg_states"] = {p: bool(sig[p]["on"]) for p in sig}
        else:
            plan = sleeves[key]["plan"]
            record["last_momentum_check"] = {"scores": plan["scores"], "picks": plan["picks"],
                                             "weights": plan["weights"], "vol_scale": plan["scale"],
                                             "realized_vols": plan["realized_vols"], "dd_triggered": False}
        doc.set(record, merge=True)


def execute(api, env="live", confirmation=None, audit_path=None, use_margin=False):
    if env == "live" and confirmation != CONFIRMATION_TOKEN:
        raise ValueError(f"Live-Ausfuehrung braucht --confirm {CONFIRMATION_TOKEN}")
    if not get_market_clock(api).get("is_open"):
        raise RuntimeError("Markt geschlossen - nichts gesendet")
    dry = build_dry_run(api, env, use_margin=use_margin)
    if dry["pending_orders"]:
        raise RuntimeError(f"Offene Orders vorhanden: {dry['pending_orders']}")
    audit_path = Path(audit_path or f"output/reallocate_{datetime.datetime.now():%Y%m%d_%H%M%S}.json")
    audit = {"dry_run": dry, "sell_fills": [], "buy_fills": [], "started_at": datetime.datetime.utcnow().isoformat()}
    write_audit(audit_path, audit)
    for o in dry["sells"]:
        order = _close_position(api, o["symbol"]) if o["kind"] == "close" else \
            bot.submit_order(api, o["symbol"], o["qty"], "sell")
        filled = strict_wait_for_fill(api, order["id"])
        audit["sell_fills"].append({"symbol": o["symbol"], "qty": filled["filled_qty"],
                                    "price": filled["filled_avg_price"]})
        write_audit(audit_path, audit)
    for o in dry["buys"]:
        order = bot.submit_order(api, o["symbol"], o["qty"], "buy")
        filled = strict_wait_for_fill(api, order["id"])
        audit["buy_fills"].append({"symbol": o["symbol"], "qty": filled["filled_qty"],
                                   "price": filled["filled_avg_price"]})
        write_audit(audit_path, audit)
    post = capture_snapshot(api)
    audit["after"] = post
    _write_sleeve_state(env, dry["sleeves"], post["positions"])
    db = bot.get_firestore_client().collection(f"strategy-balances-{env}")
    for doc_key, syms in RETIRED_DOCS.items():
        # Zweiter Lauf (z. B. Margin-Aufstockung): die Stilllegung steht schon,
        # ein erneutes set() wuerde retirement_orders mit [] ueberschreiben.
        if (db.document(doc_key).get().to_dict() or {}).get("retired"):
            continue
        db.document(doc_key).set({"retired": True, "retired_at": datetime.datetime.utcnow().isoformat(),
                                  "retirement_orders": [f for f in audit["sell_fills"] if f["symbol"] in syms],
                                  "retirement_note": "2026-09-23: durch Mix8 ersetzt; QLD/EFO gehen an Mix8"},
                                 merge=True)
    bot.recalculate_all_strategies_cost_basis(api, env, silent=True)
    write_audit(audit_path, audit)
    by_sleeve = {label: sum(float(p["market_value"]) for p in post["positions"] if p["symbol"] in bot.STRATEGY_SYMBOLS[k])
                 for k, (_, label) in bot.SLEEVES.items()}
    eq = float(post["account"]["equity"])
    head = f"💳 Margin-Aufstockung ausgefuehrt (+${dry['margin']:,.2f})" if dry.get("margin") else "🔁 Umschichtung ausgefuehrt"
    msg = head + "\n\n" + "\n".join(
        f"{label}: ${v:,.2f} ({v / eq * 100:.1f} %)" for label, v in by_sleeve.items())
    msg += f"\n\n{len(audit['sell_fills'])} Verkaeufe, {len(audit['buy_fills'])} Kaeufe\nCash: ${float(post['account']['cash']):,.2f}"
    bot.send_telegram_message(msg)
    return {"audit_path": str(audit_path), "by_sleeve": by_sleeve, "equity": eq}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", choices=["live", "paper"], default="paper")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--audit-path")
    parser.add_argument("--use-margin", action="store_true",
                        help="zusaetzlich die vom Margin-Gate freigegebene Margin investieren")
    args = parser.parse_args()
    api = bot.set_alpaca_environment(args.env, use_secret_manager=False)
    result = build_dry_run(api, args.env, use_margin=args.use_margin) if args.dry_run else \
        execute(api, args.env, confirmation=args.confirm, audit_path=args.audit_path,
                use_margin=args.use_margin)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
