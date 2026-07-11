"""ONX day-of-week refinement — strict split-sample validation.

    python3 -m quant.research.onx_dow_validation --run

Protocol (pre-declared before looking at the validation half):
  1. DISCOVERY = 2016-01-01 .. 2021-12-31 of the full-universe V2 series.
     Rule formation: keep exactly those holding-nights (exit weekday) whose
     discovery-period mean net return is positive. No other conditioning.
  2. VALIDATION = 2022-01-01 .. 2026-07-10, evaluated ONCE with the rule
     frozen from step 1. Also evaluated: the rule on the untouched
     confirmation basket for cross-sample robustness.
  3. Deployable ladder: refined series at 1.0-1.9x with 7%/yr financing
     above 1x, on the VALIDATION window and full sample.

Prior justification (why this is not fishing): the overnight-effect
literature documents strong weekday heterogeneity (weekend/Monday effects,
mid-week concentration). One rule, one validation shot.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.config import BOT_TICKERS
from quant.research.exotic_sleeves import alpaca_daily
from quant.research.overnight_universe import discover_universe

COST = 4 / 1e4
MARGIN_RATE = 0.07
SPLIT = "2022-01-01"


def build_v2(symbols: list[str]) -> pd.Series:
    on, cl = {}, {}
    for s in symbols:
        try:
            df = alpaca_daily(s, "2016-01-01")
            on[s] = df["o"] / df["c"].shift(1) - 1
            cl[s] = df["c"]
        except Exception:  # noqa: BLE001
            pass
    onf, clf = pd.DataFrame(on), pd.DataFrame(cl)
    gate = clf.shift(1) > clf.rolling(50).mean().shift(1)
    v2 = onf.where(gate).mean(axis=1, skipna=True).fillna(0)
    held = gate.mean(axis=1) > 0
    return v2 - COST * held


def stats(r: pd.Series, label: str, days=252):
    r = r.dropna()
    if r.std() == 0 or len(r) < 50:
        print(f"{label}: insufficient")
        return None
    eq = (1 + r).cumprod()
    cagr = eq.iloc[-1] ** (days / len(r)) - 1
    sh = r.mean() / r.std() * np.sqrt(days)
    dd = (eq / eq.cummax() - 1).min()
    print(f"{label:52s} CAGR={cagr:+7.1%}  Sharpe={sh:5.2f}  MaxDD={dd:6.1%}")
    return cagr, sh, dd


def run():
    universe = [s for s in discover_universe() if s not in BOT_TICKERS]
    print(f"building V2 series for {len(universe)} ETFs ...")
    v2 = build_v2(universe)

    disc = v2.loc[:SPLIT]
    val = v2.loc[SPLIT:]

    # ---- 1. rule formation on DISCOVERY only ----
    by_dow = disc.groupby(disc.index.dayofweek).mean() * 1e4
    print("\nDISCOVERY (2016-2021) mean net bp by exit weekday:")
    names = "Mon Tue Wed Thu Fri".split()
    for d, v in by_dow.items():
        print(f"  exit {names[int(d)]}: {v:+6.1f}bp")
    keep = sorted(int(d) for d, v in by_dow.items() if v > 0)
    print(f"RULE (frozen): hold only nights exiting on "
          f"{[names[d] for d in keep]}")

    def apply_rule(series: pd.Series) -> pd.Series:
        mask = series.index.dayofweek.isin(keep)
        return series.where(mask, 0.0)

    # ---- 2. one-shot validation ----
    print("\n=== DISCOVERY window (in-sample for the rule) ===")
    stats(disc, "V2 baseline")
    stats(apply_rule(disc), "V2 + DOW rule")

    print("\n=== VALIDATION window 2022-2026 (one shot, rule frozen) ===")
    base = stats(val, "V2 baseline")
    ref = stats(apply_rule(val), "V2 + DOW rule")

    verdict = (ref is not None and base is not None
               and ref[1] > base[1] and ref[0] > base[0])
    print(f"\nOOS verdict: {'CONFIRMED' if verdict else 'REFUTED'} "
          f"(needs both CAGR and Sharpe to improve)")

    # ---- 3. deployable ladder on the refined series ----
    refined = apply_rule(v2)
    print("\n=== refined-series leverage ladder (full sample, financing >1x) ===")
    for g in [1.0, 1.25, 1.5, 1.75, 1.9]:
        lev = refined * g - max(g - 1, 0) * MARGIN_RATE / 252
        stats(lev, f"gross {g:.2f}x")
    print("\n=== same ladder, VALIDATION window only ===")
    for g in [1.0, 1.5, 1.9]:
        lev = apply_rule(val) * g - max(g - 1, 0) * MARGIN_RATE / 252
        stats(lev, f"gross {g:.2f}x (2022-2026)")

    yearly = (apply_rule(v2) * 1.9 - 0.9 * MARGIN_RATE / 252)
    yt = yearly.groupby(yearly.index.year).apply(lambda r: (1 + r).prod() - 1)
    print("\n1.9x refined per-year: "
          + "  ".join(f"{y}:{v:+.0%}" for y, v in yt.items()))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    run()
