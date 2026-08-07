"""HAR-RV Volatilitätsprognose — der Test, der aus der TimesFM-Bewertung folgt.

    python3 -m quant.research.har_vol_study --run

WARUM DIESER TEST: Die Foundation-Model-Literatur zeigt, dass Log-HAR (Corsi
2009) neun Transformer-TSFMs auf realisierter Volatilität schlägt (TimesFM 2.5
liegt 8.6–33% dahinter, MCS-Inklusion 86–90% vs ≤30%). Volatilität ist die
eine Größe in unseren Daten, die echte Autokorrelation trägt. Unser Portfolio
gewichtet invers zur Vol (`vol_63d`) — eine bessere Vol-Schätzung verbessert
also direkt die Risikoparität, ohne neuen Turnover.

VORREGISTRIERT:
  H1  Log-HAR (1d/5d/22d Parkinson-RV, expanding-window OLS) schlägt die
      Trailing-21d-Vol beim Ranking der FORWARD-5d-Vol (Rank-IC, t > 3).
  H2  HAR-Vol statt Trailing-Vol in der Inverse-Vol-Gewichtung hebt den
      Netto-Sharpe um ≥ +0.03.
  Abbruch: fällt H1 oder H2, wird das Thema geschlossen und dokumentiert.

Kein Look-ahead: OLS-Koeffizienten werden expanding-window je Jahr geschätzt
(nur Daten < Jahresbeginn), Parkinson-RV nutzt nur High/Low des jeweiligen Tags.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.data.bq import query

SQL = """
WITH px AS (
  SELECT date, symbol, high, low, close, close * volume AS dvol
  FROM `trading-436516.quant.eod_bars`
  WHERE close > 0 AND high > 0 AND low > 0 AND adjusted_close > 0
    AND date >= '2003-01-01'
),
-- Stufe 1: alles, was LAG braucht (BigQuery erlaubt keine verschachtelten
-- analytischen Funktionen)
r1 AS (
  SELECT date, symbol, close, dvol,
    POW(LN(SAFE_DIVIDE(high, low)), 2) / (4 * LN(2)) AS rv1,
    SAFE_DIVIDE(close, LAG(close) OVER w) - 1 AS ret1
  FROM px
  WINDOW w AS (PARTITION BY symbol ORDER BY date)
),
-- Stufe 2: rollierende Aggregate über Stufe-1-Spalten
har AS (
  SELECT date, symbol, rv1,
    AVG(rv1) OVER (PARTITION BY symbol ORDER BY date
                   ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)  AS rv5,
    AVG(rv1) OVER (PARTITION BY symbol ORDER BY date
                   ROWS BETWEEN 21 PRECEDING AND CURRENT ROW) AS rv22,
    STDDEV(ret1) OVER (PARTITION BY symbol ORDER BY date
                       ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) AS trail_vol21,
    SQRT(AVG(rv1) OVER (PARTITION BY symbol ORDER BY date
                        ROWS BETWEEN 1 FOLLOWING AND 5 FOLLOWING)) AS fwd_vol5,
    AVG(dvol) OVER (PARTITION BY symbol ORDER BY date
                    ROWS BETWEEN 62 PRECEDING AND 1 PRECEDING) AS adv63
  FROM r1
),
u AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY date ORDER BY adv63 DESC) AS liq_rank
  FROM har
  WHERE rv1 > 0 AND rv5 > 0 AND rv22 > 0 AND trail_vol21 > 0 AND fwd_vol5 > 0
)
SELECT date, symbol, rv1, rv5, rv22, trail_vol21, fwd_vol5, adv63
FROM u WHERE liq_rank <= 1500
"""


def run():
    print("Lade Parkinson-RV-Panel (Top-1500) ...")
    df = query(SQL)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["date", "symbol"]).reset_index(drop=True)
    print(f"{len(df):,} Zeilen, {df['date'].dt.year.min()}–"
          f"{df['date'].dt.year.max()}")

    # Log-HAR: log(fwd_vol5) ~ log(rv1) + log(rv5) + log(rv22)
    for c in ["rv1", "rv5", "rv22", "fwd_vol5", "trail_vol21"]:
        df[f"l_{c}"] = np.log(df[c].clip(lower=1e-10))
    feats = ["l_rv1", "l_rv5", "l_rv22"]

    print("\n═══ H1: Log-HAR vs Trailing-Vol beim Forward-Vol-Ranking ═══")
    preds = []
    for year in range(2005, 2027):
        tr = df[df["date"] < f"{year}-01-01"]
        te = df[(df["date"] >= f"{year}-01-01") & (df["date"] <= f"{year}-12-31")]
        if len(tr) < 50_000 or te.empty:
            continue
        X = np.column_stack([np.ones(len(tr))] + [tr[f].values for f in feats])
        beta, *_ = np.linalg.lstsq(X, tr["l_fwd_vol5"].values, rcond=None)
        Xt = np.column_stack([np.ones(len(te))] + [te[f].values for f in feats])
        o = te[["date", "symbol", "fwd_vol5", "trail_vol21", "adv63"]].copy()
        o["har_vol"] = np.exp(Xt @ beta)
        preds.append(o)
        if year in (2005, 2015, 2026):
            print(f"  {year}: β = {np.round(beta, 3)}")
    p = pd.concat(preds, ignore_index=True)

    def daily_ic(col):
        return p.groupby("date").apply(
            lambda g: g[col].corr(g["fwd_vol5"], method="spearman")).dropna()

    ic_har, ic_trail = daily_ic("har_vol"), daily_ic("trail_vol21")
    d = (ic_har - ic_trail.reindex(ic_har.index)).dropna()
    t = d.mean() / d.std() * np.sqrt(len(d))
    print(f"\nRank-IC (Forward-5d-Vol), OOS 2005–2026, {len(ic_har):,} Tage:")
    print(f"  Log-HAR        {ic_har.mean():+.4f}")
    print(f"  Trailing-21d   {ic_trail.mean():+.4f}")
    print(f"  Differenz      {d.mean():+.4f}   t = {t:+.1f}   "
          f"→ H1 {'BESTÄTIGT (t>3)' if t > 3 else 'GEFALLEN'}")

    # H2: Portfolio-Wirkung — HAR-Vol statt vol_63d in der Gewichtung
    print("\n═══ H2: Netto-Sharpe-Wirkung im XSR-Portfolio ═══")
    import os
    from quant.config import STAGING_DIR
    from quant.backtest.portfolio_sim import simulate_tranches
    path = os.path.join(STAGING_DIR, "preds_wf_v2_full.parquet")
    if not os.path.exists(path):
        print("preds_wf_v2_full.parquet fehlt — H2 übersprungen")
        return
    xsr = pd.read_parquet(path)
    xsr["date"] = pd.to_datetime(xsr["date"])
    merged = xsr.merge(p[["date", "symbol", "har_vol"]], on=["date", "symbol"],
                       how="left")
    # HAR-Vol ist eine 5d-Vol → auf annualisierte Skala wie vol_63d bringen
    merged["har_vol_ann"] = merged["har_vol"] * np.sqrt(252)
    cov = merged["har_vol_ann"].notna().mean()
    print(f"HAR-Vol-Abdeckung der Predictions: {cov:.0%}")

    def sh(res):
        r = res["net_ret"]
        return (r.mean() / r.std() * np.sqrt(252),
                (1 + r).cumprod().iloc[-1] ** (252 / len(r)) - 1)

    base = merged.copy()
    s0, c0 = sh(simulate_tranches(base, k=5))
    alt = merged.copy()
    alt["vol_63d"] = alt["har_vol_ann"].fillna(alt["vol_63d"])
    s1, c1 = sh(simulate_tranches(alt, k=5))
    print(f"  Basis (vol_63d)    Sharpe {s0:.3f}  CAGR {c0:+.1%}")
    print(f"  HAR-Vol-Gewichtung Sharpe {s1:.3f}  CAGR {c1:+.1%}")
    print(f"  Δ Sharpe {s1 - s0:+.3f}  → H2 "
          f"{'BESTÄTIGT (≥+0.03)' if s1 - s0 >= 0.03 else 'GEFALLEN'}")

    # Trial-Registry (ehrliche Versuchszahl)
    from quant.research.trials_registry import log_trial
    try:
        log_trial("XSR", simulate_tranches(alt, k=5)["net_ret"],
                  variant="HAR-Vol-Gewichtung",
                  verdict="KANDIDAT" if s1 - s0 >= 0.03 else "VERWORFEN",
                  notes="Log-HAR statt vol_63d in Inverse-Vol-Gewichtung")
    except Exception as e:  # noqa: BLE001
        print(f"  Registry-Log fehlgeschlagen: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    if not a.run:
        ap.print_help()
        sys.exit(1)
    run()
