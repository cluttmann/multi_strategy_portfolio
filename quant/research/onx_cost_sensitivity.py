"""ONX-Kostensensitivität — hält der beste Sleeve die GEMESSENEN Kosten aus?

    python3 -m quant.research.onx_cost_sensitivity --run

ANLASS: Der Live-Kostenmonitor hat an den ersten echten Fills 10.0bp Slippage
gegen den offiziellen Schlussprint gemessen (notional-gewichtet), nicht die
im ONX-Backtest angenommenen 4bp ROUND-TRIP. Und die Slippage skalierte invers
mit Liquidität: YINN 3.6bp, DFEN 9.2bp, CURE 16.8bp.

ZWEI FRAGEN:
  F1  Bei welchen Round-Trip-Kosten stirbt ONX? (Sensitivitätsleiter)
  F2  Rettet ein LIQUIDITÄTSFILTER den Sleeve? Wenn Slippage invers zur
      Liquidität skaliert, sollte ein Top-N-nach-ADV-Universum billiger sein
      — der Executor nimmt ohnehin nur die 8 liquidesten Namen (TOP_N=8),
      der Backtest rechnete aber über alle 28. Das ist eine Inkonsistenz
      zwischen Backtest und Live-Umsetzung, die hier zum ersten Mal geprüft wird.

Vorregistriert: keine Parameter werden auf das Ergebnis gefittet. Getestet
werden genau die Kostenstufen {4, 10, 20, 30}bp Round-Trip und genau die
Universumsgrößen {5, 8, 15, 28} (8 ist der Live-Wert des Executors).
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.data.bq import query
from quant.research.letf_rebalance_flow import UNIV_3X

COST_LADDER = [4, 10, 20, 30]        # bps Round-Trip
UNIVERSE_SIZES = [5, 8, 15, 28]      # 8 = Live-Wert (onx_live.TOP_N)


def load() -> pd.DataFrame:
    q = ", ".join(repr(s) for s in UNIV_3X)
    df = query(f"""
      WITH px AS (
        SELECT date, symbol,
          open * SAFE_DIVIDE(adjusted_close, close) AS ao,
          adjusted_close AS ac,
          close * volume AS dvol
        FROM `trading-436516.quant.eod_bars`
        WHERE symbol IN ({q}) AND close > 0 AND adjusted_close > 0
      ),
      r AS (
        SELECT date, symbol, ac, dvol,
          SAFE_DIVIDE(LEAD(ao) OVER w, ac) - 1 AS r_on
        FROM px WINDOW w AS (PARTITION BY symbol ORDER BY date)
      )
      SELECT date, symbol, r_on, dvol,
        AVG(ac) OVER (PARTITION BY symbol ORDER BY date
                      ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS sma50,
        ac,
        AVG(dvol) OVER (PARTITION BY symbol ORDER BY date
                        ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS adv21
      FROM r""")
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["r_on", "sma50", "adv21"])
    return df[df["r_on"].abs() < 0.5]


def sleeve_returns(df: pd.DataFrame, top_n: int, cost_bp: float) -> pd.Series:
    """V2-Regel (Trendgate) + Liquiditätsfilter Top-N nach ADV, EW."""
    g = df[df["ac"] > df["sma50"]].copy()          # Trendgate wie V2
    g["liq_rank"] = g.groupby("date")["adv21"].rank(ascending=False)
    g = g[g["liq_rank"] <= top_n]
    return (g.groupby("date")["r_on"].mean() - cost_bp / 1e4).sort_index()


def stats(r: pd.Series) -> tuple[float, float, float]:
    r = r.dropna()
    if len(r) < 100:
        return (np.nan,) * 3
    yrs = (r.index.max() - r.index.min()).days / 365.25
    eq = (1 + r).cumprod()
    return (r.mean() / r.std() * np.sqrt(len(r) / yrs),
            eq.iloc[-1] ** (1 / yrs) - 1,
            (eq / eq.cummax() - 1).min())


def run():
    df = load()
    print(f"Panel: {len(df):,} Symbol-Tage, {df['date'].min():%Y-%m} → "
          f"{df['date'].max():%Y-%m}\n")

    # F1+F2 gemeinsam: Matrix Universumsgröße × Kosten
    for label, sub in [("VOLL 2008–2026", df),
                       ("AKTUELLES REGIME 2022–2026", df[df["date"] >= "2022-01-01"])]:
        print(f"═══ {label}: Sharpe (CAGR) je Universum × Round-Trip-Kosten ═══")
        print(f"{'Top-N':>6s} " + "".join(f"{c:>16s}" for c in
                                          [f"{c}bp" for c in COST_LADDER]))
        for n in UNIVERSE_SIZES:
            row = f"{n:>6d} "
            for c in COST_LADDER:
                s, cagr, _ = stats(sleeve_returns(sub, n, c))
                mark = "*" if n == 8 else " "
                row += f"{s:>8.2f}({cagr:+5.0%}){mark}" if not np.isnan(s) \
                    else f"{'—':>16s}"
            print(row)
        print()

    # Welche Slippage haben die Top-8 tatsächlich? Proxy über ADV-Verteilung
    print("═══ Liquiditätsprofil: gehandelte Namen im Live-Universum (Top-8) ═══")
    last = df[df["date"] == df["date"].max()].copy()
    last = last[last["ac"] > last["sma50"]]
    last["liq_rank"] = last["adv21"].rank(ascending=False)
    top = last.nsmallest(8, "liq_rank")[["symbol", "adv21"]]
    for _, r in top.iterrows():
        print(f"  {r.symbol:6s} ADV21 ${r.adv21/1e6:8.1f}M")
    print(f"  Median-ADV der Top-8: ${top['adv21'].median()/1e6:.1f}M")

    # Break-even-Kosten je Universumsgröße (aktuelles Regime = das relevante)
    print("\n═══ Break-even-Kosten (Round-Trip), Regime 2022–2026 ═══")
    recent = df[df["date"] >= "2022-01-01"]
    for n in UNIVERSE_SIZES:
        gross = sleeve_returns(recent, n, 0.0)
        be = gross.mean() * 1e4
        print(f"  Top-{n:2d}: Brutto {be:5.1f}bp/Tag → ONX ist ab "
              f"{be:.1f}bp Round-Trip wertlos "
              f"({'ÜBER' if be > 20 else 'UNTER'} den gemessenen ~20bp)")

    # Trial-Registry für die getesteten Varianten
    from quant.research.trials_registry import log_trial
    for n in (8, 28):
        for c in (10, 20):
            try:
                log_trial("ONX", sleeve_returns(df, n, c),
                          variant=f"Top-{n} @ {c}bp RT",
                          verdict="Kostentest",
                          notes="Sensitivität nach Live-Slippage-Messung")
            except Exception as e:  # noqa: BLE001
                print(f"  log Top-{n}@{c}bp: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    if not a.run:
        ap.print_help()
        sys.exit(1)
    run()
