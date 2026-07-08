"""
Modeled long-history daily MSCI ACWI (USD, gross-equivalent) back to 1988.

The point: real MSCI ACWI daily only starts 2001, and a MSCI-World-only proxy is
WRONG before then — World omits emerging markets, whose ACWI weight evolved
(<1% in 1988 → 6.8% in 1997 → ~4% post-EM-crises 2002 → ~12% today). Over
1988-2000 a World-only proxy understates ACWI by ~0.34%/yr for exactly that
reason.

Instead we anchor to the REAL ACWI monthly history — which embeds the true
time-varying EM weights by construction — taken from the Curvo MSCI ACWI series
(monthly, 1987+, EUR net TR) and FX-converted to USD. That conversion is
validated: converted-Curvo vs real MSCI ACWI USD-gross over 2001+ has monthly
corr 0.990 / drift −0.05%/yr (the net↔gross gap washes out).

Daily granularity (needed for an SMA gate) comes from temporal disaggregation:
within each pre-2001 month we take MSCI World's daily shape and rescale it
multiplicatively so the month compounds EXACTLY to the real ACWI monthly return.
World is used only for intra-month texture — every monthly return is the real
EM-share-weighted ACWI, not a World approximation.

Splice: modeled daily (1988→2001) → real MSCI ACWI gross daily (2001→2008) →
real ACWI ETF (2008+), the latter two already in extended_data's acwi_tr.

FX: USD/EUR = DEXUSEU (1999+); pre-1999 synthesised from the Deutsche Mark
(FRED EXGEUS, DEM/USD) via the locked 1 EUR = 1.95583 DEM rate.
"""
import os
from pathlib import Path
import numpy as np, pandas as pd, requests

DATA = Path(__file__).resolve().parent / "data"
CURVO = DATA / "ACWI_curvo_monthly_eur_1987-2026.csv"
OUT_CSV = DATA / "ACWI_modeled_daily_1970-2026.csv"
EUR_DEM = 1.95583
# Pre-ACWI segment: the index only exists from Dec-1987. Before that the honest
# predecessor IS MSCI World — at ACWI's 1988 inception EM weighed <1% and the
# MSCI EM index itself only starts 1988, so "all-country" ≈ World back then.
# We therefore extend 1970→1987 with World daily returns AS-IS (no anchor, no
# scaling) and label the segment accordingly. bil/cash exists from 1970-02.
PRE_ACWI_START = "1970-02-02"

# ACWI emerging-markets weight over time (MSCI; for documentation/QA only — the
# real monthly history already embeds these, we don't re-impose them).
EM_WEIGHT_HISTORY = {1988: 0.008, 1997: 0.068, 2002: 0.040, 2010: 0.135,
                     2021: 0.130, 2026: 0.100}


def _fred(sid):
    r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                     params={"series_id": sid, "api_key": os.environ["FREDKEY"],
                             "file_type": "json"}, timeout=60).json()
    s = pd.DataFrame(r["observations"])
    s["date"] = pd.to_datetime(s["date"])
    s = s[s.value != "."]
    return s.assign(v=lambda d: d.value.astype(float)).set_index("date")["v"]


def usd_per_eur_monthly() -> pd.Series:
    """Month-end USD/EUR: DEXUSEU 1999+, synthetic DEM-based before."""
    eu = _fred("DEXUSEU").resample("M").last()
    dem = _fred("EXGEUS")                       # DEM per USD, monthly (to 1998)
    syn = (EUR_DEM / dem).resample("M").last()  # USD per EUR pre-euro
    return pd.concat([syn[syn.index < "1999-01-01"],
                      eu[eu.index >= "1999-01-01"]]).sort_index()


def real_acwi_usd_monthly() -> pd.Series:
    """Real MSCI ACWI monthly USD total return (Curvo EUR-net × FX), 1988+."""
    c = pd.read_csv(CURVO)
    c.columns = ["m", "lvl"]
    c["date"] = pd.to_datetime(c["m"], format="%m/%Y") + pd.offsets.MonthEnd(0)
    c = c.set_index("date")["lvl"].astype(float)
    fx_ret = usd_per_eur_monthly().pct_change()
    return ((1 + c.pct_change()) * (1 + fx_ret) - 1).dropna()


