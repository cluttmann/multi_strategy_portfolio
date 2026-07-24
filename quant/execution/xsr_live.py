"""XSR sleeve live executor — cross-sectional ranker, open-auction flow.

    python3 -m quant.execution.xsr_live --plan [--dry-run]     # pre-market
    python3 -m quant.execution.xsr_live --execute [--dry-run]  # place opg orders
    python3 -m quant.execution.xsr_live --reconcile            # after open

Timing matches the validated backtest exactly: scores come from the last
COMPLETE trading day's features (EODHD EOD data, loaded overnight); entries
fill at the opening auction via `opg` orders; labels in the backtest were
open(t+1)→open(t+1+h), so live and backtest see the same prices.

Sizing: n_side scales with equity (whole shares only for auction orders);
long-short dollar-balanced; 5-day tranche rotation is approximated live by
only trading the DELTA between yesterday's book and today's target (the
hysteresis/tranche turnover control from the sim).
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd

from quant.config import BOT_TICKERS, STAGING_DIR
from quant.execution import broker, ledger, risk
from quant.execution.telegram import notify

SLEEVE = "xsr"
SLEEVE_ALLOC = 0.40       # of equity, per side (gross 2x alloc)
TARGET_POS_USD = 400.0    # min sensible whole-share position
K_TRANCHE = 5             # rotate 1/K of the book per day


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
    shorts = df.tail(n_side)

    def sized(sub: pd.DataFrame, sign: int) -> dict[str, int]:
        w = (1.0 / sub["vol_63d"].clip(lower=0.10))
        w = w / w.sum() * side_budget
        out = {}
        for sym, notional, px in zip(sub["symbol"], w, sub["raw_close"]):
            qty = int(notional // px)
            if qty >= 1:
                out[sym] = sign * qty
        return out

    target = {**sized(longs, +1), **sized(shorts, -1)}
    prev = (ledger.get_sleeve(SLEEVE).get("target") or {})
    # tranche-style turnover control: keep any incumbent still in the top/
    # bottom 30% band; rotate the rest
    band_syms = set(df.head(n_side * 6)["symbol"])
    band_syms_s = set(df.tail(n_side * 6)["symbol"])
    kept = {s: q for s, q in prev.items()
            if (q > 0 and s in band_syms) or (q < 0 and s in band_syms_s)}
    merged = {**target, **kept}  # incumbents keep their size
    plan_doc = {"target": merged, "n_side": n_side, "scale": scale,
                "equity": equity}
    print(f"plan: {len([q for q in merged.values() if q > 0])} long / "
          f"{len([q for q in merged.values() if q < 0])} short, "
          f"~${side_budget:,.0f}/side")
    if not dry_run:
        ledger.set_sleeve(SLEEVE, {**ledger.get_sleeve(SLEEVE),
                                   "plan": plan_doc})
    notify(f"XSR plan: {len(merged)} names, ${side_budget:,.0f}/side, "
           f"scale {scale}" + (" [DRY RUN]" if dry_run else ""))


def execute(dry_run: bool):
    state = ledger.get_sleeve(SLEEVE)
    target = (state.get("plan") or {}).get("target") or {}
    if not target:
        notify("XSR execute: no plan — standing down (fail-closed)")
        return
    acct = broker.account()
    equity = float(acct["equity"])
    held = state.get("positions") or {}
    orders = []
    for sym in set(target) | set(held):
        delta = int(target.get(sym, 0)) - int(held.get(sym, 0))
        if delta == 0:
            continue
        orders.append((sym, delta))
    gross = 0.0
    prices = broker.latest_prices([s for s, _ in orders])
    placed = 0
    for i, (sym, delta) in enumerate(sorted(orders)):
        px = prices.get(sym) or 0
        notional = abs(delta) * px
        ok, why = risk.check_order(sym, notional, SLEEVE, equity,
                                   gross + notional)
        if not ok:
            notify(f"XSR: {sym} blocked — {why}")
            continue
        side = "buy" if delta > 0 else "sell"
        if dry_run:
            print(f"[dry] opg {side} {abs(delta)} {sym}")
        else:
            broker.submit_order(sym, abs(delta), side, "opg", SLEEVE, i)
        gross += notional
        placed += 1
    if not dry_run:
        ledger.set_sleeve(SLEEVE, {**state, "pending_target": target})
    notify(f"XSR execute: {placed} opg orders, delta gross ≈ ${gross:,.0f}"
           + (" [DRY RUN]" if dry_run else ""))


def reconcile():
    state = ledger.get_sleeve(SLEEVE)
    held = dict(state.get("positions") or {})
    for o in broker.orders_today(SLEEVE):
        if o["status"] != "filled":
            continue
        q = int(float(o["filled_qty"]))
        s = o["symbol"]
        held[s] = held.get(s, 0) + (q if o["side"] == "buy" else -q)
    held = {s: q for s, q in held.items() if q != 0}
    ledger.set_sleeve(SLEEVE, {**state, "positions": held})
    notify(f"XSR reconcile: {len(held)} positions")


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
