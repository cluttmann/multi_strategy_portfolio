"""
Extended-history data layer.

Fetches long-history proxies (Vanguard mutual funds, indices, FRED yields) for
each asset class used by the production strategies, then splices them with the
real ETFs at the appropriate inception dates.

Output: clean total-return DataFrames covering 1987-01-01 → present.

Splice convention: pre-inception → use proxy; on/after inception → use real ETF.
Both series are total-return scaled so the splice point is continuous.
"""
from __future__ import annotations
import os
import requests
import pandas as pd
import numpy as np
from pathlib import Path

# SIM CSVs live in research/data/.
SIM_DIR = Path(__file__).resolve().parent / "data"

# Cache stays in /tmp — regenerable, would just pollute the repo.
CACHE_DIR = "/tmp/mega_backtest/_cache_extended"
os.makedirs(CACHE_DIR, exist_ok=True)

EODHD_TOKEN_ENV = "EODHD_TOKEN"
EXTENDED_START = "1970-01-02"  # Pushed from 1987 — EFASIM/NTSDSIM/URTHSIM all reach 1970; TLTSIM/IEFSIM 1962; GLDSIM/SLVSIM 1968
EXTENDED_END = "2026-12-31"


def eodhd_token():
    tok = os.environ.get(EODHD_TOKEN_ENV)
    if not tok:
        raise RuntimeError(f"Set {EODHD_TOKEN_ENV} env var with your EODHD API token")
    return tok


def fetch_eodhd_series(symbol: str, start: str = EXTENDED_START,
                       end: str = EXTENDED_END,
                       asset_type: str = "us") -> pd.Series:
    """Fetch a single price series from EODHD. Returns daily adjusted close."""
    cache_path = f"{CACHE_DIR}/{symbol.replace('.', '_').replace('/', '_')}.parquet"
    if os.path.exists(cache_path):
        df = pd.read_parquet(cache_path)
        return df["close"]

    suffix_map = {"us": ".US", "index": ".INDX", "cc": ".CC"}
    suffix = suffix_map.get(asset_type, ".US")
    eod_sym = symbol if any(symbol.endswith(s) for s in suffix_map.values()) else f"{symbol}{suffix}"

    r = requests.get(f"https://eodhd.com/api/eod/{eod_sym}",
                     params={"api_token": eodhd_token(), "fmt": "json",
                             "from": start, "to": end}, timeout=30)
    if r.status_code != 200:
        print(f"  ✗ {symbol}: HTTP {r.status_code}")
        return None
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        print(f"  ✗ {symbol}: no data returned")
        return None

    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    df = df.set_index("t").sort_index()
    close_col = "adjusted_close" if "adjusted_close" in df.columns else "close"
    series = df[close_col].rename("close")
    series.to_frame().to_parquet(cache_path)
    print(f"  ✓ {symbol}: {len(series)} bars ({series.index[0].date()} → {series.index[-1].date()})")
    return series


def fetch_fred_yield_series(series_id: str = "DGS3MO",
                              start: str = EXTENDED_START) -> pd.Series:
    """Fetch a yield series from FRED. Returns annualized percent yield."""
    cache_path = f"{CACHE_DIR}/fred_{series_id}.parquet"
    if os.path.exists(cache_path):
        df = pd.read_parquet(cache_path)
        return df["yield"]
    # Use FRED's public CSV endpoint (no auth needed)
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    df = pd.read_csv(url)
    df.columns = ["date", "yield"]
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    df["yield"] = pd.to_numeric(df["yield"], errors="coerce")
    df = df.dropna()
    df = df.loc[start:]
    df.to_parquet(cache_path)
    print(f"  ✓ FRED {series_id}: {len(df)} obs ({df.index[0].date()} → {df.index[-1].date()})")
    return df["yield"]


def yield_to_daily_return(yield_pct: pd.Series) -> pd.Series:
    """Convert annualized yield (%) to daily return. yield_pct of 5.0 → 5% annual."""
    daily_yield = yield_pct / 100.0 / 252.0
    return daily_yield


# ────────────────────────────────────────────────────────────────────────
# Splicing helpers
# ────────────────────────────────────────────────────────────────────────

