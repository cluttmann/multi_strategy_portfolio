"""Familie 14: LETF-Zwangsrebalancierung als mechanische Marktirregularität.

    python3 -m quant.research.letf_rebalance_flow --run

MECHANISMUS (kein Data-Mining — a priori vorhergesagt):
Ein gehebelter ETF mit Faktor L muss sein Exposure JEDEN Tag zum Schluss
zurücksetzen. Bei Tagesrendite r des Basiswerts lautet der Zwangsfluss
    Δ = (L² − L) · AUM · r
also für L=3: ~6·AUM·r, gleichgerichtet mit der Bewegung (Kauf an Auf-,
Verkauf an Abwärtstagen). Dieser unelastische Fluss drückt den Schlusskurs in
Bewegungsrichtung; der Druck verschwindet nach dem Print → Rücklauf über Nacht.

VORREGISTRIERTE HYPOTHESEN (Vorzeichen vorher festgelegt):
  H1  ONX-Übernachtrendite (close→open) ist NEGATIV mit der Tagesrendite
      korreliert: an starken Aufwärtstagen kaufen wir einen aufgeblähten
      Schluss.
  H2  Der Effekt skaliert mit |r| (größerer Zwangsfluss).
  H3  Er ist bei 3×-Fonds stärker als bei 2× (Faktor 6 vs. 2 in L²−L).

Wenn H1 hält, ist das ein mechanistisches Conditioning für unseren besten
Sleeve — kein neuer Sleeve, sondern eine Schärfung von ONX.
Kosten: 4bp/Tag Round-Trip wie im ONX-Backtest.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.data.bq import query

COST = 4 / 1e4

# 3x-Bull-Universum (wie ONX) und 2x-Kontrollgruppe für H3
UNIV_3X = ["SOXL", "TNA", "TECL", "FAS", "LABU", "UDOW", "DFEN", "DPST",
           "NAIL", "URTY", "MIDU", "RETL", "CURE", "DRN", "WEBL", "HIBL",
           "UTSL", "EDC", "YINN", "DUSL", "KORU", "PILL", "TPOR", "WANT",
           "UMDD", "EURL", "MEXX", "TYD"]
UNIV_2X = ["SSO", "QLD", "UWM", "ROM", "UYG", "DDM", "MVV", "SAA", "UGE",
           "UPW", "URE", "UCC", "RXL", "UXI", "UST"]


def load(symbols: list[str]) -> pd.DataFrame:
    q = ", ".join(repr(s) for s in symbols)
    df = query(f"""
      WITH px AS (
        SELECT date, symbol,
          open  * SAFE_DIVIDE(adjusted_close, close) AS ao,
          adjusted_close AS ac,
          close * volume AS dvol
        FROM `trading-436516.quant.eod_bars`
        WHERE symbol IN ({q}) AND close > 0 AND adjusted_close > 0
      )
      SELECT date, symbol, dvol,
        SAFE_DIVIDE(ac, LAG(ac) OVER w) - 1               AS r_day,
        SAFE_DIVIDE(ac, NULLIF(ao,0)) - 1                 AS r_intraday,
        SAFE_DIVIDE(LEAD(ao) OVER w, ac) - 1              AS r_overnight,
        AVG(dvol) OVER (PARTITION BY symbol ORDER BY date
                        ROWS BETWEEN 21 PRECEDING AND 1 PRECEDING) AS adv21
      FROM px
      WINDOW w AS (PARTITION BY symbol ORDER BY date)""")
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["r_day", "r_overnight"])
    # Artefaktschutz wie überall
    return df[(df["r_day"].abs() < 0.5) & (df["r_overnight"].abs() < 0.5)]


def report(df: pd.DataFrame, label: str):
    print(f"\n═══ {label}: {len(df):,} Symbol-Tage, "
          f"{df['date'].min():%Y-%m} → {df['date'].max():%Y-%m} ═══")

    # H1: Vorzeichenbeziehung Tagesrendite → Übernachtrendite
    ic = df["r_day"].corr(df["r_overnight"], method="spearman")
    print(f"H1  rank-corr(r_day, r_overnight) = {ic:+.4f}  "
          f"(erwartet: negativ)")

    q = pd.qcut(df["r_day"], 5, labels=["Q1 stark −", "Q2", "Q3", "Q4",
                                        "Q5 stark +"])
    g = df.groupby(q, observed=True).agg(
        n=("r_overnight", "size"),
        r_day_bp=("r_day", lambda s: s.mean() * 1e4),
        overnight_bp=("r_overnight", lambda s: s.mean() * 1e4))
    g["net_bp"] = g["overnight_bp"] - COST * 1e4
    print(g.round(1).to_string())

    # H2: Skalierung mit |r| — Übernacht-Rendite nur an Extremtagen
    print("\nH2  Übernachtrendite nach |Tagesrendite| (netto, bp):")
    for lo, hi, name in [(0, 0.01, "|r| < 1%"), (0.01, 0.03, "1–3%"),
                         (0.03, 0.06, "3–6%"), (0.06, 1.0, "> 6%")]:
        sub = df[(df["r_day"].abs() >= lo) & (df["r_day"].abs() < hi)]
        if len(sub) < 200:
            continue
        up = sub[sub["r_day"] > 0]["r_overnight"].mean() * 1e4 - COST * 1e4
        dn = sub[sub["r_day"] < 0]["r_overnight"].mean() * 1e4 - COST * 1e4
        print(f"  {name:9s} n={len(sub):6,}  nach Aufwärtstag {up:+7.1f}  "
              f"nach Abwärtstag {dn:+7.1f}  Spread {dn - up:+7.1f}")

    return ic


def strategy_test(df: pd.DataFrame):
    """ONX-Schärfung: nur nach Abwärtstagen kaufen (mechanistisch motiviert)."""
    print("\n═══ ONX-Schärfung: Zwangsfluss-Conditioning ═══")
    daily = {}
    variants = {
        "V0 unconditional (Basis)": df,
        "V1 nur nach Abwärtstag (r_day<0)": df[df["r_day"] < 0],
        "V2 nur nach starkem Abwärtstag (<-2%)": df[df["r_day"] < -0.02],
        "V3 Aufwärtstage ausgeschlossen (>+2%)": df[df["r_day"] < 0.02],
    }
    for name, sub in variants.items():
        if sub.empty:
            continue
        # equal-weight über alle am Tag qualifizierten Namen
        d = sub.groupby("date")["r_overnight"].mean() - COST
        yrs = (d.index.max() - d.index.min()).days / 365.25
        # Kapital nur an Tagen mit Signal investiert
        eq = (1 + d).cumprod()
        cagr = eq.iloc[-1] ** (1 / yrs) - 1
        sh = d.mean() / d.std() * np.sqrt(len(d) / yrs) if d.std() > 0 else 0
        dd = (eq / eq.cummax() - 1).min()
        print(f"{name:40s} Tage={len(d):5,} CAGR={cagr:+7.1%} "
              f"Sharpe={sh:5.2f} MaxDD={dd:6.1%}")
        daily[name] = d
    # Regime-Split der besten Variante
    print("\nRegime-Split (2022+ = das Regime, das zählt):")
    for name, d in daily.items():
        d2 = d.loc["2022":]
        if len(d2) < 100:
            continue
        eq = (1 + d2).cumprod()
        yrs = (d2.index.max() - d2.index.min()).days / 365.25
        print(f"  {name:40s} CAGR={eq.iloc[-1] ** (1/yrs) - 1:+7.1%} "
              f"Sharpe={d2.mean()/d2.std()*np.sqrt(len(d2)/yrs):5.2f}")


def run():
    d3 = load(UNIV_3X)
    ic3 = report(d3, "3×-Bull-ETFs (L²−L = 6)")
    d2 = load(UNIV_2X)
    ic2 = report(d2, "2×-Bull-ETFs (L²−L = 2, Kontrollgruppe)")
    print(f"\nH3  |corr| 3× = {abs(ic3):.4f} vs 2× = {abs(ic2):.4f} → "
          f"{'BESTÄTIGT (3× stärker)' if abs(ic3) > abs(ic2) else 'NICHT bestätigt'}")
    strategy_test(d3)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    a = p.parse_args()
    if not a.run:
        p.print_help()
        sys.exit(1)
    run()
