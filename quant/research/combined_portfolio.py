"""Combined-portfolio and leverage analysis vs the 50%/yr goal.

    python3 -m quant.research.combined_portfolio --run

Stacks the sleeves measured so far (daily net return series, all costs in):
  ONX   overnight 3x universe, V2 trend-gated (the core engine)
  VOLC  SVXY contango>3% (vol carry)
  CTREND crypto TSMOM net (mapped onto trading days)
Computes correlations, an equal-vol-weighted stack, and the leverage ladder
with margin financing charged on borrow above 1x (BM+1.5% ≈ 7%/yr — charged
even though paper doesn't bill it, because live would).

Honesty rules: no weight optimization on realized returns (that's in-sample
overfitting at the portfolio level) — weights are inverse-vol only. MaxDD and
the worst-year column are reported next to every CAGR.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.research.exotic_sleeves import alpaca_daily, fred
from quant.research.overnight_universe import discover_universe
from quant.config import BOT_TICKERS

MARGIN_RATE = 0.07  # annual, on gross above 1x


def onx_v2() -> pd.Series:
    univ = [s for s in discover_universe() if s not in BOT_TICKERS]
    on, cl = {}, {}
    for s in univ:
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
    return (v2 - (4 / 1e4) * held).rename("ONX")


def vol_carry() -> pd.Series:
    vix, vix3m = fred("VIXCLS"), fred("VIX3MCLS")
    if vix3m.empty:
        vix3m = fred("VXVCLS")
    svxy = alpaca_daily("SVXY", "2016-01-01")
    ret = svxy["c"].pct_change()
    contango = (vix3m.reindex(svxy.index).ffill()
                / vix.reindex(svxy.index).ffill() - 1).shift(1)
    pos = (contango > 0.03).astype(float)
    turn = pos.diff().abs().fillna(0)
    return (pos * ret - turn * 3 / 1e4).rename("VOLC")


def crypto_trend() -> pd.Series:
    from quant.research.exotic_sleeves import CRYPTO
    px = {}
    for s in CRYPTO:
        try:
            px[s] = alpaca_daily(s, "2021-01-01", crypto=True)["c"]
        except Exception:  # noqa: BLE001
            pass
    close = pd.DataFrame(px).ffill()
    ret = close.pct_change()
    sig = ((close > close.rolling(20).mean())
           & (close > close.rolling(50).mean())
           & (close.pct_change(20) > 0)).shift(1)
    vol20 = ret.rolling(20).std() * np.sqrt(365)
    w = (sig * (0.40 / vol20.shift(1)).clip(upper=1.0))
    w = w.div(w.sum(axis=1).clip(lower=1.0), axis=0)
    strat = (w * ret).sum(axis=1) - w.diff().abs().sum(axis=1).fillna(0) * 25 / 1e4
    return strat.rename("CTREND")


def stats(r: pd.Series, days=252):
    r = r.dropna()
    eq = (1 + r).cumprod()
    yrs = len(r) / days
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    sh = r.mean() / r.std() * np.sqrt(days) if r.std() > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    worst = r.groupby(r.index.year).apply(lambda x: (1 + x).prod() - 1).min()
    return cagr, sh, dd, worst


def xsr() -> pd.Series:
    import os
    from quant.config import STAGING_DIR
    path = os.path.join(STAGING_DIR, "sim_wf_v1.parquet")
    df = pd.read_parquet(path)
    s = df["net_ret"]
    s.index = pd.to_datetime(s.index)
    return s.rename("XSR")


def run():
    print("building sleeve series ...")
    onx = onx_v2()
    volc = vol_carry()
    ct = crypto_trend()
    xs = xsr()
    panel = pd.concat([onx, volc, ct, xs], axis=1).loc["2016":]
    # crypto only exists 2021+; treat pre-2021 as 0 (not deployed)
    panel = panel.fillna(0.0)

    print("\nsleeve stats (net):")
    for c in panel.columns:
        cagr, sh, dd, worst = stats(panel[c])
        print(f"  {c:7s} CAGR={cagr:+7.1%}  Sharpe={sh:5.2f}  MaxDD={dd:6.1%}  "
              f"worst yr={worst:+.0%}")

    print("\ncorrelations (daily):")
    print(panel.corr().round(2).to_string())

    # inverse-vol weights (no return-based optimization)
    vols = panel.loc["2021":].std() * np.sqrt(252)
    w = (1 / vols) / (1 / vols).sum()
    print(f"\ninverse-vol weights: " +
          ", ".join(f"{c}={w[c]:.0%}" for c in panel.columns))
    stack = (panel * w).sum(axis=1)

    print("\n=== leverage ladder on the stack (financing charged >1x) ===")
    print(f"{'gross':>6s} {'CAGR':>8s} {'Sharpe':>7s} {'MaxDD':>7s} "
          f"{'worst yr':>9s}")
    for g in [1.0, 1.25, 1.5, 1.75, 2.0, 2.5]:
        lev = stack * g - max(g - 1, 0) * MARGIN_RATE / 252
        cagr, sh, dd, worst = stats(lev)
        flag = "  ← 50%+" if cagr >= 0.50 else ""
        print(f"{g:6.2f} {cagr:+8.1%} {sh:7.2f} {dd:7.1%} {worst:+9.0%}{flag}")

    print("\n=== ONX-only leverage ladder (the single best engine) ===")
    for g in [1.0, 1.25, 1.5, 1.75, 2.0]:
        lev = onx.fillna(0) * g - max(g - 1, 0) * MARGIN_RATE / 252
        cagr, sh, dd, worst = stats(lev)
        flag = "  ← 50%+" if cagr >= 0.50 else ""
        print(f"{g:6.2f} {cagr:+8.1%} {sh:7.2f} {dd:7.1%} {worst:+9.0%}{flag}")

    # per-year table of the 1.5x stack
    lev = stack * 1.5 - 0.5 * MARGIN_RATE / 252
    yearly = lev.groupby(lev.index.year).apply(lambda x: (1 + x).prod() - 1)
    print("\n1.5x stack per-year: " +
          "  ".join(f"{y}:{v:+.0%}" for y, v in yearly.items()))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    run()
