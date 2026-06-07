"""
JST Macrohistory Database (R6) loader — long-run asset-class returns for context.

Source: Jordà-Schularick-Taylor Macrohistory Database, release 6.
        https://www.macrohistory.net/database/
Papers: "The Rate of Return on Everything, 1870-2015" (QJE 2019) and
        "No Price Like Home" (QJE 2017). Documentation PDFs in data/jst/docs/.

WHAT THIS IS — and what it is NOT
---------------------------------
ANNUAL nominal total returns for 18 advanced economies, 1870-2020, across
equities, housing, bonds, and bills, plus macro/credit aggregates and a banking
-crisis chronology. This is an academic macro-finance panel, NOT a tradeable
price feed.

It is therefore *frequency-incompatible* with the daily backtest engine in
mega_backtest.py (200-SMA gates, 6m momentum, vol-targeting all need daily
bars). Do NOT wire it into extended_data.py. Use it for the things annual data
is actually good for:
  - century-scale equity risk premium / real-return base rates
  - long-run drawdown & crisis frequency context (crisisJST)
  - sanity-checking the deep proxies (e.g. spy_tr/urth_tr) against 150y of data
  - regime base rates (how often equities fall in/around banking crises)

Key return columns (all NOMINAL, decimal — 0.19 == +19%):
  eq_tr       equity total return        bond_tr   long govt bond total return
  bill_rate   short bill / cash rate      housing_tr housing total return
  eq_capgain / eq_div_rtn  equity price vs dividend components
  risky_tr / safe_tr / capital_tr  paper's per-country blended aggregates
Deflate by cpi (an index level) to get real returns — essential, because
nominal series include hyperinflation episodes (Weimar etc.).
"""
from pathlib import Path
import pandas as pd

JST_DIR = Path(__file__).resolve().parent / "data" / "jst"
JST_XLSX = JST_DIR / "JSTdatasetR6.xlsx"

RETURN_COLS = ["eq_tr", "bond_tr", "bill_rate", "housing_tr",
               "risky_tr", "safe_tr", "capital_tr"]


def load_jst() -> pd.DataFrame:
    """Return the full tidy panel (year x country, 2718 rows, 59 cols)."""
    df = pd.read_excel(JST_XLSX)
    return df.sort_values(["country", "year"]).reset_index(drop=True)


def add_real_returns(df: pd.DataFrame) -> pd.DataFrame:
    """Add real (CPI-deflated) versions of the return columns, per country.

    real = (1 + nominal) / (1 + inflation) - 1, inflation from the cpi index.
    """
    df = df.copy()
    df["infl"] = df.groupby("country")["cpi"].pct_change(fill_method=None)
    for c in RETURN_COLS:
        if c in df.columns:
            df[f"{c}_real"] = (1 + df[c]) / (1 + df["infl"]) - 1
    return df


def global_aggregate(df: pd.DataFrame, col: str = "eq_tr",
                     real: bool = True, weight: str = "rgdpmad") -> pd.DataFrame:
    """GDP-weighted global aggregate of a return column, chained to an index.

    Weights each country by real GDP (rgdpmad, Maddison 1990 int'l $ — the only
    cross-country-comparable size measure in the panel), renormalised each year
    over countries that actually report the return that year. Mirrors the
    GDP-weighting the RORE paper uses for its "global" series.

    Returns a frame indexed by year with columns [ret, index] (index base 100 at
    the first year with full data).
    """
    work = add_real_returns(df) if real else df.copy()
    src = f"{col}_real" if real else col
    sub = work[["year", "country", src, weight]].dropna(subset=[src, weight])
    # year-by-year GDP weights, renormalised over reporting countries
    sub = sub.copy()
    sub["w"] = sub.groupby("year")[weight].transform(lambda s: s / s.sum())
    ann = (sub[src] * sub["w"]).groupby(sub["year"]).sum().rename("ret")
    out = ann.to_frame()
    out["index"] = (1 + out["ret"]).cumprod() * 100.0
    return out


def crisis_years(df: pd.DataFrame, country: str | None = None) -> dict | list:
    """Banking-crisis years (crisisJST==1). Per-country dict, or list if named."""
    cr = df[df["crisisJST"] == 1][["country", "year"]]
    if country:
        return sorted(cr[cr.country == country].year.astype(int).tolist())
    return {c: sorted(g.year.astype(int).tolist())
            for c, g in cr.groupby("country")}


def _cagr(index: pd.Series) -> float:
    yrs = index.index[-1] - index.index[0]
    return (index.iloc[-1] / index.iloc[0]) ** (1 / yrs) - 1


if __name__ == "__main__":
    df = load_jst()
    print(f"JST R6: {df.shape[0]} rows, {df.country.nunique()} countries, "
          f"{int(df.year.min())}-{int(df.year.max())}")

    print("\n=== GDP-weighted GLOBAL real total returns (annual) ===")
    for col, lbl in [("eq_tr", "Equity"), ("bond_tr", "Bonds"),
                     ("bill_rate", "Bills"), ("housing_tr", "Housing")]:
        g = global_aggregate(df, col, real=True)
        print(f"  {lbl:8} {int(g.index[0])}-{int(g.index[-1])}: "
              f"real CAGR {_cagr(g['index'])*100:5.2f}%  "
              f"vol {g['ret'].std()*100:5.2f}%  "
              f"worst yr {g['ret'].min()*100:6.1f}%")

    print("\n=== Banking crises since 1970 (count by country) ===")
    cy = {c: [y for y in ys if y >= 1970] for c, ys in crisis_years(df).items()}
    for c in sorted(cy, key=lambda k: -len(cy[k]))[:6]:
        print(f"  {c:12} {cy[c]}")