def splice_series(proxy: pd.Series, real: pd.Series,
                  splice_date: str = None) -> pd.Series:
    """
    Splice a proxy series with a real series at the real series' inception
    (or an explicit splice_date). Both are scaled so the splice point matches.

    Result is a continuous returns series. After splicing, the level is
    rescaled so the real-ETF era is exact and the proxy era is shifted.
    """
    real = real.dropna()
    if real.empty:
        return proxy
    if splice_date is None:
        splice_date = real.index[0]
    else:
        splice_date = pd.to_datetime(splice_date)

    # Find the splice anchor — last common date before splice or first real date
    proxy_pre = proxy[proxy.index < splice_date]
    if proxy_pre.empty:
        return real

    # Convert both to returns, concatenate, rebuild level
    proxy_rets = proxy_pre.pct_change().fillna(0)
    real_rets = real.pct_change().fillna(0)
    combined_rets = pd.concat([proxy_rets, real_rets])
    combined_rets = combined_rets[~combined_rets.index.duplicated(keep="last")].sort_index()

    # Rebuild price level starting from proxy's first value
    level = (1 + combined_rets).cumprod() * float(proxy.iloc[0])
    return level


# ────────────────────────────────────────────────────────────────────────
# The extended data fetcher — primary entry point
# ────────────────────────────────────────────────────────────────────────

def _clean_series(s: pd.Series, max_daily_return: float = 0.25) -> pd.Series:
    """Scrub obviously-bad data points. Any day with |return| > max_daily_return
    is treated as a data error: we replace it with the previous value (carry-forward).

    Returns are recomputed from the cleaned price series, eliminating outliers.
    """
    if s is None or len(s) < 2:
        return s
    s = s.dropna().copy()
    daily_ret = s.pct_change()
    bad_mask = daily_ret.abs() > max_daily_return
    if bad_mask.any():
        bad_dates = s.index[bad_mask].tolist()
        for d in bad_dates:
            pos = s.index.get_loc(d)
            if pos > 0:
                s.iloc[pos] = s.iloc[pos - 1]  # carry-forward
        print(f"    cleaned {bad_mask.sum()} outliers (>|{max_daily_return*100:.0f}%| daily)")
    return s


