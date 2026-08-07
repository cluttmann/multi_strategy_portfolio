"""Leveraged-ETF variance-drag harvest — short both legs of 3x pairs.

    python3 -m quant.research.pair_decay --run

Edge: a 3x bull and its -3x bear sibling BOTH lose to volatility drag
(log-drift penalty ≈ -4.5σ² each). Shorting both, dollar-balanced, is
market-neutral to first order and collects the drag. The position is short
gamma: it loses when the underlying trends hard between rebalances — that,
plus bear-leg borrow fees, is why the edge persists structurally.

Honest cost model (paper flatters all of this, so it's charged here):
  borrow: 12%/yr on bear legs, 3%/yr on bull legs, charged daily
  trading: 10bp per side on every rebalance trade
  rebalance: monthly (calendar), plus an emergency rebalance whenever a leg
  drifts to 1.75x its target weight (squeeze guard — realistic risk practice)

Variants (pre-registered, no further tuning):
  V1 monthly rebalance
  V2 monthly + emergency drift guard
  V3 = V2 sized at 50% notional per pair (half-gross, for the ladder)
Pairs: SOXL/SOXS, TNA/TZA, FAS/FAZ, LABU/LABD, SPXU excluded? SPXL is a bot
ticker — the SPXL/SPXU pair is excluded from TRADING; UDOW/SDOW used instead.
Report full sample AND 2022-2026 window separately (regime honesty).
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.research.exotic_sleeves import alpaca_daily, perf

PAIRS = [("SOXL", "SOXS"), ("TNA", "TZA"), ("FAS", "FAZ"),
         ("LABU", "LABD"), ("UDOW", "SDOW")]
BORROW_BULL = 0.03
BORROW_BEAR = 0.12
COST_SIDE = 10 / 1e4
DRIFT_GUARD = 1.75


def pair_series(bull: str, bear: str, guard: bool) -> pd.Series | None:
    try:
        b = alpaca_daily(bull, "2016-01-01")["c"]
        s = alpaca_daily(bear, "2016-01-01")["c"]
    except Exception as e:  # noqa: BLE001
        print(f"{bull}/{bear}: {e}")
        return None
    df = pd.DataFrame({"b": b, "s": s}).dropna()
    rb, rs = df["b"].pct_change().fillna(0), df["s"].pct_change().fillna(0)

    # short $0.5 of each leg per $1 of pair notional
    wb = ws = 0.5
    rows = []
    month = None
    for d in df.index:
        # daily P&L of the short-short book (weights are of pair notional)
        pnl = -(wb * rb[d] + ws * rs[d])
        pnl -= (wb * BORROW_BULL + ws * BORROW_BEAR) / 252
        # weights drift with leg performance (short: weight grows when leg rises)
        wb *= (1 + rb[d])
        ws *= (1 + rs[d])
        need_rebal = (month is not None and d.month != month)
        if guard and max(wb, ws) > DRIFT_GUARD * 0.5:
            need_rebal = True
        if month is None:
            month = d.month
        if need_rebal:
            turn = abs(wb - 0.5) + abs(ws - 0.5)
            pnl -= turn * COST_SIDE
            wb = ws = 0.5
            month = d.month
        rows.append((d, pnl))
    return pd.Series(dict(rows)).rename(f"{bull}/{bear}")


def run():
    print("simulating short-short pairs (per $1 pair notional) ...")
    for guard, label in [(False, "V1 monthly"), (True, "V2 monthly+guard")]:
        print(f"\n═══ {label} ═══")
        legs = []
        for bull, bear in PAIRS:
            r = pair_series(bull, bear, guard)
            if r is None:
                continue
            legs.append(r)
            perf(r, f"{bull}/{bear}")
        if not legs:
            return
        basket = pd.concat(legs, axis=1).mean(axis=1, skipna=True)
        perf(basket, "EW pair basket")
        perf(basket.loc["2022":], "EW basket 2022-2026 (regime check)")
        if guard:
            # squeeze stress: worst 10 days
            worst = basket.nsmallest(10) * 1e4
            print("worst 10 days (bp of pair notional): "
                  + ", ".join(f"{d.date()}:{v:.0f}" for d, v in worst.items()))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    run()
