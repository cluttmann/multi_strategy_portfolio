"""XSR sleeve live executor — cross-sectional ranker, open-auction flow.

    python3 -m quant.execution.xsr_live --plan [--dry-run]     # pre-market
    python3 -m quant.execution.xsr_live --execute [--dry-run]  # place opg orders
    python3 -m quant.execution.xsr_live --reconcile            # after open

Timing matches the validated backtest exactly: scores come from the last
COMPLETE trading day's features (EODHD EOD data, loaded overnight); entries
fill at the opening auction via `opg` orders; labels in the backtest were
open(t+1)→open(t+1+h), so live and backtest see the same prices.

Sizing: n_side scales with equity (whole shares only for auction orders);
long-short dollar-balanced.

Turnover-Kontrolle: ECHTE ueberlappende Tranchen (Jegadeesh-Titman), identisch
zu portfolio_sim.simulate_tranches. Jeden Tag wird eine Tranche in Vollgroesse
aus den heutigen Scores gebildet; gehandelt wird der Mittelwert der letzten
K_TRANCHE Tranchen. Vorher stand hier eine Hysterese ("Bestandsschutz im
6x-Band"), die mit keinem k des Simulators korrespondierte — Backtest und Live
massen verschiedene Buecher.

Short-Bein: nur easy_to_borrow (siehe borrowable()). Alpaca lehnt opg-Orders
fuer hard-to-borrow Titel ab, und der Simulator unterstellt 200bp/Jahr Leihe,
was fuer HTB-Namen unrealistisch ist.
"""

import argparse
import datetime as dt
import os
import sys

import numpy as np
import pandas as pd

from quant.config import BOT_TICKERS, STAGING_DIR
from quant.execution import broker, ledger, risk
from quant.execution.telegram import notify

SLEEVE = "xsr"
# RISIKOPARITÄT 2026-08-06 (risk_budget_study.py): 0.40 → 0.14 pro Seite.
# XSR trug mit 0.80x Gross und 20.1 % Vol rund 46 % des Risikobudgets, bei der
# NIEDRIGSTEN Sharpe der vier Sleeves (0.60 vs. DTRD 0.72 / EOMT 0.88 /
# MERGARB 1.05) — und kostet pro Renditeeinheit doppelt so viel Bilanz wie die
# anderen, weil seine Reihe auf 2x internem Gross läuft. Neues Gross 0.28x
# (= 2 × 0.14) entspricht ~2.9 % Vol-Beitrag, gleichauf mit den anderen drei.
# Split-Sample-belegt: OOS-Sharpe des Gesamtbuchs 0.55 → 1.14, MaxDD −18.6 %
# → −6.9 %. Die Umgewichtung bringt kaum Zusatzrendite, sondern den
# Risikopuffer, der den Hebel überhaupt tragbar macht.
SLEEVE_ALLOC = 0.14       # of equity, per side (gross 2x alloc)
TARGET_POS_USD = 400.0    # min sensible whole-share position

# HALTEDAUER = 21 Tage, geändert 2026-07-25 nach der vorregistrierten Regel aus
# FINDINGS ("Deploy 5d, 21d als kostenrobuster Fallback — Burn-in misst echte
# Fills als Entscheider"). Der Burn-in hat 9.9-10.0bp Slippage gemessen, doppelt
# so viel wie angenommen. Bei 10bp gemessen, mit Leih-Gate, Regime 2022+:
#     k=5  Sharpe -0.048   Turnover 0.545
#     k=10 Sharpe +0.076   Turnover 0.302
#     k=21 Sharpe +0.199   Turnover 0.158
# Wichtig: es braucht KEINE 21d-Modelle. Dasselbe 5d-Signal 21 Tage gehalten
# liefert 0.199 gegen 0.190 des eigens trainierten 21d-Modells — der Gewinn kommt
# aus dem Turnover, nicht aus dem Label.
K_TRANCHE = 21


def _ensure_models() -> str:
    """Modell-Verzeichnis lokal bereitstellen; in der Cloud aus GCS syncen.

    GCS-Bucket via QNT_MODEL_BUCKET (z.B. gs://trading-436516-quant-models).
    Lokal (Bucket ungesetzt) wird STAGING_DIR/models genutzt.
    """
    model_dir = os.path.join(STAGING_DIR, "models")
    bucket = os.environ.get("QNT_MODEL_BUCKET")
    if bucket:
        from google.cloud import storage
        os.makedirs(model_dir, exist_ok=True)
        bkt = bucket.replace("gs://", "").split("/", 1)[0]
        client = storage.Client()
        for b in client.list_blobs(bkt, prefix="models/ranker_"):
            dst = os.path.join(model_dir, os.path.basename(b.name))
            if not os.path.exists(dst):
                b.download_to_filename(dst)
    return model_dir