def fetch_extended_data() -> dict:
    """
    Returns a dict of spliced total-return series + raw proxies.
    Each entry is a daily-frequency pd.Series indexed by trading days.

    Keys:
      'spy_tr'    : US large-cap total return (VFINX pre-1993, SPY post)
      'efa_tr'    : Intl developed total return (AEPGX pre-2001, EFA post)
      'tlt_tr'    : 20+y Treasury total return (VUSTX pre-2002, TLT post)
      'ief_tr'    : 7-10y Treasury total return (VFITX pre-2002, IEF post)
      'gld_tr'    : Gold total return (XAU pre-2004, GLD post)
      'dbc_tr'    : Commodity total return (BCOM pre-2006, DBC post)
      'bil_tr'    : Cash / 3m T-bill (FRED DGS3MO → daily return)
      'qqq_tr'    : Nasdaq-100 (QQQSIM 1986+, real QQQ post-1999-03-10)
      'agg_tr'    : Aggregate bond (VBMFX pre-2003, AGG post)
      'hyg_tr'    : High-yield corporate (VWEHX pre-2007, HYG post)
      'lqd_tr'    : Investment-grade corporate (VWESX pre-2002, LQD post)
      'tip_tr'    : TIPS (VIPSX pre-2003, TIP post)
      'kmlm_tr'   : Managed futures (KMLMSIM 1988+, real KMLM post-2020-12-02)
      'ntsd_tr'   : WisdomTree US Plus Intl Equity (NTSDSIM 1970+, real NTSD post-2026-03-19)
      'urth_tr'   : MSCI World (URTHSIM 1970+, real URTH post-2012-01-12)
      'acwi_tr'   : MSCI ACWI gross TR, USD (MSCI EOD index 2001+, real ACWI ETF post-2008-03-28)
      'bnd_tr'    : Total US bond market (BNDSIM 1986+, real BND post-2007-04-03)
      'slv_tr'    : Silver (SLVSIM 1968+, real SLV post-2006-04-21)
    """
    print("Fetching extended-history proxies from EODHD + FRED...")
    out = {}

    # Helper to fetch + clean
    def f(sym, **kw):
        s = fetch_eodhd_series(sym, **kw)
        return _clean_series(s) if s is not None else None

    # US large-cap equity: SPYSIM (Testfolio 1885+) → real SPY (1993-01-29+)
    # Replaces older VFINX proxy — Testfolio's model carries proper SPY ER and
    # extends the window dramatically (1885 vs 1976 for VFINX).
    spy = f("SPY")
    SPYSIM_PATH = str(SIM_DIR / "SPYSIM_daily_returns_1885-2026.csv")
    if os.path.exists(SPYSIM_PATH):
        sim = pd.read_csv(SPYSIM_PATH)
        sim["date"] = pd.to_datetime(sim["date"])
        sim = sim.set_index("date").sort_index()
        sim_level = (1 + sim["return_pct"] / 100.0).cumprod() * 100.0
        if spy is not None and len(spy) > 0:
            out["spy_tr"] = splice_series(sim_level, spy, splice_date="1993-01-29")
            print(f"  ✓ SPYSIM: spliced with real SPY at 1993-01-29 ({len(sim)} sim days, back to {sim.index[0].date()})")
        else:
            out["spy_tr"] = sim_level
    else:
        vfinx = f("VFINX")
        out["spy_tr"] = splice_series(vfinx, spy)
        print(f"  ⚠ SPYSIM CSV not found at {SPYSIM_PATH}, falling back to VFINX proxy")

    # Intl developed: EFASIM (Testfolio MSCI EAFE 1970+) → real EFA (2001-08-14+)
    # Replaces AEPGX proxy — AEPGX is an actively-managed fund (cash-cushioned,
    # ~27% lower vol than EFA), so EFA pre-2001 wasn't pure beta. EFASIM is the
    # actual MSCI EAFE index model.
    efa = f("EFA")
    EFASIM_PATH = str(SIM_DIR / "EFASIM_daily_returns_1970-2026.csv")
    if os.path.exists(EFASIM_PATH):
        sim = pd.read_csv(EFASIM_PATH)
        sim["date"] = pd.to_datetime(sim["date"])
        sim = sim.set_index("date").sort_index()
        sim_level = (1 + sim["return_pct"] / 100.0).cumprod() * 100.0
        if efa is not None and len(efa) > 0:
            out["efa_tr"] = splice_series(sim_level, efa, splice_date="2001-08-14")
            print(f"  ✓ EFASIM: spliced with real EFA at 2001-08-14 ({len(sim)} sim days; replaces AEPGX active-fund proxy)")
        else:
            out["efa_tr"] = sim_level
    else:
        aepgx = f("AEPGX")
        out["efa_tr"] = splice_series(aepgx, efa) if aepgx is not None else efa
        print(f"  ⚠ EFASIM CSV not found at {EFASIM_PATH}, falling back to AEPGX active-fund proxy")

    # LT Treasury: TLTSIM (Testfolio 1962+) → real TLT (2002-07-30+)
    # Replaces VUSTX proxy (corr 0.96, slight duration variance: VUSTX ~20yr,
    # TLT 20+yr). TLTSIM is the Testfolio model of the 20+yr Treasury return
    # series with TLT's expense ratio applied.
    tlt = f("TLT")
    TLTSIM_PATH = str(SIM_DIR / "TLTSIM_daily_returns_1962-2026.csv")
    if os.path.exists(TLTSIM_PATH):
        sim = pd.read_csv(TLTSIM_PATH)
        sim["date"] = pd.to_datetime(sim["date"])
        sim = sim.set_index("date").sort_index()
        sim_level = (1 + sim["return_pct"] / 100.0).cumprod() * 100.0
        if tlt is not None and len(tlt) > 0:
            out["tlt_tr"] = splice_series(sim_level, tlt, splice_date="2002-07-30")
            print(f"  ✓ TLTSIM: spliced with real TLT at 2002-07-30 ({len(sim)} sim days)")
        else:
            out["tlt_tr"] = sim_level
    else:
        vustx = f("VUSTX")
        out["tlt_tr"] = splice_series(vustx, tlt) if vustx is not None else tlt
        print(f"  ⚠ TLTSIM CSV not found at {TLTSIM_PATH}, falling back to VUSTX proxy")

    # IT Treasury: IEFSIM (Testfolio 1962+) → real IEF (2002-07-30+)
    # Replaces VFITX proxy (corr 0.93, slight duration variance: VFITX 5-10yr,
    # IEF 7-10yr). IEFSIM models the 7-10yr Treasury return series with IEF's ER.
    ief = f("IEF")
    IEFSIM_PATH = str(SIM_DIR / "IEFSIM_daily_returns_1962-2026.csv")
    if os.path.exists(IEFSIM_PATH):
        sim = pd.read_csv(IEFSIM_PATH)
        sim["date"] = pd.to_datetime(sim["date"])
        sim = sim.set_index("date").sort_index()
        sim_level = (1 + sim["return_pct"] / 100.0).cumprod() * 100.0
        if ief is not None and len(ief) > 0:
            out["ief_tr"] = splice_series(sim_level, ief, splice_date="2002-07-30")
            print(f"  ✓ IEFSIM: spliced with real IEF at 2002-07-30 ({len(sim)} sim days)")
        else:
            out["ief_tr"] = sim_level
    else:
        vfitx = f("VFITX")
        out["ief_tr"] = splice_series(vfitx, ief) if vfitx is not None else ief
        print(f"  ⚠ IEFSIM CSV not found at {IEFSIM_PATH}, falling back to VFITX proxy")

    # Gold: GLDSIM (Testfolio 1968+, REAL gold spot) → real GLD (2004-11-18+)
    # CRITICAL FIX: previously used XAU = Philadelphia Gold/Silver MINING Index,
    # which is mining stocks (vol ~38% ann, equity-like), not gold (vol ~18%).
    # GLDSIM is the actual gold price model — clean, no equity beta.
    gld = f("GLD")
    GLDSIM_PATH = str(SIM_DIR / "GLDSIM_daily_returns_1968-2026.csv")
    if os.path.exists(GLDSIM_PATH):
        sim = pd.read_csv(GLDSIM_PATH)
        sim["date"] = pd.to_datetime(sim["date"])
        sim = sim.set_index("date").sort_index()
        sim_level = (1 + sim["return_pct"] / 100.0).cumprod() * 100.0
        if gld is not None and len(gld) > 0:
            out["gld_tr"] = splice_series(sim_level, gld, splice_date="2004-11-18")
            print(f"  ✓ GLDSIM: spliced with real GLD at 2004-11-18 ({len(sim)} sim days; replaces XAU mining-stock proxy)")
        else:
            out["gld_tr"] = sim_level
    else:
        # Last-resort fallback to XAU (with warning)
        xau = f("XAU", asset_type="index")
        out["gld_tr"] = splice_series(xau, gld) if xau is not None else gld
        print(f"  ⚠ GLDSIM CSV not found at {GLDSIM_PATH}, falling back to XAU mining-stock proxy (POOR QUALITY)")

    # Commodity: SPGSCI → BCOM → DBC
    spgsci = f("SPGSCI", asset_type="index")
    bcom = f("BCOM", asset_type="index")
    dbc = f("DBC")
    # Splice 3-way: SPGSCI pre-1991, BCOM 1991-2006, DBC 2006+
    if spgsci is not None and bcom is not None:
        commodity_proxy = splice_series(spgsci, bcom, splice_date="1991-01-02")
    elif bcom is not None:
        commodity_proxy = bcom
    else:
        commodity_proxy = None
    if commodity_proxy is not None and dbc is not None:
        out["dbc_tr"] = splice_series(commodity_proxy, dbc, splice_date="2006-02-03")
    else:
        out["dbc_tr"] = dbc

    # Cash: 3m T-bill yield → daily return
    # DGS3MO (daily) only starts 1981-09-04. Use TB3MS (monthly, since 1934) as
    # the pre-1981 proxy and splice. Critical for SPXL SMA + other LETFs running
    # back to 1970 — without this, their borrow cost is 0% pre-1981, dramatically
    # overstating CAGR during a high-rate era.
    fred_yield = fetch_fred_yield_series("DGS3MO")
    daily_idx = out["spy_tr"].index
    try:
        tb3ms_yield = fetch_fred_yield_series("TB3MS")
        # TB3MS is monthly — forward-fill to daily within months.
        if tb3ms_yield is not None and len(tb3ms_yield) > 0:
            tb3ms_first = tb3ms_yield.index[0]
            dgs_first = fred_yield.index[0]
            # Use TB3MS for everything before DGS3MO's first date
            pre_dgs = tb3ms_yield[tb3ms_yield.index < dgs_first]
            combined = pd.concat([pre_dgs, fred_yield]).sort_index()
            combined = combined[~combined.index.duplicated(keep="last")]
            fred_yield = combined
            print(f"  ✓ Cash rate: TB3MS ({tb3ms_first.date()}+) → DGS3MO ({dgs_first.date()}+) spliced")
    except Exception as e:
        print(f"  ⚠ TB3MS fetch failed ({e}), pre-1981 LETF borrow may be understated")
    fred_daily = fred_yield.reindex(daily_idx, method="ffill") / 100.0 / 252.0
    out["bil_daily_return"] = fred_daily

    # Nasdaq: QQQSIM (Testfolio 1986+) → real QQQ (1999-03-10+)
    QQQSIM_PATH = str(SIM_DIR / "QQQSIM_daily_returns_1986-2026.csv")
    qqq_real = f("QQQ")
    if os.path.exists(QQQSIM_PATH):
        sim = pd.read_csv(QQQSIM_PATH)
        sim["date"] = pd.to_datetime(sim["date"])
        sim = sim.set_index("date").sort_index()
        sim_level = (1 + sim["return_pct"] / 100.0).cumprod() * 100.0
        if qqq_real is not None and len(qqq_real) > 0:
            out["qqq_tr"] = splice_series(sim_level, qqq_real, splice_date="1999-03-10")
            print(f"  ✓ QQQSIM: spliced with real QQQ at 1999-03-10 ({len(sim)} sim days)")
        else:
            out["qqq_tr"] = sim_level
    else:
        out["qqq_tr"] = qqq_real
        print(f"  ⚠ QQQSIM CSV not found at {QQQSIM_PATH}")

    # Aggregate bonds: route AGG through the same BNDSIM (Testfolio) — AGG and
    # BND are the same asset class (Bloomberg US Aggregate exposure). The user
    # opted to swap the lower-quality VBMFX→AGG splice (corr 0.81 at boundary)
    # for BNDSIM, which is a Testfolio Tier-A reconstruction. AGG real ETF is
    # then spliced over at AGG's inception.
    agg = f("AGG")
    # Reuse the BNDSIM level series we built above (out["bnd_tr"] is built
    # by the BNDSIM block); fall back to VBMFX→AGG if BNDSIM missing.
    # NOTE: the BNDSIM block runs AFTER this block currently, so we defer.
    out["agg_tr"] = None  # populated below after BNDSIM is loaded

    # BND total bond market: Testfolio BND sim 1986+ → real BND (2007-04-03+)
    BNDSIM_PATH = str(SIM_DIR / "BND_daily_returns_1986-2026.csv")
    bnd_real = f("BND")
    if os.path.exists(BNDSIM_PATH):
        sim = pd.read_csv(BNDSIM_PATH)
        sim["date"] = pd.to_datetime(sim["date"])
        sim = sim.set_index("date").sort_index()
        sim_level = (1 + sim["return_pct"] / 100.0).cumprod() * 100.0
        if bnd_real is not None and len(bnd_real) > 0:
            out["bnd_tr"] = splice_series(sim_level, bnd_real, splice_date="2007-04-03")
            print(f"  ✓ BNDSIM: spliced with real BND at 2007-04-03 ({len(sim)} sim days)")
        else:
            out["bnd_tr"] = sim_level
        # Route AGG through the same BNDSIM (BND and AGG are both Bloomberg Agg
        # exposure — same asset class). Splice real AGG at AGG's inception.
        agg = f("AGG")
        if agg is not None and len(agg) > 0:
            out["agg_tr"] = splice_series(sim_level, agg, splice_date="2003-09-26")
            print(f"  ✓ AGG: routed through BNDSIM (Testfolio Bloomberg-Agg model) + real AGG at 2003-09-26")
        else:
            out["agg_tr"] = sim_level
    else:
        out["bnd_tr"] = bnd_real
        print(f"  ⚠ BNDSIM CSV not found at {BNDSIM_PATH}")
        # Fall back to VBMFX→AGG
        vbmfx = f("VBMFX"); agg = f("AGG")
        out["agg_tr"] = splice_series(vbmfx, agg) if vbmfx is not None else agg

    # High-yield: VWEHX (1978) → HYG (2007)
    vwehx = f("VWEHX"); hyg = f("HYG")
    out["hyg_tr"] = splice_series(vwehx, hyg)

    # Investment-grade corp: VWESX (1986) → LQD (2002)
    vwesx = f("VWESX"); lqd = f("LQD")
    out["lqd_tr"] = splice_series(vwesx, lqd)

    # KMLM: simulated daily returns from KFA Mount Lucas pre-launch (1988+)
    # spliced with real KMLM at its 2020-12-02 inception.
    KMLMSIM_PATH = str(SIM_DIR / "KMLMSIM_daily_returns.csv")
    if os.path.exists(KMLMSIM_PATH):
        sim = pd.read_csv(KMLMSIM_PATH)
        sim["Date"] = pd.to_datetime(sim["Date"])
        sim = sim.set_index("Date").sort_index()
        sim_returns = sim["Return (%)"] / 100.0  # convert from percent
        # Build a price level from the sim returns
        sim_level = (1 + sim_returns).cumprod() * 100.0
        kmlm_real = f("KMLM")
        if kmlm_real is not None:
            out["kmlm_tr"] = splice_series(sim_level, kmlm_real,
                                            splice_date="2020-12-02")
        else:
            out["kmlm_tr"] = sim_level
        print(f"  ✓ KMLMSIM: spliced with real KMLM at 2020-12-02 ({len(sim_returns)} sim days)")
    else:
        out["kmlm_tr"] = None
        print(f"  ⚠ KMLMSIM CSV not found at {KMLMSIM_PATH}")

    # DBMF: simulated MONTHLY returns from Testfolio pre-launch (2000-01+)
    # spliced with real DBMF at its 2019-05-08 inception. Monthly returns
    # are distributed evenly across trading days within each month via
    # (1 + r_m)^(1/n_days) - 1 to preserve the monthly compounded return
    # while producing a smooth daily series.
    DBMFSIM_PATH = str(SIM_DIR / "DBMFSIM_monthly_returns.csv")
    if os.path.exists(DBMFSIM_PATH):
        sim = pd.read_csv(DBMFSIM_PATH)
        sim["Date"] = pd.to_datetime(sim["Date"], format="%Y-%m")
        sim["Return"] = sim["Return"].str.rstrip("%").astype(float) / 100.0
        # Build daily price level by distributing each monthly return across
        # the trading days that fall in that month.
        spy_idx = out.get("spy_tr").index if out.get("spy_tr") is not None else None
        if spy_idx is None:
            print(f"  ⚠ DBMFSIM: cannot build daily series — no SPY trading-day index")
            out["dbmf_tr"] = None
        else:
            month_to_ret = {(d.year, d.month): r for d, r in zip(sim["Date"], sim["Return"])}
            daily_returns = pd.Series(0.0, index=spy_idx)
            # Group SPY trading days by year-month, distribute return across them
            for (yr, mo), grp in daily_returns.groupby(
                [daily_returns.index.year, daily_returns.index.month]
            ):
                r_m = month_to_ret.get((yr, mo))
                if r_m is None:
                    continue
                n = len(grp)
                if n == 0:
                    continue
                r_d = (1 + r_m) ** (1.0 / n) - 1
                daily_returns.loc[grp.index] = r_d
            sim_level = (1 + daily_returns).cumprod() * 100.0
            # Trim leading zeros (before any DBMFSIM data starts)
            first_month = sim["Date"].min()
            sim_level = sim_level.loc[sim_level.index >= first_month]
            try:
                dbmf_real = f("DBMF")
            except Exception:
                dbmf_real = None
            if dbmf_real is not None:
                out["dbmf_tr"] = splice_series(sim_level, dbmf_real,
                                                splice_date="2019-05-08")
                print(f"  ✓ DBMFSIM: spliced with real DBMF at 2019-05-08 ({len(sim)} sim months, daily-aligned)")
            else:
                out["dbmf_tr"] = sim_level
                print(f"  ✓ DBMFSIM: sim-only (no real DBMF cache; will be overlaid by Alpaca fetch in mega_backtest)")
    else:
        out["dbmf_tr"] = None
        print(f"  ⚠ DBMFSIM CSV not found at {DBMFSIM_PATH}")

    # SLV: simulated silver daily returns from Testfolio (1968+)
    # spliced with real SLV at its 2006-04-21 inception.
    SLVSIM_PATH = str(SIM_DIR / "SLVSIM_daily_returns_1968-2026.csv")
    slv_real = f("SLV")
    if os.path.exists(SLVSIM_PATH):
        sim = pd.read_csv(SLVSIM_PATH)
        sim["date"] = pd.to_datetime(sim["date"])
        sim = sim.set_index("date").sort_index()
        sim_level = (1 + sim["return_pct"] / 100.0).cumprod() * 100.0
        if slv_real is not None and len(slv_real) > 0:
            out["slv_tr"] = splice_series(sim_level, slv_real, splice_date="2006-04-21")
            print(f"  ✓ SLVSIM: spliced with real SLV at 2006-04-21 ({len(sim)} sim days)")
        else:
            out["slv_tr"] = sim_level
    else:
        out["slv_tr"] = slv_real
        print(f"  ⚠ SLVSIM CSV not found at {SLVSIM_PATH}")

    # URTH: simulated MSCI World daily returns from Testfolio (1970+)
    # spliced with real URTH at its 2012-01-12 inception.
    URTHSIM_PATH = str(SIM_DIR / "URTHSIM_daily_returns_1970-2026.csv")
    if os.path.exists(URTHSIM_PATH):
        sim = pd.read_csv(URTHSIM_PATH)
        sim["date"] = pd.to_datetime(sim["date"])
        sim = sim.set_index("date").sort_index()
        sim_returns = sim["return_pct"] / 100.0
        sim_level = (1 + sim_returns).cumprod() * 100.0
        urth_real = f("URTH")
        if urth_real is not None and len(urth_real) > 0:
            out["urth_tr"] = splice_series(sim_level, urth_real,
                                            splice_date="2012-01-12")
            print(f"  ✓ URTHSIM: spliced with real URTH at 2012-01-12 ({len(sim_returns)} sim days)")
        else:
            out["urth_tr"] = sim_level
            print(f"  ✓ URTHSIM: sim-only — {len(sim_returns)} days")
    else:
        out["urth_tr"] = None
        print(f"  ⚠ URTHSIM CSV not found at {URTHSIM_PATH}")

    # ACWI: authoritative MSCI ACWI gross total-return index levels (USD) pulled
    # from MSCI's own end-of-day API (index code 892400, GRTR). Daily gross TR is
    # only served from 2000-12-29 — true ACWI cannot go earlier on any free feed
    # (the 1987+ Curvo series is monthly EUR-net, frequency/currency-incompatible).
    # Spliced with the real iShares MSCI ACWI ETF (ACWI.US, 2008-03-28 inception).
    ACWISIM_PATH = str(SIM_DIR / "ACWI_msci_grtr_daily_2001-2026.csv")
    if os.path.exists(ACWISIM_PATH):
        sim = pd.read_csv(ACWISIM_PATH)
        sim["date"] = pd.to_datetime(sim["date"])
        sim = sim.set_index("date").sort_index()
        sim_returns = sim["return_pct"] / 100.0  # CSV is in percent
        sim_level = (1 + sim_returns).cumprod() * 100.0
        acwi_real = f("ACWI")
        if acwi_real is not None and len(acwi_real) > 0:
            out["acwi_tr"] = splice_series(sim_level, acwi_real,
                                            splice_date="2008-03-28")
            print(f"  ✓ ACWI: MSCI gross-TR index spliced with real ACWI ETF at 2008-03-28 ({len(sim_returns)} index days)")
        else:
            out["acwi_tr"] = sim_level
            print(f"  ✓ ACWI: MSCI gross-TR index only — {len(sim_returns)} days")
    else:
        out["acwi_tr"] = None
        print(f"  ⚠ ACWI CSV not found at {ACWISIM_PATH}")

    # NTSD: simulated daily returns from Testfolio's WisdomTree model (1970+)
    # spliced with real NTSD at its 2026-03-19 inception.
    NTSDSIM_PATH = str(SIM_DIR / "NTSDSIM_daily_returns_1970-2026.csv")
    if os.path.exists(NTSDSIM_PATH):
        sim = pd.read_csv(NTSDSIM_PATH)
        sim["date"] = pd.to_datetime(sim["date"])
        sim = sim.set_index("date").sort_index()
        sim_returns = sim["return_pct"] / 100.0  # CSV is in percent
        sim_level = (1 + sim_returns).cumprod() * 100.0
        ntsd_real = f("NTSD")
        if ntsd_real is not None and len(ntsd_real) > 0:
            out["ntsd_tr"] = splice_series(sim_level, ntsd_real,
                                            splice_date="2026-03-19")
            print(f"  ✓ NTSDSIM: spliced with real NTSD at 2026-03-19 ({len(sim_returns)} sim days)")
        else:
            out["ntsd_tr"] = sim_level
            print(f"  ✓ NTSDSIM: sim-only (no real NTSD yet) — {len(sim_returns)} days")
    else:
        out["ntsd_tr"] = None
        print(f"  ⚠ NTSDSIM CSV not found at {NTSDSIM_PATH}")

    # TIPS: VIPSX (Vanguard TIPS Fund, 2000+) → TIP (2003+)
    try:
        vipsx = f("VIPSX"); tip = f("TIP")
        out["tip_tr"] = splice_series(vipsx, tip) if vipsx is not None else tip
    except Exception:
        out["tip_tr"] = None

    # Floor every series at EXTENDED_START. SPYSIM extends back to 1885 and
    # GLDSIM/SLVSIM/NTSDSIM to 1968-1970, but most other inputs don't exist
    # before 1986-1990. Without flooring, the SPY-driven common_idx would span
    # 1885-2026 with mostly-NaN columns pre-1987, producing nonsense aggregates.
    floor_dt = pd.Timestamp(EXTENDED_START)
    for k, v in list(out.items()):
        if k == "bil_daily_return":
            continue
        if v is None:
            continue
        if isinstance(v, pd.Series):
            out[k] = v.loc[floor_dt:]

    # Re-align everything to SPY's trading calendar to prevent holiday misalignments
    base_idx = out["spy_tr"].index
    for k, v in list(out.items()):
        if k == "bil_daily_return":
            continue
        if v is None:
            continue
        out[k] = v.reindex(base_idx).ffill()

    print(f"\n  ✓ Extended data fetched. Common range:")
    common_start = max(s.index[0] for s in out.values() if isinstance(s, pd.Series) and s is not None)
    common_end = min(s.index[-1] for s in out.values() if isinstance(s, pd.Series) and s is not None)
    print(f"    Common start: {common_start.date()}")
    print(f"    Common end:   {common_end.date()}")
    return out


