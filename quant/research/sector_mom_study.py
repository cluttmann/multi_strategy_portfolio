"""Familie 22 (SECTORMOM): US-Branchen-Momentum (Moskowitz/Grinblatt JF 1999).

    python3 -m quant.research.sector_mom_study --run

MECHANISMUS: Branchenweite Schocks (Regulierung, Rohstoffpreise, Zinsen)
diffundieren langsamer in die Erwartungen von Analysten/Anlegern als
einzeltitelspezifische Nachrichten — Moskowitz/Grinblatt zeigen, dass
Branchen-Momentum STÄRKER und robuster ist als Einzeltitel-Momentum und
einen Großteil von dessen Erklärungskraft trägt. Wir handeln die
liquidesten, direktesten Träger dieses Effekts (SPDR-Sektor-ETFs) statt
Einzeltitel — kein Informationsvorsprung, eine Kompensation für langsame
Informationsdiffusion (Regel R1: Haltedauer Wochen bis Monate).

ANDERS ALS DTRD: DTRDs Cross-Asset-TSMOM-Universum (Anleihen, Rohstoffe,
Währungen, Intl-Aktien, Immobilien) enthält KEINE US-Sektor-ETFs — das ist
eine andere Anlageklasse mit anderem Mechanismus (Trendfolge über
Anlageklassen vs. relative Branchenrotation innerhalb US-Aktien).

QUANTITATIVE VORHERSAGE (Regel R2):
  (a) Rendite skaliert MONOTON mit dem Formations-Momentum-Rang der
      Branche;
  (b) der Effekt ist über mehrere Haltedauern robust (kein Ein-Punkt-Fund);
  (c) ρ zu XSR ist NIEDRIG (G7 misst automatisch) — Branchen-Rotation ist
      eine andere Informationsebene als XSRs Einzeltitel-Cross-Section,
      auch wenn beide "Momentum" im Namen tragen.

UNIVERSUM: 9 klassische SPDR-Sektoren mit durchgehender Historie seit 2000
(XLB/XLE/XLF/XLI/XLK/XLP/XLU/XLV/XLY). XLC (Kommunikation, erst 2018) und
XLRE (Immobilien, erst 2015) bewusst NICHT im Backtest-Universum — zu kurze
Historie für Regel R7 (5+ Jahre Holdout); Ergänzung fürs Live-Signal wäre
eine spätere, offengelegte Erweiterung, kein stiller Sample-Unterschied.

VORREGISTRIERT (Regel R6): genau 3 Varianten — Haltedauer (21/42/63 Tage).
Formation ist fix (12M-Momentum ex-1M, wie XSRs eigenes mom_12m_ex1m-Feature
— dieselbe, literaturübliche Definition, keine Sweep-Achse). Training bis
2019, Holdout 2020-2026 (Regel R7).
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.data.bq import query

SECTORS = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]
HORIZONS = [21, 42, 63]        # genau 3 Varianten (Regel R6)
N_LEG = 3                       # long/short je 3 von 9 Sektoren
FORM_LOOKBACK = 252              # 12M
FORM_SKIP = 21                   # ex-1M (wie mom_12m_ex1m)
TRAIN_END = "2019-12-31"
COST_BPS = 10.0


def load(start="2000-01-01") -> pd.DataFrame:
    q = ", ".join(repr(s) for s in SECTORS)
    df = query(f"""
      SELECT date, symbol, adjusted_close AS ac
      FROM `trading-436516.quant.eod_bars`
      WHERE symbol IN ({q}) AND adjusted_close > 0 AND date >= '{start}'
      ORDER BY date""")
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot(index="date", columns="symbol", values="ac").sort_index()


def build_momentum(px: pd.DataFrame) -> pd.DataFrame:
    """12M-Momentum ex-1M je Sektor, täglich (wie XSRs mom_12m_ex1m)."""
    ret_form = px.shift(FORM_SKIP) / px.shift(FORM_LOOKBACK) - 1
    return ret_form


def _portfolio(px: pd.DataFrame, mom: pd.DataFrame, h: int,
              n_leg: int = N_LEG, cost_bps: float = COST_BPS) -> pd.Series:
    """Monatliche Neusortierung, Positionen h Tage gehalten (überlappende
    Tranchen wie simulate_tranches, aber für ein 9-Namen-Universum von Hand,
    da simulate_tranches auf ein liquides 1500-Namen-Feature-Panel zielt)."""
    ret = px.pct_change()
    idx = px.index
    rebal_days = [d for i, d in enumerate(idx) if i == 0
                 or d.month != idx[i - 1].month]
    tranches = []
    for d in rebal_days:
        row = mom.loc[d].dropna()
        if len(row) < 2 * n_leg:
            continue
        row = row.sort_values(ascending=False)
        longs, shorts = row.head(n_leg).index, row.tail(n_leg).index
        w = pd.Series(0.0, index=SECTORS)
        w[longs] = 0.5 / n_leg
        w[shorts] = -0.5 / n_leg
        i0 = idx.get_loc(d)
        tranche_ret = pd.Series(0.0, index=idx[i0:min(i0 + h, len(idx))])
        for j, dd in enumerate(tranche_ret.index):
            if j == 0:
                continue
            r = float((ret.loc[dd] * w).sum())
            tranche_ret.loc[dd] = r
        tranche_ret.iloc[0] -= float(w.abs().sum()) * cost_bps / 1e4  # Einstieg
        tranche_ret.iloc[-1] -= float(w.abs().sum()) * cost_bps / 1e4  # Ausstieg
        tranches.append(tranche_ret)
    if not tranches:
        return pd.Series(dtype=float)
    wide = pd.concat(tranches, axis=1)
    # k Tranchen können überlappen -> Durchschnitt der aktiven Tranchen je Tag
    return wide.mean(axis=1, skipna=True).dropna()


def stats(r: pd.Series, ann=252) -> dict:
    r = r.dropna()
    if len(r) < 200:
        return {}
    eq = (1 + r).cumprod()
    return {"n": len(r), "sharpe": r.mean() / r.std() * np.sqrt(ann),
            "cagr": eq.iloc[-1] ** (ann / len(r)) - 1,
            "maxdd": (eq / eq.cummax() - 1).min()}


def returns(h: int = 63, start="2000-01-01") -> pd.Series:
    """Discovery-Pipeline-Entry-Point."""
    px = load(start)
    mom = build_momentum(px)
    return _portfolio(px, mom, h)


def live_weights(h: int = 63) -> tuple[dict, str]:
    """G8-Entry-Point: aktuellste Sektor-Momentum-Rangfolge."""
    px = load(start="2023-01-01")
    if px.empty:
        return {}, "keine Sektor-Kursdaten (fail-closed)"
    mom = build_momentum(px)
    last = mom.index[-1]
    row = mom.loc[last].dropna()
    if len(row) < 2 * N_LEG:
        return {}, f"nur {len(row)} Sektoren am {last:%Y-%m-%d} → flat"
    row = row.sort_values(ascending=False)
    longs, shorts = row.head(N_LEG).index.tolist(), row.tail(N_LEG).index.tolist()
    w = 0.5 / N_LEG
    weights = {s: w for s in longs}
    weights.update({s: -w for s in shorts})
    return (weights, f"long {longs} / short {shorts} "
                     f"(12M-Momentum ex-1M, {last:%Y-%m-%d}), gross 100%")


def run():
    px = load()
    print(f"Universum: {px.shape[1]} Sektoren, "
          f"{px.index.min():%Y-%m} → {px.index.max():%Y-%m}\n")
    mom = build_momentum(px)

    print("═══ Variantenwahl (Training bis 2019) ═══")
    print(f"{'H':>4s} {'Sharpe':>8s} {'CAGR':>8s}")
    tr = {}
    for h in HORIZONS:
        r = _portfolio(px, mom, h).loc[:TRAIN_END]
        s = stats(r)
        tr[h] = s
        print(f"{h:4d} {s.get('sharpe', float('nan')):8.2f} "
              f"{s.get('cagr', float('nan')):+8.1%}")
    best = max(tr, key=lambda k: tr[k].get("sharpe", -9))
    print(f"\nGewählte Variante: H={best} Tage")

    print("\n═══ HOLDOUT 2020-2026 ═══")
    full = _portfolio(px, mom, best)
    ho = stats(full.loc["2020-01-01":])
    print(f"Sharpe {ho.get('sharpe', float('nan')):.2f} | "
          f"CAGR {ho.get('cagr', float('nan')):+.1%} | "
          f"MaxDD {ho.get('maxdd', float('nan')):.1%}")
    sf = stats(full)
    print(f"GESAMT: Sharpe {sf['sharpe']:.2f} | CAGR {sf['cagr']:+.1%} | "
          f"MaxDD {sf['maxdd']:.1%}")
    r22 = stats(full.loc["2022-01-01":])
    print(f"2022+ (Regel R4): Sharpe {r22.get('sharpe', float('nan')):.2f} | "
          f"CAGR {r22.get('cagr', float('nan')):+.1%}")

    print("\n═══ VORHERSAGE (a): monoton mit dem Momentum-Rang? ═══")
    monthly_mom = mom.resample("ME").last()
    monthly_ret = px.pct_change(21).resample("ME").last()
    rows = []
    for d in monthly_mom.index[1:]:
        prev = monthly_mom.index[monthly_mom.index.get_loc(d) - 1]
        m = monthly_mom.loc[prev].dropna()
        if len(m) < 3:
            continue
        rk = m.rank(pct=True)
        for s in m.index:
            if s in monthly_ret.columns and pd.notna(monthly_ret.loc[d, s]):
                rows.append({"rank": rk[s], "ret": monthly_ret.loc[d, s]})
    rdf = pd.DataFrame(rows)
    rdf["tercile"] = pd.qcut(rdf["rank"], 3, labels=["tief", "mittel", "hoch"])
    dec = rdf.groupby("tercile", observed=True)["ret"].mean() * 12
    print(dec.to_string())
    print(f"monoton steigend: {bool(dec.reindex(['tief','mittel','hoch']).is_monotonic_increasing)}")

    print("\n═══ VORHERSAGE (b): robust über Haltedauern? ═══")
    for h in HORIZONS:
        r = _portfolio(px, mom, h)
        s = stats(r)
        print(f"  H={h:3d}: Sharpe {s.get('sharpe', float('nan')):5.2f} | "
              f"CAGR {s.get('cagr', float('nan')):+7.1%}")

    print("\n═══ ORTHOGONALITÄT (Vorhersage c) ═══")
    from quant.research.discovery import live_sleeve_returns, portfolio_delta
    ex = live_sleeve_returns()
    before, after, rhos = portfolio_delta(full, ex, sf["sharpe"])
    for nm, rho in rhos.items():
        flag = " ⚠ REDUNDANT (>0.5)" if abs(rho) > 0.5 else ""
        print(f"  ρ(SECTORMOM, {nm}) = {rho:+.3f}{flag}")
    print(f"  Portfolio-Sharpe {before:.2f} → {after:.2f} "
          f"(Δ {after-before:+.3f})")

    print("\n═══ G5: Trials protokollieren ═══")
    from quant.research.trials_registry import log_trial
    for h in HORIZONS:
        try:
            log_trial("SECTORMOM", _portfolio(px, mom, h), variant=f"H={h}d",
                      verdict="KANDIDAT" if h == best else "Variante",
                      notes="US-Sektor-ETF-Momentum, 12M-ex-1M, 3-von-9 long/short")
        except Exception as e:  # noqa: BLE001
            print(f"  H={h}: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    if not a.run:
        ap.print_help()
        sys.exit(1)
    run()
