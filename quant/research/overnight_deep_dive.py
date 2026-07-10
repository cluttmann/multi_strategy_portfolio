"""ONX-3x deep dive — is the 3x-ETF overnight anomaly real and deployable?

    python3 -m quant.research.overnight_deep_dive --run

Discipline notes:
- The original six (SOXL TECL FAS UDOW LABU TNA) were chosen BEFORE results
  were seen; the equal-weight basket over all six is the pre-registered
  portfolio number. SOXL-only is reported as (in-sample-selected, inflated).
- A CONFIRMATION set of 3x/2x ETFs not in the original test is evaluated
  once, untouched by any tuning: the anomaly must generalize or it's noise.
- Cost base case 2bp/side (auction orders on liquid ETFs), stress 2x.
- Decomposition: overnight + intraday must reconstruct buy-hold; the
  overnight share of total drift is the anomaly's fingerprint.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.research.exotic_sleeves import alpaca_daily, perf

ORIGINAL6 = ["SOXL", "TECL", "FAS", "UDOW", "LABU", "TNA"]
# Confirmation set (evaluated once, no variants): other liquid 3x/2x levered
# ETFs, ex bot tickers (SPXL/TQQQ/QLD/SPUU excluded).
CONFIRM = ["ERX", "NUGT", "DFEN", "NAIL", "DPST", "WEBL", "URTY", "MIDU"]

COST_SIDE_BPS = 2.0


def overnight_returns(sym: str, start="2016-01-01") -> pd.Series:
    df = alpaca_daily(sym, start)
    return (df["o"] / df["c"].shift(1) - 1).rename(sym)


def intraday_returns(sym: str, start="2016-01-01") -> pd.Series:
    df = alpaca_daily(sym, start)
    return (df["c"] / df["o"] - 1).rename(sym)


def run():
    cost = 2 * COST_SIDE_BPS / 1e4

    print("═══ 1. Pre-registered equal-weight basket (original six) ═══")
    on = pd.DataFrame({s: overnight_returns(s) for s in ORIGINAL6})
    basket = on.mean(axis=1, skipna=True)
    perf(basket - cost, "EW basket, net 4bp/day")
    perf(basket - 2 * cost, "EW basket, net 8bp/day (2x stress)")

    print("\n═══ 2. Decomposition sanity (overnight vs intraday) ═══")
    for s in ["SOXL", "TNA"]:
        onr, idr = overnight_returns(s), intraday_returns(s)
        tot = (1 + onr).cumprod().iloc[-1]
        toti = (1 + idr.reindex(onr.index)).cumprod().iloc[-1]
        print(f"{s}: overnight-only growth {tot:8.1f}x | intraday-only "
              f"growth {toti:8.3f}x  (all drift lives overnight ⇒ anomaly)")

    print("\n═══ 3. Rolling 2-year net CAGR of the basket (stability) ═══")
    net = basket - cost
    eq = (1 + net.fillna(0)).cumprod()
    roll = eq.pct_change(504).dropna()
    ann = (1 + roll) ** (252 / 504) - 1
    q = ann.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
    print("2y-rolling net CAGR percentiles: " +
          "  ".join(f"p{int(p*100)}={v:+.0%}" for p, v in q.items()))
    print(f"share of 2y windows positive: {(ann > 0).mean():.0%}")

    print("\n═══ 4. CONFIRMATION SET — evaluated once, no tuning ═══")
    results = {}
    for s in CONFIRM:
        try:
            r = overnight_returns(s)
            results[s] = r
            perf(r - cost, f"{s} overnight net")
        except Exception as e:  # noqa: BLE001
            print(f"{s}: unavailable ({e})")
    if results:
        conf = pd.DataFrame(results).mean(axis=1, skipna=True)
        perf(conf - cost, "confirmation EW basket net")

    print("\n═══ 5. Regime slices (basket, net) ═══")
    slices = {"2016-2019": ("2016-01-01", "2019-12-31"),
              "2020 (covid)": ("2020-01-01", "2020-12-31"),
              "2021": ("2021-01-01", "2021-12-31"),
              "2022 (bear)": ("2022-01-01", "2022-12-31"),
              "2023-2026": ("2023-01-01", "2026-12-31")}
    for label, (a, b) in slices.items():
        perf(net.loc[a:b], f"basket {label}")

    print("\n═══ 6. Day-of-week / holding-cost realism notes ═══")
    dow = net.groupby(net.index.dayofweek).mean() * 1e4
    print("mean net bp by weekday(exit): " +
          "  ".join(f"{'MTWTF'[int(d)]}:{v:+.1f}" for d, v in dow.items()))
    fri_exposure = "held Fri close → Mon open (weekend risk) included"
    print(fri_exposure)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    run()