def to_returns_frame(data: dict) -> pd.DataFrame:
    """Convert the data dict to a daily-returns DataFrame on the SPY index."""
    base_idx = data["spy_tr"].index
    rets = {}
    for k, v in data.items():
        if k == "bil_daily_return":
            rets["BIL"] = v.reindex(base_idx).fillna(0)
            continue
        if v is None:
            continue
        # Map names to ETF-style tickers used in strategies
        key_map = {
            "spy_tr": "SPY", "efa_tr": "EFA", "tlt_tr": "TLT", "ief_tr": "IEF",
            "gld_tr": "GLD", "dbc_tr": "DBC", "qqq_tr": "QQQ", "agg_tr": "AGG",
            "hyg_tr": "HYG", "lqd_tr": "LQD", "tip_tr": "TIP",
        }
        ticker = key_map.get(k, k)
        rets[ticker] = v.reindex(base_idx).ffill().pct_change().fillna(0)
    return pd.DataFrame(rets)


def to_prices_frame(data: dict) -> pd.DataFrame:
    """Convert to prices DataFrame (for signal computation)."""
    base_idx = data["spy_tr"].index
    px = {}
    for k, v in data.items():
        if k == "bil_daily_return":
            continue
        if v is None:
            continue
        key_map = {
            "spy_tr": "SPY", "efa_tr": "EFA", "tlt_tr": "TLT", "ief_tr": "IEF",
            "gld_tr": "GLD", "dbc_tr": "DBC", "qqq_tr": "QQQ", "agg_tr": "AGG",
            "hyg_tr": "HYG", "lqd_tr": "LQD", "tip_tr": "TIP",
        }
        ticker = key_map.get(k, k)
        px[ticker] = v.reindex(base_idx).ffill()
    return pd.DataFrame(px)


if __name__ == "__main__":
    data = fetch_extended_data()
    rets = to_returns_frame(data)
    print(f"\nReturns frame: {rets.shape}, columns: {list(rets.columns)}")
    print(f"Date range: {rets.index[0].date()} → {rets.index[-1].date()}")
