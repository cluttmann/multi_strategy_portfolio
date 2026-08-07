"""ONX-3x full-universe test — the no-selection version.

    python3 -m quant.research.overnight_universe --run

Universe: EVERY active, tradable long-leveraged 3x bull equity/sector ETF on
Alpaca, discovered programmatically from the assets API (name matching), bear
funds excluded, bot tickers excluded from the DEPLOYABLE basket. No ticker is
hand-picked; the basket return is the skipna mean of whatever traded each day.

Exactly three pre-registered variants (no further tuning permitted):
  V1  EW basket, always in.
  V2  V1 + per-ETF trend gate (yesterday close > 50d SMA).
  V3  V1 scaled to a 40% annualized vol target (20d trailing, cap 1.5x).
Costs: 4bp/day round trip base, 8bp stress reported for V1.
"""

import argparse
import re
import sys

import numpy as np
import pandas as pd
import requests

from quant.config import ALPACA_KEY_PAPER, ALPACA_SECRET_PAPER, BOT_TICKERS
from quant.research.exotic_sleeves import alpaca_daily, perf

H = {"APCA-API-KEY-ID": ALPACA_KEY_PAPER, "APCA-API-SECRET-KEY": ALPACA_SECRET_PAPER}

BULL3X_PAT = re.compile(r"\b(bull\s*3x|3x\s*(shares|bull)|ultrapro)\b", re.I)
BEAR_PAT = re.compile(r"\b(bear|inverse|short)\b", re.I)


def discover_universe() -> list[str]:
    r = requests.get("https://paper-api.alpaca.markets/v2/assets",
                     params={"status": "active", "asset_class": "us_equity"},
                     headers=H, timeout=60)
    r.raise_for_status()
    assets = r.json()
    syms = []
    for a in assets:
        name = a.get("name") or ""
        if not a.get("tradable"):
            continue
        if BULL3X_PAT.search(name) and not BEAR_PAT.search(name):
            syms.append(a["symbol"])
    return sorted(set(syms))


def run():
    univ = discover_universe()
    deployable = [s for s in univ if s not in BOT_TICKERS]
    print(f"discovered {len(univ)} bull-3x ETFs; {len(deployable)} deployable "
          f"(bot tickers removed): {deployable}")

    cost = 4 / 1e4
    on, closes = {}, {}
    for s in deployable:
        try:
            df = alpaca_daily(s, "2016-01-01")
            if len(df) < 200:
                print(f"  {s}: only {len(df)} days, kept (young fund)")
            on[s] = df["o"] / df["c"].shift(1) - 1
            closes[s] = df["c"]
        except Exception as e:  # noqa: BLE001
            print(f"  {s}: no data ({e})")
    onf = pd.DataFrame(on)
    cl = pd.DataFrame(closes)
    print(f"panel: {onf.shape[1]} ETFs × {onf.shape[0]} days, "
          f"{onf.index.min().date()} → {onf.index.max().date()}")

    # V1 equal weight
    v1 = onf.mean(axis=1, skipna=True)
    perf(v1 - cost, "V1 EW all-universe, net 4bp")
    perf(v1 - 2 * cost, "V1 EW all-universe, net 8bp (stress)")

    # V2 trend gate
    sma50 = cl.rolling(50).mean()
    gate = (cl.shift(1) > sma50.shift(1))
    gated = onf.where(gate)
    v2 = gated.mean(axis=1, skipna=True).fillna(0)
    # cost only on names actually held
    held_frac = gate.reindex(columns=onf.columns).mean(axis=1)
    perf(v2 - cost * (held_frac > 0), "V2 trend-gated, net 4bp")

    # V3 vol targeted
    bvol = v1.rolling(20).std().shift(1) * np.sqrt(252)
    scale = (0.40 / bvol).clip(upper=1.5)
    v3 = v1 * scale
    perf(v3 - cost * scale, "V3 vol-target 40%, net")

    yearly = pd.DataFrame({
        "V1": (v1 - cost), "V2": (v2 - cost * (held_frac > 0)),
        "V3": (v3 - cost * scale)})
    print("\nper-year net returns:")
    yt = yearly.groupby(yearly.index.year).apply(lambda d: (1 + d).prod() - 1)
    print(yt.applymap(lambda v: f"{v:+.0%}").to_string())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    run()
