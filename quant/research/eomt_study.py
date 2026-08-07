"""Familie 15 (EOMT): Monatsend-Duration-Ernte in Treasury-ETFs.

    python3 -m quant.research.eomt_study --run

MECHANISMUS (Zwangsfluss, kein Informations-Edge): Index-gebundene Real-Money-
Investoren (Lebensversicherer, passive Bond-Fonds) müssen zum Monatsend-
Rebalancing Duration verlängern, weil die Indizes neu emittierte lange Papiere
aufnehmen. Kalendarisch erzwungen und preis-insensitiv. Zahler ist der
benchmark-gebundene Anleihenkäufer, der am letzten Handelstag Immediacy kauft.

EVIDENZ: Hartley & Schwarz (1990-2018): 10y-Note letzte 3 Tage +0.25%/Monat,
Sharpe ≈1.0 nach Kosten. NY Fed 2024: Volumen in Benchmark-Treasuries am
letzten Monatstag seit 2020 ~46% höher (Mechanismus lebt).

VORREGISTRIERT, VOR DEM ERSTEN LAUF FIXIERT:
  H1  Überrendite der letzten k Handelstage des Monats ist POSITIV
  H2  Sie steigt MONOTON mit der Duration (SHY < IEF < TLT < EDV)
  Variantensatz: genau 4 (Entry T-2, T-3, T-4, T-5) — eng gehalten, damit
      sd_trials klein bleibt und das G5-Gate (DSR>0.95) passierbar ist.
  Trainingssample 2002-2019; 2020-2026 ist STRIKTES HOLDOUT (kein Fit darauf).
  Kosten 4bp Round-Trip (MOC/MOO auf liquide Treasury-ETFs).
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.data.bq import query

COST = 4 / 1e4
UNIVERSE = ["SHY", "IEF", "TLT", "EDV"]      # aufsteigende Duration
DURATION = {"SHY": 1.9, "IEF": 7.5, "TLT": 17.5, "EDV": 24.0}
VARIANTS = [2, 3, 4, 5]                       # Entry T-k, genau 4 Varianten
TRAIN_END = "2019-12-31"


def load() -> pd.DataFrame:
    q = ", ".join(repr(s) for s in UNIVERSE)
    df = query(f"""
      SELECT date, symbol, adjusted_close AS ac
      FROM `trading-436516.quant.eod_bars`
      WHERE symbol IN ({q}) AND adjusted_close > 0 AND date >= '2002-01-01'
      ORDER BY date""")
    df["date"] = pd.to_datetime(df["date"])
    px = df.pivot(index="date", columns="symbol", values="ac").sort_index()
    return px


def month_end_returns(px: pd.DataFrame, k: int) -> pd.DataFrame:
    """Rendite von Close T-k bis Close des letzten Handelstags je Monat."""
    ret = px.pct_change()
    # Handelstag-Index innerhalb des Monats, von hinten gezählt (1 = letzter)
    grp = px.index.to_period("M")
    pos_from_end = pd.Series(index=px.index, dtype=float)
    for _, idx in pd.Series(px.index, index=grp).groupby(level=0):
        n = len(idx)
        pos_from_end.loc[idx.values] = np.arange(n, 0, -1)
    # Fenster: die letzten k Tage (pos_from_end <= k)
    window = pos_from_end <= k
    out = {}
    for sym in px.columns:
        r = ret[sym].where(window)
        out[sym] = r.groupby(grp).apply(lambda s: (1 + s.dropna()).prod() - 1
                                       if s.notna().any() else np.nan)
    res = pd.DataFrame(out)
    res.index = res.index.to_timestamp()
    return res.dropna(how="all")


def stats(r: pd.Series, per_year=12) -> dict:
    r = r.dropna()
    if len(r) < 12:
        return {}
    sh = r.mean() / r.std() * np.sqrt(per_year)
    cagr = (1 + r).prod() ** (per_year / len(r)) - 1
    eq = (1 + r).cumprod()
    return {"n": len(r), "mean_bp": r.mean() * 1e4, "sharpe": sh,
            "cagr": cagr, "hit": (r > 0).mean(),
            "maxdd": (eq / eq.cummax() - 1).min(), "skew": r.skew()}


def run():
    px = load()
    print(f"Daten: {list(px.columns)}, {px.index.min():%Y-%m} → "
          f"{px.index.max():%Y-%m}")

    print("\n═══ H1/H2 im TRAININGSSAMPLE 2002–2019 ═══")
    print(f"{'k':>2s} {'Symbol':7s} {'Dur':>5s} {'n':>4s} {'bp/Mon':>8s} "
          f"{'Sharpe':>7s} {'Treffer':>8s} {'Schiefe':>8s}")
    train_res = {}
    for k in VARIANTS:
        me = month_end_returns(px, k)
        me_tr = me.loc[:TRAIN_END]
        for sym in UNIVERSE:
            if sym not in me_tr:
                continue
            s = stats(me_tr[sym] - COST)
            if not s:
                continue
            train_res[(k, sym)] = s
            print(f"{k:2d} {sym:7s} {DURATION[sym]:5.1f} {s['n']:4d} "
                  f"{s['mean_bp']:8.1f} {s['sharpe']:7.2f} {s['hit']:7.0%} "
                  f"{s['skew']:8.2f}")

    # H2: Monotonie in Duration je Variante
    print("\nH2 Monotonie in Duration (Sharpe je k):")
    for k in VARIANTS:
        sh = [train_res.get((k, s), {}).get("sharpe", np.nan) for s in UNIVERSE]
        mono = all(a <= b for a, b in zip(sh[:-1], sh[1:])
                   if not (np.isnan(a) or np.isnan(b)))
        print(f"  k={k}: " + " → ".join(f"{s} {v:.2f}" for s, v in
                                        zip(UNIVERSE, sh))
              + f"   {'MONOTON ✓' if mono else 'nicht monoton'}")

    # Bestes k im Training wählen (nur unter den 4 vorregistrierten)
    best_k = max(VARIANTS, key=lambda k: np.nanmean(
        [train_res.get((k, s), {}).get("sharpe", np.nan)
         for s in ("IEF", "TLT", "EDV")]))
    print(f"\nGewählte Variante aus dem Training: k = {best_k}")

    print("\n═══ HOLDOUT 2020–2026 (nie gefittet) ═══")
    me = month_end_returns(px, best_k)
    ho = me.loc["2020-01-01":]
    print(f"{'Symbol':7s} {'n':>4s} {'bp/Mon':>8s} {'Sharpe':>7s} "
          f"{'CAGR':>8s} {'Treffer':>8s} {'MaxDD':>8s}")
    for sym in UNIVERSE:
        s = stats(ho[sym] - COST)
        if s:
            print(f"{sym:7s} {s['n']:4d} {s['mean_bp']:8.1f} {s['sharpe']:7.2f} "
                  f"{s['cagr']:+8.1%} {s['hit']:7.0%} {s['maxdd']:8.1%}")

    # Handelbares Sleeve: EW über IEF/TLT/EDV, nur Monatsend-Fenster
    print("\n═══ SLEEVE (EW IEF/TLT/EDV, k=%d) ═══" % best_k)
    for label, sub in [("Training 2002-2019", me.loc[:TRAIN_END]),
                       ("Holdout 2020-2026", me.loc["2020-01-01":]),
                       ("Gesamt", me)]:
        port = sub[["IEF", "TLT", "EDV"]].mean(axis=1) - COST
        s = stats(port)
        if s:
            print(f"{label:20s} n={s['n']:3d} Sharpe={s['sharpe']:5.2f} "
                  f"CAGR={s['cagr']:+6.1%} Treffer={s['hit']:4.0%} "
                  f"MaxDD={s['maxdd']:6.1%} Schiefe={s['skew']:+5.2f}")

    # Trial-Registry: alle 4 Varianten protokollieren (ehrliche Versuchszahl)
    print("\n═══ G5: Deflated Sharpe (alle 4 Varianten protokolliert) ═══")
    from quant.research.trials_registry import log_trial
    for k in VARIANTS:
        m = month_end_returns(px, k)
        port = m[["IEF", "TLT", "EDV"]].mean(axis=1) - COST
        try:
            log_trial("EOMT", port.dropna(), variant=f"Entry T-{k}",
                      verdict="KANDIDAT" if k == best_k else "Variante",
                      notes="Monatsend-Duration, vorregistriert", ann=12,
                      config={"k": k, "univ": ["IEF", "TLT", "EDV"]})
        except Exception as e:  # noqa: BLE001
            print(f"  k={k}: log fehlgeschlagen ({e})")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    a = p.parse_args()
    if not a.run:
        p.print_help()
        sys.exit(1)
    run()