def build(world_daily: pd.Series, real_acwi_daily: pd.Series,
          save: bool = True) -> pd.Series:
    """Return modeled daily ACWI total-return series (1988+).

    world_daily      : MSCI World daily returns (extended_data urth_tr.pct_change)
    real_acwi_daily  : real ACWI daily returns (extended_data acwi_tr.pct_change),
                       used as-is from its 2001 start.
    """
    splice = real_acwi_daily.dropna().index[0]            # ~2001-01-02
    acwi_m = real_acwi_usd_monthly()                       # real, EM-weighted
    # Disaggregate only WHOLE months strictly before the splice month; the splice
    # month itself comes entirely from real daily ACWI. (Avoids scaling a 1-day
    # partial month to a full-month return — a spurious single-day spike.)
    splice_month_start = splice.replace(day=1)
    w = world_daily.dropna()
    w = w[w.index < splice_month_start]                    # pre-splice-month daily World

    # multiplicative temporal disaggregation: World shape, real ACWI monthly drift
    parts = []
    wm = w.groupby(w.index.to_period("M"))
    for per, day_rets in wm:
        ts = per.to_timestamp("M")
        if ts not in acwi_m.index:
            continue
        W = (1 + day_rets).prod() - 1
        A = acwi_m.loc[ts]
        scale = ((1 + A) / (1 + W)) ** (1.0 / len(day_rets))
        parts.append((1 + day_rets) * scale - 1)
    modeled_pre = pd.concat(parts).sort_index()
    modeled_pre = modeled_pre[modeled_pre.index >= "1988-01-01"]

    # 1970→1987: MSCI World as-is (ACWI predecessor — see PRE_ACWI_START note)
    world_head = world_daily.dropna()
    world_head = world_head[(world_head.index >= PRE_ACWI_START)
                            & (world_head.index < "1988-01-01")]

    full = pd.concat([world_head, modeled_pre,
                      real_acwi_daily.loc[splice:]]).sort_index()
    full = full[~full.index.duplicated()]

    if save:
        lvl = (1 + full).cumprod() * 100.0
        df = pd.DataFrame({"date": full.index,
                           "return_pct": full.values * 100.0,
                           "balance": lvl.values})
        df.to_csv(OUT_CSV, index=False, float_format="%.6f")
    return full


def load_or_build(ext) -> pd.Series:
    """Cached daily modeled ACWI returns; (re)builds from extended_data if absent."""
    if OUT_CSV.exists():
        df = pd.read_csv(OUT_CSV, parse_dates=["date"]).set_index("date")
        return df["return_pct"] / 100.0
    return build(ext["urth_tr"].pct_change(), ext["acwi_tr"].pct_change())


if __name__ == "__main__":
    import extended_data as ed
    ext = ed.fetch_extended_data()
    s = build(ext["urth_tr"].pct_change(), ext["acwi_tr"].pct_change())
    print(f"\nModeled ACWI daily: {len(s)} days  {s.index[0].date()} → {s.index[-1].date()}")
    yrs = len(s) / 252
    print(f"CAGR {((1+s).prod()**(1/yrs)-1)*100:.2f}%  → {OUT_CSV.name}")

    # QA: pre-2001 monthly aggregates must equal the real ACWI monthly anchor
    am = real_acwi_usd_monthly()
    mm = (1 + s[s.index < "2001-01-01"]).groupby(
        s[s.index < "2001-01-01"].index.to_period("M")).prod() - 1
    chk = pd.concat([am.rename("real"),
                     mm.rename("modeled").set_axis(mm.index.to_timestamp("M"))],
                    axis=1).dropna()
    err = (chk["real"] - chk["modeled"]).abs().max()
    print(f"QA pre-2001 monthly reconstruction max error: {err:.2e} (should be ~0)")
    # World-only would have understated returns — show the gap captured
    world = ext["urth_tr"].pct_change()
    wpre = (1 + world[(world.index >= "1988-01-01") & (world.index < "2001-01-01")]).prod()
    mpre = (1 + s[(s.index >= "1988-01-01") & (s.index < "2001-01-01")]).prod()
    print(f"1988-2000 growth: modeled(real EM) {mpre:.3f}× vs World-only {wpre:.3f}× "
          f"(+{(mpre/wpre-1)*100:.1f}% EM contribution captured)")
    head = s[s.index < "1988-01-01"]
    print(f"1970-1987 head (World as-is): {len(head)} days, "
          f"CAGR {((1+head).prod()**(252/len(head))-1)*100:.2f}%")