def latest_scores() -> pd.DataFrame:
    """Score the latest complete day with the newest saved fold model."""
    import lightgbm as lgb
    from quant.data.bq import query
    from quant.features.xsr_v2_features import T_V2
    from quant.models.train_ranker import V2_FEATURES

    model_dir = _ensure_models()
    models = sorted(f for f in os.listdir(model_dir) if f.startswith("ranker_"))
    model = lgb.Booster(model_file=os.path.join(model_dir, models[-1]))
    day = query(f"SELECT MAX(date) d FROM `{T_V2}`").iloc[0].d
    df = query(f"SELECT * FROM `{T_V2}` WHERE date = '{day}'")
    feats = [f for f in V2_FEATURES if f in df.columns]
    df["score"] = model.predict(df[feats])
    print(f"scored {len(df):,} names for {day} with {models[-1]}")
    return df[["symbol", "score", "vol_63d", "raw_close"]].dropna(
        subset=["score"])


def borrowable() -> set[str] | None:
    """Leihbares Universum aus dem jüngsten Leih-Snapshot.

    WARUM ALS GATE UND NICHT ALS NACHBEHANDLUNG: Alpaca lehnt `opg`-Orders für
    hard-to-borrow Titel mit HTTP 422 / 42210000 ab ("only day orders are
    allowed"). Am 2026-07-24 hat genau das (ASTC) den XSR-Lauf abgebrochen —
    und weil `execute()` keine Fehlerisolierung hatte, wurde damit das GANZE
    Buch nicht platziert. Das ist der Grund für die 3 Fills.

    Wichtiger noch als der Ausführungsfehler ist die KOSTENSEITE: der Backtest
    unterstellt 200bp/Jahr Leihkosten (portfolio_sim.BORROW_BPS_YR). Für
    hard-to-borrow Titel sind 20-100 %/Jahr üblich. Ein Short-Bein mit
    HTB-Namen ist also nicht nur unhandelbar, es macht die validierte
    Sharpe-Zahl unehrlich. Von 14.216 Symbolen im Snapshot sind 8.846
    hard_to_borrow — das Gate ist keine Randkorrektur.

    Fail-closed: ohne Snapshot gibt es None, und der Aufrufer verzichtet dann
    auf das Short-Bein statt blind zu shorten.
    """
    from quant.data.bq import query
    try:
        df = query("""
          SELECT DISTINCT symbol FROM `trading-436516.quant.borrow_snapshots`
          WHERE DATE(snap_ts) >= DATE_SUB(CURRENT_DATE(), INTERVAL 4 DAY)
            AND shortable AND borrow_status = 'easy_to_borrow'""")
    except Exception as e:  # noqa: BLE001
        print(f"Leih-Snapshot nicht lesbar ({e}) — Short-Bein entfällt")
        return None
    if len(df) < 500:
        print(f"Leih-Snapshot zu dünn ({len(df)} Symbole) — Short-Bein entfällt")
        return None
    return set(df["symbol"])


