"""Options Phase-A — systematic defined-risk premium selling (SPY/QQQ).

    python3 -m quant.research.options_phase_a --run

The last untested instrument class. Data reality: Alpaca options BARS and
TRADES exist since 2024-02 (~2.4y); QUOTES/greeks history does not exist
anywhere → entry/exit prices are approximated from daily bar closes of the
contracts with a WIDE cost haircut (options spreads >> equity spreads).

Strategy family under test (the canonical retail-feasible short-vol trade):
  weekly short put spread on SPY and QQQ
  - each Monday: sell the ~30-delta put, buy the put 2% further OTM,
    ~4-11 DTE (nearest Friday expiry), hold to expiry, cash-settle P&L from
    the underlying's Friday close (defined risk, no early management)
  - delta proxied via strike distance: short strike ≈ 2% OTM of Monday close
    (no historical greeks → strike-distance proxy, disclosed)
  - vol filter variant: only sell when VIX < 25 (calm regime)
Costs: entry credit haircut 15% of credit + $0.02/leg commission-equivalent;
this is deliberately punitive to reflect unknown historical spreads.

2.4 years cannot clear the gauntlet — this is an existence read on whether
the premium collected survives the realized path (incl. Aug-2024 vol spike,
2025 drawdowns, 2026 YTD).
"""

import argparse
import sys

import numpy as np
import pandas as pd
import requests

from quant.config import ALPACA_KEY_PAPER, ALPACA_SECRET_PAPER
from quant.research.exotic_sleeves import alpaca_daily, fred

H = {"APCA-API-KEY-ID": ALPACA_KEY_PAPER,
     "APCA-API-SECRET-KEY": ALPACA_SECRET_PAPER}

UNDERLYINGS = ["SPY", "QQQ"]
OTM_SHORT = 0.02   # short strike ~2% OTM
WIDTH = 0.02       # long strike 2% further OTM
CREDIT_HAIRCUT = 0.15


def occ(sym: str, expiry: pd.Timestamp, cp: str, strike: float) -> str:
    return f"{sym}{expiry:%y%m%d}{cp}{int(round(strike * 1000)):08d}"


def bar_close(contract: str, day: pd.Timestamp) -> float | None:
    r = requests.get(f"https://data.alpaca.markets/v1beta1/options/bars",
                     headers=H, params={
                         "symbols": contract, "timeframe": "1Day",
                         "start": f"{day:%Y-%m-%d}T00:00:00Z",
                         "end": f"{day:%Y-%m-%d}T23:59:59Z", "limit": 5},
                     timeout=30)
    if not r.ok:
        return None
    bars = (r.json().get("bars") or {}).get(contract) or []
    return bars[-1]["c"] if bars else None


OTM_GRID = [0.015, 0.02, 0.03]
WIDTH_GRID = [0.01, 0.02]
VIX_FILTER_GRID = [True, False]


def weekly_returns(df: pd.DataFrame) -> pd.Series:
    """ret_on_risk-Spalte als Rendite-Serie, indiziert auf das Wochendatum —
    der gemeinsame Nenner, den trials_registry.log_trial erwartet."""
    s = df.set_index("date")["ret_on_risk"].sort_index()
    s.index = pd.to_datetime(s.index)
    return s


def simulate(otm_short: float = OTM_SHORT, width: float = WIDTH,
            vix_filter: bool = False) -> pd.DataFrame:
    """Reine Backtest-Funktion für EINE Variante — liefert die Wochenzeilen,
    schreibt nichts. `run_all_variants()` und `run()` sind die I/O-Wrapper."""
    vix = fred("VIXCLS", start="2024-01-01")
    results = []
    for u in UNDERLYINGS:
        px = alpaca_daily(u, "2024-02-01")["c"]
        px.index = pd.to_datetime(px.index)
        mondays = [d for d in px.index if d.weekday() == 0]
        for d in mondays:
            spot = px[d]
            expiry = d + pd.Timedelta(days=(4 - d.weekday()) % 7)
            if expiry <= d:
                expiry += pd.Timedelta(days=7)
            if expiry not in px.index:
                continue
            v = vix.reindex([d]).ffill().iloc[-1]
            if vix_filter and v >= 25:
                continue
            k_short = round(spot * (1 - otm_short))
            k_long = round(spot * (1 - otm_short - width))
            cs = occ(u, expiry, "P", k_short)
            cl = occ(u, expiry, "P", k_long)
            p_short = bar_close(cs, d)
            p_long = bar_close(cl, d)
            if p_short is None or p_long is None:
                continue
            credit = (p_short - p_long) * (1 - CREDIT_HAIRCUT) - 0.04
            if credit <= 0:
                continue
            settle = px[expiry]
            payoff = -max(k_short - settle, 0) + max(k_long - settle, 0)
            pnl = credit + payoff
            width_usd = k_short - k_long
            results.append({"u": u, "date": d, "vix": v, "credit": credit,
                            "pnl": pnl, "max_loss": width_usd - credit,
                            "ret_on_risk": pnl / (width_usd - credit)})
    return pd.DataFrame(results)


def run_all_variants():
    """Das vorregistrierte 12-Varianten-Raster ({OTM 1.5/2/3%} x
    {Breite 1/2%} x {VIX-Filter an/aus}) — jede Variante wird bei
    trials_registry protokolliert, BEVOR irgendeine für die Beförderung
    ausgewählt wird. Das ist die Lektion aus dem G5-Vorfall (XSR sprang
    zwischen DSR 0.996/0.611, je nachdem ob der Modell-Zoo mitzählte, weil
    das Variantenraster nicht vorher fixiert war)."""
    from quant.research.trials_registry import log_trial
    logged = []
    for otm in OTM_GRID:
        for width in WIDTH_GRID:
            for vf in VIX_FILTER_GRID:
                label = f"otm{otm}_w{width}_vix{'on' if vf else 'off'}"
                df = simulate(otm, width, vf)
                if len(df) < 20:
                    print(f"{label}: nur {len(df)} Wochen — überspringe")
                    continue
                r = weekly_returns(df)
                d = log_trial(family="OPTPREM", returns=r, variant=label,
                             ann=52, config={"otm": otm, "width": width,
                                             "vix_filter": vf})
                logged.append({"variant": label, **d})
    return pd.DataFrame(logged)


def run():
    """Unveränderter Einzellauf mit den bisherigen Default-Parametern —
    behält die ursprüngliche `--run`-Semantik für Ad-hoc-Checks."""
    df = simulate(OTM_SHORT, WIDTH, vix_filter=False)
    if df.empty:
        print("no fills — options bar data too sparse for these strikes")
        return
    print(f"\n{len(df)} weekly spreads with data")

    def block(label, sub):
        if len(sub) < 10:
            return
        wins = (sub.pnl > 0).mean()
        rr = sub.ret_on_risk
        ann = rr.mean() * 52
        print(f"{label:32s} n={len(sub):4d}  win={wins:4.0%}  "
              f"avg P&L/risk={rr.mean()*100:+5.1f}%  ann≈{ann*100:+6.0f}% "
              f"worst wk={rr.min()*100:+.0f}%")

    block("all weeks", df)
    for u in UNDERLYINGS:
        block(f"  {u} only", df[df.u == u])
    df.to_parquet("quant/_staging/options_phase_a.parquet")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    p.add_argument("--variants", action="store_true")
    args = p.parse_args()
    if args.variants:
        out = run_all_variants()
        print(out[["variant", "sharpe_net", "cagr_net", "dsr"]]
              .to_string(index=False))
    elif args.run:
        run()
    else:
        p.print_help()
        sys.exit(1)
