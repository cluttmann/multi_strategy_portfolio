"""
Reproduces REAL_EXTRA_DRAG in sma_sweep_acwi.py — the real-product cost
calibration of the synthetic 2× LETF formula.

Method: build the synthetic 2× S&P (engine formula, actual T-bill financing) on
the full US calendar, then compare month-end levels (own calendars — no
day-level intersection, which silently deletes market days and corrupts the
comparison) against the real Xtrackers S&P 500 2x Leveraged Daily Swap UCITS
ETF (XS2D.LSE, USD, TER 0.60%, unfunded swap — structurally the template for
the 2026 Scalable/Xtrackers 2× ACWI product).

Result (2026-07): synth CAGR 24.50% vs real 23.76% → synth runs +0.73%/yr rich;
monthly corr 0.95 (residual = LSE-midday vs US-close sampling at month ends).
Applied as REAL_EXTRA_DRAG = 0.75%/yr (rounded up — deliberate conservatism).

Stability: trimming the first 0/6/24 month-ends gives +0.73/+0.74/+0.79%/yr —
the estimate is NOT endpoint-fragile (measured band ~0.7-0.8%/yr).
It is, however, a US-underlying transfer — a 2× ACWI swap index carries a larger
dividend-withholding surface, so the real ACWI fund may lag slightly more.
Re-calibrate against the actual fund NAV once it trades:
    EODHD symbol will appear under the new ISIN; rerun with SYM_REAL swapped.
"""
import os, sys, requests
import numpy as np, pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extended_data as ed
from sma_sweep_acwi import make_lev

SYM_REAL = "XS2D.LSE"          # real 2× S&P swap UCITS (USD line)
START = "2010-04-01"


def real_levels(sym=SYM_REAL, start=START):
    r = requests.get(f"https://eodhd.com/api/eod/{sym}",
                     params={"api_token": os.environ["EODHD_TOKEN"], "fmt": "json",
                             "from": start, "to": "2099-01-01"}, timeout=60).json()
    df = pd.DataFrame(r); df.index = pd.to_datetime(df["date"])
    return df["adjusted_close"].astype(float)


def calibrate(trim_months=0):
    ext = ed.fetch_extended_data()
    spy = ext["spy_tr"].pct_change().dropna()
    spy = spy[spy.index >= START]                        # full US calendar
    b = ext["bil_daily_return"].reindex(spy.index).fillna(0).values
    synth_lvl = pd.Series(np.cumprod(1 + make_lev(spy.values, b, 2.0)),
                          index=spy.index)
    sm = synth_lvl.resample("M").last().pct_change().dropna()
    xm = real_levels().resample("M").last().pct_change().dropna()
    both = pd.concat([sm.rename("synth"), xm.rename("real")], axis=1).dropna()
    both = both.iloc[trim_months:-1]
    yrs = len(both) / 12
    ann = lambda s: ((1 + s).prod() ** (1 / yrs) - 1) * 100
    return {"months": len(both), "corr": both["synth"].corr(both["real"]),
            "synth_cagr": ann(both["synth"]), "real_cagr": ann(both["real"]),
            "diff": ann(both["synth"]) - ann(both["real"])}


if __name__ == "__main__":
    for trim in (0, 6, 24):
        r = calibrate(trim)
        print(f"trim {trim:>2} month-ends: n={r['months']}  corr={r['corr']:.4f}  "
              f"synth {r['synth_cagr']:.2f}%  real {r['real_cagr']:.2f}%  "
              f"diff {r['diff']:+.2f}%/yr")
    print("\n→ REAL_EXTRA_DRAG in sma_sweep_acwi.py = 0.0075 (base scenario; "
          "measured trim-stability band ~0.7-0.8%/yr)")