def plan(dry_run: bool):
    from quant.execution.guard import guard_or_exit
    burn = guard_or_exit(SLEEVE)
    acct = broker.account()
    equity = float(acct["equity"])
    scale = risk.drawdown_scale(equity) * burn
    side_budget = equity * SLEEVE_ALLOC * scale
    n_side = int(np.clip(side_budget / TARGET_POS_USD, 5, 75))

    df = latest_scores()
    df = df[~df["symbol"].isin(BOT_TICKERS)]
    df = df.sort_values("score", ascending=False)
    longs = df.head(n_side)
    # Short-Bein nur aus leihbaren Namen (siehe borrowable()). Die Longs bleiben
    # unberührt — Leihbarkeit ist nur für die Short-Seite relevant.
    lend = borrowable()
    if lend is None:
        shorts = df.iloc[0:0]
        notify("XSR: kein Leih-Snapshot → nur Long-Bein (fail-closed)")
    else:
        elig = df[df["symbol"].isin(lend)]
        shorts = elig.tail(n_side)
        drop = len(df.tail(n_side)) - len(
            df.tail(n_side)[df.tail(n_side)["symbol"].isin(lend)])
        if drop:
            print(f"Short-Bein: {drop} von {n_side} schlechtesten Namen nicht "
                  f"leihbar → durch die nächstschlechteren ersetzt")

    def sized(sub: pd.DataFrame, sign: int) -> dict[str, int]:
        w = (1.0 / sub["vol_63d"].clip(lower=0.10))
        w = w / w.sum() * side_budget
        out = {}
        for sym, notional, px in zip(sub["symbol"], w, sub["raw_close"]):
            qty = int(notional // px)
            if qty >= 1:
                out[sym] = sign * qty
        return out

    # ── ECHTE ÜBERLAPPENDE TRANCHEN (Jegadeesh–Titman), wie im Simulator ──────
    # Vorher stand hier eine Hysterese ("Bestandsschutz im 6x-Band"), die den
    # Turnover nur ungefähr dämpfte und mit keinem k des Simulators
    # korrespondierte. Der Simulator bildet jeden Tag 1/k des Buches neu und
    # handelt den MITTELWERT der letzten k Tranchen; genau das wird hier jetzt
    # abgebildet. Ohne diesen Gleichlauf misst der Backtest ein Buch, das live
    # nie existiert — der wiederkehrende Fehler in diesem Projekt.
    heute = {**sized(longs, +1), **sized(shorts, -1)}
    st = ledger.get_sleeve(SLEEVE)
    tranches = list(st.get("tranches") or [])
    tranches.append({"stand": str(pd.Timestamp.today().date()), "ziel": heute})
    tranches = tranches[-K_TRANCHE:]          # nur die letzten k behalten
    agg: dict[str, float] = {}
    for tr in tranches:
        for s, q in (tr.get("ziel") or {}).items():
            agg[s] = agg.get(s, 0.0) + float(q)
    # Mittelwert über k Tranchen — nicht über die vorhandenen: solange weniger
    # als k Tranchen gesammelt sind, läuft das Buch bewusst kleiner an, statt
    # am ersten Tag volle Größe mit einem einzigen Signal aufzubauen.
    merged = {s: int(v / K_TRANCHE) for s, v in agg.items()
              if abs(v / K_TRANCHE) >= 1}
    # `stand` ist das Frische-Siegel, das execute() prüft — siehe dort. Ohne
    # es kann execute() einen Altplan nicht von einem heutigen unterscheiden.
    plan_doc = {"target": merged, "n_side": n_side, "scale": scale,
                "equity": equity, "k": K_TRANCHE, "tranchen": len(tranches),
                "stand": str(pd.Timestamp.today().date())}
    print(f"plan: {len([q for q in merged.values() if q > 0])} long / "
          f"{len([q for q in merged.values() if q < 0])} short, "
          f"~${side_budget:,.0f}/side, {len(tranches)}/{K_TRANCHE} Tranchen")
    if not dry_run:
        ledger.set_sleeve(SLEEVE, {**st, "plan": plan_doc,
                                   "tranches": tranches})
    notify(f"XSR plan: {len(merged)} Namen, ${side_budget:,.0f}/Seite, "
           f"Tranchen {len(tranches)}/{K_TRANCHE}, scale {scale}"
           + (" [DRY RUN]" if dry_run else ""))


def execute(dry_run: bool):
    # PAUSE- UND FRISCHE-GATE. Analog zum ONX-Kapitalfehler vom 2026-08-06:
    # dort prüfte decide() die Pause, enter() aber nicht — und kaufte deshalb
    # nach der Pause täglich denselben veralteten Plan nach, bis 25 % der
    # Equity in einem stillgelegten Sleeve steckten. XSR hat exakt dieselbe
    # Aufteilung (plan() um 12:30 UTC gegated, execute() um 13:00 UTC war es
    # nicht), also exakt dieselbe latente Lücke — hier präventiv geschlossen,
    # bevor sie einmal zuschlägt.
    from quant.execution.guard import guard_or_exit
    guard_or_exit(SLEEVE)
    state = ledger.get_sleeve(SLEEVE)
    plan = state.get("plan") or {}
    target = plan.get("target") or {}
    if not target:
        notify("XSR execute: no plan — standing down (fail-closed)")
        return
    if str(plan.get("stand") or "") != str(dt.date.today()):
        notify(f"XSR execute: Plan ist nicht von heute "
               f"(stand={plan.get('stand')!r}) — standing down (fail-closed)")
        return
    acct = broker.account()
    equity = float(acct["equity"])
    # WICHTIG: Broker-Positionen sind die Wahrheit, nicht das Ledger. Alpaca
    # markiert gefüllte Auktionsorders als "expired", wodurch das Ledger leer
    # blieb, während Positionen offen waren — ohne diesen Abgleich hätte XSR
    # am nächsten Handelstag die Positionen VERDOPPELT (Bug gefunden
    # 2026-07-25, vor dem ersten Montag).
    actual = broker.positions()
    known = set(state.get("symbol_universe") or []) | set(
        (state.get("positions") or {}).keys()) | set(target)
    held = {s: int(q) for s, q in actual.items() if s in known and q != 0}
    if held != (state.get("positions") or {}):
        print(f"Ledger-Abgleich: {len(held)} echte Positionen "
              f"(Ledger hatte {len(state.get('positions') or {})})")
    orders = []
    for sym in set(target) | set(held):
        delta = int(target.get(sym, 0)) - int(held.get(sym, 0))
        if delta == 0:
            continue
        # |Ziel| < |Bestand| heisst: die Order baut ab. Solche Orders duerfen
        # nicht am Konto-Gross-Deckel scheitern, sonst blockiert der Schutz
        # genau die risikosenkenden Trades.
        reduces = abs(int(target.get(sym, 0))) < abs(int(held.get(sym, 0)))
        orders.append((sym, delta, reduces))
    gross = 0.0
    prices = broker.latest_prices([s for s, _, _ in orders])
    placed = 0
    rejected: list[tuple[str, str]] = []
    for i, (sym, delta, reduces) in enumerate(sorted(orders)):
        px = prices.get(sym) or 0
        notional = abs(delta) * px
        ok, why = risk.check_order(sym, notional, SLEEVE, equity,
                                   gross + notional,
                                   reduces_exposure=reduces)
        if not ok:
            notify(f"XSR: {sym} blocked — {why}")
            continue
        side = "buy" if delta > 0 else "sell"
        if dry_run:
            print(f"[dry] opg {side} {abs(delta)} {sym}")
        else:
            # FEHLERISOLIERUNG JE ORDER. Vorher brach eine einzige Ablehnung
            # (ASTC, hard-to-borrow, 2026-07-24) den ganzen Lauf ab, und das
            # gesamte restliche Buch wurde nie platziert. Ein Broker-Nein zu
            # einem Namen ist ein normaler Betriebszustand, kein Grund, die
            # Strategie stillzulegen.
            try:
                broker.submit_order(sym, abs(delta), side, "opg", SLEEVE, i)
            except Exception as e:  # noqa: BLE001
                rejected.append((sym, str(e)[:110]))
                continue
        gross += notional
        placed += 1
    if not dry_run:
        universe = sorted(set(state.get("symbol_universe") or [])
                          | set(target) | set(held))
        ledger.set_sleeve(SLEEVE, {**state, "pending_target": target,
                                   "positions": held,
                                   "symbol_universe": universe})
    msg = (f"XSR execute: {placed} opg orders, delta gross ≈ ${gross:,.0f}"
           + (" [DRY RUN]" if dry_run else ""))
    if rejected:
        # Ablehnungen sind sichtbar zu machen, nicht zu verschweigen: ein
        # dauerhaft abgelehnter Name verzerrt das Buch gegenüber dem Backtest.
        msg += (f" | {len(rejected)} abgelehnt: "
                + ", ".join(f"{s} ({e.split('message')[-1][:40]})"
                            for s, e in rejected[:5]))
        print("Abgelehnte Orders:")
        for s, e in rejected:
            print(f"  {s}: {e}")
    notify(msg)


def reconcile():
    state = ledger.get_sleeve(SLEEVE)
    held = dict(state.get("positions") or {})
    for s, q in broker.sleeve_fills_today(SLEEVE).items():
        held[s] = held.get(s, 0) + q
    # Gegen die Broker-Wahrheit abgleichen (verhindert Ledger-Drift)
    actual = broker.positions()
    held = {s: int(actual[s]) for s in held if s in actual and actual[s] != 0}
    ledger.set_sleeve(SLEEVE, {**state, "positions": held})
    notify(f"XSR reconcile: {len(held)} Positionen, netto "
           f"{sum(1 for q in held.values() if q > 0)}L/"
           f"{sum(1 for q in held.values() if q < 0)}S")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--plan", action="store_true")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--reconcile", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.plan:
        plan(args.dry_run)
    elif args.execute:
        execute(args.dry_run)
    elif args.reconcile:
        reconcile()
    else:
        p.print_help()
        sys.exit(1)
