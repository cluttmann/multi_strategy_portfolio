"""Familie 21 (FXCARRY): G6-Zinsdifferenz-Carry (Fama 1984 Forward-Premium-Puzzle).

    python3 -m quant.research.fx_carry_study --run

MECHANISMUS: Ungedeckte Zinsparität (UIP) sagt voraus, dass die
Hochzinswährung um genau den Zinsvorteil abwertet — im Mittel passiert das
NICHT (Fama 1984). Wer die Hochzinswährung hält, wird für ein reales Risiko
bezahlt: Carry-Trades brechen abrupt in Risk-off-Phasen zusammen (2008, 2015
CHF-Freigabe, 2020), weil gehebelte Carry-Positionen dann gleichzeitig
aufgelöst werden — eine ECHTE Versicherungsprämie gegen Tail-Risiko, kein
Informationsvorsprung (Regel R1 erfüllt: Haltedauer ist ein Monat).

Anders als die ursprüngliche CARRY-Familie (gekillt, MECHANISMUS_WIDERLEGT +
REDUNDANT zu DTRD mit ρ=0.78): dort wurde die 12M-Trailing-Ausschüttungs-
rendite von ETFs als Carry-Proxy benutzt — träge und ETF-Struktur-verzerrt.
Hier wird die ZINSDIFFERENZ selbst benutzt (FRED-Kurzfristzinsen, monatlich
aktuell), genau die Lehre aus dem CARRY-Kill-Eintrag umgesetzt. Neue
Anlageklasse (G10-FX statt Cross-Asset-ETFs), neue Hypothese, eigener
Versuchszähler (Regel R6/R7) — kein Parameter-Sweep der alten Familie.

QUANTITATIVE VORHERSAGE (Regel R2):
  (a) Rendite skaliert MONOTON mit der Zinsdifferenz bei Formation — je
      höher der Zinsvorsprung der Long-Währung, desto höher die Rendite;
  (b) die Prämie bricht in Risk-off-Phasen zusammen (hoher VIX) — das ist
      der behauptete Versicherungscharakter, nicht ein Bug;
  (c) die Renditeverteilung ist LINKSSCHIEF (negative Schiefe) — viele
      kleine Gewinne, seltene große Verluste, das "Steamroller"-Profil, das
      Carry-Trades von reiner Zufallsrendite unterscheidet.

UNIVERSUM: 6 G10-Währungen mit verlässlicher FRED-Abdeckung (EUR, JPY, GBP,
CAD, AUD, CHF) vs. USD. Zinsen: OECD-3M-Interbankensätze
(IR3TIB01<CC>M156N). Spot: FRED-Tageskurse (DEXUSEU etc.).

VORREGISTRIERT (Regel R6): genau 3 Varianten — Beinstärke (Top/Bottom 1, 2,
3 von 6 Währungen). Die Zins- und Spot-Datenquelle sind fix (keine
Sweep-Achse). Training bis 2019, Holdout 2020-2026 (Regel R7).

LIVE-HANDELBARKEIT: Alpaca handelt keinen Spot-FX direkt. Live-Proxy sind
die CurrencyShares-ETFs (FXE/FXY/FXB/FXC/FXA/FXF) — unlevered
Fremdwährungs-Bareinlage-Tracker, die selbst schon einen Teil des
Zinsertrags einpreisen; das ist eine offengelegte Näherung an die reine
Spot+Zins-Konstruktion des Backtests, kein exakter Nachbau.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.research.exotic_sleeves import fred

RATE_SERIES = {
    "EUR": "IR3TIB01EZM156N", "JPY": "IR3TIB01JPM156N",
    "GBP": "IR3TIB01GBM156N", "CAD": "IR3TIB01CAM156N",
    "AUD": "IR3TIB01AUM156N", "CHF": "IR3TIB01CHM156N",
}
USD_RATE_SERIES = "IR3TIB01USM156N"
# FRED-FX-Konvention ist uneinheitlich: DEXUSEU/DEXUSUK/DEXUSAL sind USD je
# Fremdwährung (steigt = Fremdwährung wertet AUF), DEXJPUS/DEXCAUS/DEXSZUS
# sind Fremdwährung je USD (steigt = Fremdwährung wertet AB) — daher `invert`.
FX_SERIES = {
    "EUR": ("DEXUSEU", False), "JPY": ("DEXJPUS", True),
    "GBP": ("DEXUSUK", False), "CAD": ("DEXCAUS", True),
    "AUD": ("DEXUSAL", False), "CHF": ("DEXSZUS", True),
}
ETF_PROXY = {"EUR": "FXE", "JPY": "FXY", "GBP": "FXB", "CAD": "FXC",
            "AUD": "FXA", "CHF": "FXF"}

LEG_SIZES = [1, 2, 3]              # genau 3 Varianten (Regel R6)
TRAIN_END = "2019-12-31"
FUNDING_COST_BPS_YR = 0.0          # Zinsertrag/-kosten stecken schon im Diff


def load_panel(start="2007-01-01") -> pd.DataFrame:
    """Monatliche Renditen je Währung = Spot-Rendite + Zinsdifferenz/12."""
    usd = fred(USD_RATE_SERIES, start=start)
    usd_m = usd.resample("ME").last().ffill()
    frames = []
    for ccy, (series, invert) in FX_SERIES.items():
        spot = fred(series, start=start).dropna()
        if invert:
            spot = 1.0 / spot
        spot_m = spot.resample("ME").last()
        spot_ret = spot_m.pct_change()
        rate = fred(RATE_SERIES[ccy], start=start)
        rate_m = rate.resample("ME").last().ffill()
        df = pd.DataFrame({"spot_ret": spot_ret, "rate": rate_m,
                          "usd_rate": usd_m}).dropna(subset=["spot_ret"])
        df["rate"] = df["rate"].ffill()
        df["usd_rate"] = df["usd_rate"].ffill()
        # Formations-Zinsdifferenz ist die Rate VOM VORMONAT (ex-ante,
        # kein Blick in die Zukunft) — die Rendite dieses Monats reagiert
        # auf den Zinsvorsprung, der zu Monatsbeginn schon bekannt war.
        df["diff"] = (df["rate"] - df["usd_rate"]).shift(1)
        df["ret"] = df["spot_ret"] + (df["rate"].shift(1) / 100 / 12) \
                                    - (df["usd_rate"].shift(1) / 100 / 12)
        df["symbol"] = ccy
        frames.append(df.dropna(subset=["diff", "ret"])[["symbol", "diff", "ret"]])
    panel = pd.concat(frames).reset_index().rename(columns={"index": "date"})
    return panel


def _portfolio(panel: pd.DataFrame, n_leg: int) -> pd.Series:
    """Long die n_leg höchsten Zinsdifferenzen, short die n_leg niedrigsten,
    gleichgewichtet je Bein, dollar-neutral über beide Beine."""
    rows = []
    for d, g in panel.groupby("date"):
        g = g.sort_values("diff", ascending=False)
        if len(g) < 2 * n_leg:
            continue
        longs = g.head(n_leg)
        shorts = g.tail(n_leg)
        r = longs["ret"].mean() - shorts["ret"].mean()
        rows.append({"date": d, "ret": r})
    s = pd.DataFrame(rows).set_index("date")["ret"]
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def stats(r: pd.Series, ann=12) -> dict:
    r = r.dropna()
    if len(r) < 24:
        return {}
    eq = (1 + r).cumprod()
    return {"n": len(r), "sharpe": r.mean() / r.std() * np.sqrt(ann),
            "cagr": eq.iloc[-1] ** (ann / len(r)) - 1,
            "maxdd": (eq / eq.cummax() - 1).min(), "skew": r.skew()}


def returns(n_leg: int = 3, start: str = "2007-01-01") -> pd.Series:
    """Discovery-Pipeline-Entry-Point."""
    panel = load_panel(start)
    return _portfolio(panel, n_leg)


def live_weights(n_leg: int = 3) -> tuple[dict, str]:
    """G8-Entry-Point: aktuellster Zinsdifferenz-Rang, ETF-Proxy je Seite."""
    panel = load_panel(start="2020-01-01")
    if panel.empty:
        return {}, "keine FX-Zins-Daten (fail-closed)"
    last = panel["date"].max()
    day = panel[panel["date"] == last].sort_values("diff", ascending=False)
    if len(day) < 2 * n_leg:
        return {}, f"nur {len(day)} Währungen am {last:%Y-%m} → flat"
    longs = day.head(n_leg)["symbol"].tolist()
    shorts = day.tail(n_leg)["symbol"].tolist()
    w_leg = 0.5 / n_leg
    weights = {ETF_PROXY[c]: w_leg for c in longs}
    weights.update({ETF_PROXY[c]: -w_leg for c in shorts})
    return (weights, f"long {longs} / short {shorts} (Zinsdiff {last:%Y-%m}), "
                     f"ETF-Proxy, gross 100%")


def run():
    panel = load_panel()
    print(f"Panel: {panel['symbol'].nunique()} Währungen, "
          f"{panel['date'].min():%Y-%m} → {panel['date'].max():%Y-%m}\n")

    print("═══ Variantenwahl (Training bis 2019) ═══")
    print(f"{'n_leg':>6s} {'Sharpe':>8s} {'CAGR':>8s}")
    tr = {}
    for n in LEG_SIZES:
        r = _portfolio(panel, n).loc[:TRAIN_END]
        s = stats(r)
        tr[n] = s
        print(f"{n:6d} {s.get('sharpe', float('nan')):8.2f} "
              f"{s.get('cagr', float('nan')):+8.1%}")
    best = max(tr, key=lambda k: tr[k].get("sharpe", -9))
    print(f"\nGewählte Variante: n_leg={best}")

    print("\n═══ HOLDOUT 2020-2026 ═══")
    full = _portfolio(panel, best)
    ho = stats(full.loc["2020-01-01":])
    print(f"Sharpe {ho.get('sharpe', float('nan')):.2f} | "
          f"CAGR {ho.get('cagr', float('nan')):+.1%} | "
          f"MaxDD {ho.get('maxdd', float('nan')):.1%}")
    sf = stats(full)
    print(f"GESAMT: Sharpe {sf['sharpe']:.2f} | CAGR {sf['cagr']:+.1%} | "
          f"MaxDD {sf['maxdd']:.1%} | Schiefe {sf['skew']:+.2f}")
    r22 = stats(full.loc["2022-01-01":])
    print(f"2022+ (Regel R4): Sharpe {r22.get('sharpe', float('nan')):.2f} | "
          f"CAGR {r22.get('cagr', float('nan')):+.1%}")

    print("\n═══ VORHERSAGE (a): monoton mit der Zinsdifferenz? ═══")
    panel["decile"] = panel.groupby("date")["diff"].transform(
        lambda x: pd.qcut(x.rank(method="first"), min(6, x.nunique()),
                          labels=False, duplicates="drop"))
    dec = panel.groupby("decile")["ret"].mean() * 12
    print(dec.to_string())
    print(f"monoton steigend: {bool(dec.is_monotonic_increasing)}")

    print("\n═══ VORHERSAGE (b): bricht die Prämie bei hohem VIX zusammen? ═══")
    vix = fred("VIXCLS", start="2007-01-01").resample("ME").mean()
    v = vix.reindex(full.index, method="nearest")
    hi, lo = full[v > v.median()], full[v <= v.median()]
    sh, sl = stats(hi), stats(lo)
    print(f"  hoher VIX  Sharpe {sh.get('sharpe', float('nan')):5.2f} | "
          f"CAGR {sh.get('cagr', float('nan')):+7.1%} (n={sh.get('n', 0)})")
    print(f"  tiefer VIX Sharpe {sl.get('sharpe', float('nan')):5.2f} | "
          f"CAGR {sl.get('cagr', float('nan')):+7.1%} (n={sl.get('n', 0)})")

    print("\n═══ VORHERSAGE (c): linksschief? ═══")
    print(f"  Schiefe = {sf['skew']:+.2f} "
          f"({'BESTÄTIGT (negativ)' if sf['skew'] < 0 else 'WIDERLEGT (nicht negativ)'})")

    print("\n═══ ORTHOGONALITÄT ═══")
    from quant.research.discovery import live_sleeve_returns, portfolio_delta
    ex = live_sleeve_returns()
    before, after, rhos = portfolio_delta(full, ex, sf["sharpe"], ann=12)
    for nm, rho in rhos.items():
        flag = " ⚠ REDUNDANT (>0.5)" if abs(rho) > 0.5 else ""
        print(f"  ρ(FXCARRY, {nm}) = {rho:+.3f}{flag}")
    print(f"  Portfolio-Sharpe {before:.2f} → {after:.2f} "
          f"(Δ {after-before:+.3f})")

    print("\n═══ G5: Trials protokollieren ═══")
    from quant.research.trials_registry import log_trial
    for n in LEG_SIZES:
        try:
            log_trial("FXCARRY", _portfolio(panel, n), variant=f"n_leg={n}",
                      ann=12, verdict="KANDIDAT" if n == best else "Variante",
                      notes="G10-FX-Carry, Zinsdifferenz FRED, ETF-Proxy live")
        except Exception as e:  # noqa: BLE001
            print(f"  n_leg={n}: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    if not a.run:
        ap.print_help()
        sys.exit(1)
    run()
