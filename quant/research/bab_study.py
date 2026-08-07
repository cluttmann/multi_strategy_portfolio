"""Familie 20 (BAB): Betting Against Beta (Frazzini/Pedersen JFE 2014).

    python3 -m quant.research.bab_study --run

MECHANISMUS: Anleger mit Hebel-Restriktion (Fonds ohne Leverage-Mandat,
Privatanleger ohne Margin) wollen trotzdem mehr Marktexposure, als ihr
Kapital hergibt — sie kaufen dafür HOCHBETA-Titel statt sich zu verschulden.
Das treibt Hochbeta-Namen über ihren Fair Value und drückt Niedrigbeta-Namen
darunter. Wir haben Zugang zu Reg-T-Hebel und können die Gegenposition
einnehmen: long Low-Beta (gehebelt auf Marktexposure), short High-Beta — die
Prämie ist Kompensation für eine Bilanzrestriktion, die wir nicht haben,
kein Informationsvorsprung (Regel R1 erfüllt: wir sind hier nicht der
Ungeduldige, Haltedauer ist Wochen bis Monate).

QUANTITATIVE VORHERSAGE (Regel R2):
  (a) Rendite skaliert MONOTON FALLEND mit dem Beta-Dezil bei Formation —
      bricht die Monotonie, ist es kein reiner Beta-Effekt.
  (b) Der Effekt bleibt nach BETA-NEUTRALISIERUNG (Netto-Dollar-Beta ≈ 0,
      nicht nur dollar-neutral) positiv — sonst ist es nur eine versteckte
      Marktwette, die zufällig mit Beta korreliert.
  (c) Stärker in Perioden knapper Finanzierung (Chicago-Fed NFCI > 0, d.h.
      straffere Finanzbedingungen als im Schnitt) als in ruhigen Phasen —
      TED/Repo-Spread ist in `quant.fred_series` nicht vorhanden, NFCI ist
      der direkteste verfügbare Proxy für genau den im Mechanismus
      behaupteten Kanal (Bilanz-/Hebelrestriktion).

VORREGISTRIERT (Regel R6): genau 3 Varianten, die Haltedauer (21/42/63
Tage) — die 63-Tage-Beta-Schätzung selbst ist durch das vorhandene Feature
(`beta_63d`) fixiert, keine Sweep-Achse. Training bis 2019, Holdout
2020-2026 (Regel R7).

R5-RISIKO (aus hypothesis_queue.yaml): XSR nutzt z_beta_63d als Feature —
ρ zu XSR wird in G7 automatisch gemessen. Bei ρ > 0.5 ist BAB redundant,
kein neuer Sleeve.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.backtest.portfolio_sim import simulate_tranches
from quant.data.bq import query

HORIZONS = [21, 42, 63]          # genau 3 Varianten (Regel R6)
MIN_ADV = 10e6                    # Regel R3: nur das handelbare Tier
TRAIN_END = "2019-12-31"
COST_BPS = 10.0
N_SIDE = 50


def load(start="2007-01-01") -> pd.DataFrame:
    """Liquides Universum mit Beta + allem, was für die Sim nötig ist."""
    return query(f"""
      SELECT date, symbol, beta_63d, vol_63d, mcap, adv63,
             fwd_ret_1d, fwd_ret_5d
      FROM `trading-436516.quant.features_daily_v2`
      WHERE date >= '{start}' AND adv63 >= {MIN_ADV}
        AND mcap > 0 AND beta_63d IS NOT NULL AND vol_63d > 0
        AND fwd_ret_1d IS NOT NULL
    """)


def build_preds(df: pd.DataFrame) -> pd.DataFrame:
    """Score = -beta_63d: niedriges Beta → hoher Score → long; hohes Beta →
    niedriger Score → short. Ranking ist ordinal (simulate_tranches nimmt
    top/bottom N_SIDE nach Rang), Vorzeichenumkehr genügt — kein Z-Scoring
    nötig, weil das die Rangfolge innerhalb eines Tages nicht ändert."""
    out = df.dropna(subset=["beta_63d"]).copy()
    out["score"] = -out["beta_63d"]
    return out[["date", "symbol", "score", "fwd_ret_1d", "fwd_ret_5d",
               "vol_63d", "adv63", "beta_63d", "mcap"]]


def stats(r: pd.Series, ann=252) -> dict:
    r = r.dropna()
    if len(r) < 200:
        return {}
    eq = (1 + r).cumprod()
    return {"n": len(r), "sharpe": r.mean() / r.std() * np.sqrt(ann),
            "cagr": eq.iloc[-1] ** (ann / len(r)) - 1,
            "maxdd": (eq / eq.cummax() - 1).min(),
            "vol": r.std() * np.sqrt(ann)}


def _sim(preds: pd.DataFrame, h: int, n_side: int = N_SIDE) -> pd.Series:
    res = simulate_tranches(preds, n_side=n_side, cost_bps=COST_BPS, k=h)
    s = res["net_ret"]
    s.index = pd.to_datetime(s.index)
    return s


def returns(h: int = 21, start="2007-01-01", n_side: int = N_SIDE) -> pd.Series:
    """Discovery-Pipeline-Entry-Point."""
    df = load(start)
    preds = build_preds(df)
    return _sim(preds, h, n_side)


def live_weights(h: int = 21) -> tuple[dict, str]:
    """G8-Entry-Point: heutiger Beta-Querschnitt, top/bottom N_SIDE,
    inverse-vol je Seite, dollar-neutral (Gross 1.0) — spiegelt exakt die
    Gewichtungslogik aus portfolio_sim.simulate()."""
    df = load(start="2024-01-01")
    if df.empty:
        return {}, "keine Feature-Daten (fail-closed)"
    last_date = df["date"].max()
    day = build_preds(df[df["date"] == last_date])
    if len(day) < N_SIDE * 2:
        return {}, f"nur {len(day)} Namen am {last_date} → flat (fail-closed)"
    day = day.sort_values("score", ascending=False).reset_index(drop=True)
    longs = day.head(N_SIDE).set_index("symbol")
    shorts = day.tail(N_SIDE).set_index("symbol")

    def side_weights(g: pd.DataFrame, sign: float) -> dict:
        vol = g["vol_63d"].clip(lower=0.10)
        w = (1.0 / vol)
        w = w / w.sum() * 0.5   # 50% Gross je Seite → 100% Gesamt-Gross
        return {s: float(sign * v) for s, v in w.items()}

    weights = {**side_weights(longs, 1.0), **side_weights(shorts, -1.0)}
    gross = sum(abs(v) for v in weights.values())
    return (weights, f"{len(longs)} long / {len(shorts)} short, "
                     f"Beta-Dezil {last_date}, inverse-vol, gross {gross:.0%}")


def run():
    df = load()
    df["date"] = pd.to_datetime(df["date"])
    print(f"Universum: {df['symbol'].nunique():,} Namen, {len(df):,} Zeilen, "
          f"{df['date'].min():%Y-%m} → {df['date'].max():%Y-%m} "
          f"(ADV ≥ ${MIN_ADV/1e6:.0f}M)\n")

    preds_by_h = {h: build_preds(df) for h in HORIZONS}

    print("═══ Variantenwahl (Training bis 2019) ═══")
    print(f"{'H':>4s} {'Sharpe':>8s} {'CAGR':>8s}")
    tr = {}
    for h in HORIZONS:
        r = _sim(preds_by_h[h], h).loc[:TRAIN_END]
        s = stats(r)
        tr[h] = s
        print(f"{h:4d} {s.get('sharpe', float('nan')):8.2f} "
              f"{s.get('cagr', float('nan')):+8.1%}")
    best = max(tr, key=lambda k: tr[k].get("sharpe", -9))
    print(f"\nGewählte Variante: H={best} Tage")

    print("\n═══ HOLDOUT 2020-2026 (nie gefittet) ═══")
    full = _sim(preds_by_h[best], best)
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

    # ── Vorhersage (a): monoton fallend über Beta-Dezile ──
    print("\n═══ VORHERSAGE (a): Rendite monoton FALLEND über Beta-Dezile? ═══")
    p = preds_by_h[best].copy()
    p["decile"] = p.groupby("date")["beta_63d"].transform(
        lambda x: pd.qcut(x.rank(method="first"), 10, labels=False,
                          duplicates="drop"))
    dec_ret = p.groupby("decile").apply(
        lambda g: g["fwd_ret_1d"].mean() * 252, include_groups=False)
    print(dec_ret.to_string())
    monoton = bool(dec_ret.is_monotonic_decreasing)
    print(f"monoton fallend: {monoton}")

    # ── Vorhersage (b): nach Beta-Neutralisierung noch positiv? ──
    print("\n═══ VORHERSAGE (b): nach Netto-Beta-Neutralisierung noch positiv? ═══")
    day_groups = p.groupby("date")
    daily_alpha = []
    for d, g in day_groups:
        g = g.dropna(subset=["fwd_ret_1d", "beta_63d"])
        if len(g) < N_SIDE * 2:
            continue
        g = g.sort_values("score", ascending=False)
        longs, shorts = g.head(N_SIDE), g.tail(N_SIDE)
        # Beta-neutrale Skalierung: jede Seite wird so gehebelt, dass ihr
        # Netto-Dollar-Beta = 1 ist (Frazzini/Pedersen-Konstruktion), statt
        # nur dollar-neutral zu sein — das ist der eigentliche Test von (b).
        bl, bs = longs["beta_63d"].mean(), shorts["beta_63d"].mean()
        if bl <= 0 or bs <= 0:
            continue
        rl = longs["fwd_ret_1d"].mean() / bl
        rs = shorts["fwd_ret_1d"].mean() / bs
        daily_alpha.append({"date": d, "ret": rl - rs})
    ba = pd.DataFrame(daily_alpha).set_index("date")["ret"]
    bstat = stats(ba)
    print(f"beta-neutral Sharpe {bstat.get('sharpe', float('nan')):.2f} | "
          f"CAGR {bstat.get('cagr', float('nan')):+.1%} (n={bstat.get('n', 0)})")

    # ── Vorhersage (c): stärker bei knapper Finanzierung (NFCI > 0)? ──
    print("\n═══ VORHERSAGE (c): stärker bei straffen Finanzbedingungen (NFCI)? ═══")
    nfci = query("SELECT date, value FROM `trading-436516.quant.fred_series` "
                "WHERE series = 'NFCI'")
    nfci["date"] = pd.to_datetime(nfci["date"])
    nf = nfci.set_index("date")["value"].reindex(full.index).ffill()
    tight = full[nf > 0]
    loose = full[nf <= 0]
    st, sl = stats(tight), stats(loose)
    print(f"  straff (NFCI>0)  Sharpe {st.get('sharpe', float('nan')):5.2f} | "
          f"CAGR {st.get('cagr', float('nan')):+7.1%} (n={st.get('n', 0)})")
    print(f"  locker (NFCI<=0) Sharpe {sl.get('sharpe', float('nan')):5.2f} | "
          f"CAGR {sl.get('cagr', float('nan')):+7.1%} (n={sl.get('n', 0)})")

    # ── Orthogonalität (R5-Risiko: ρ zu XSR wegen z_beta_63d-Feature) ──
    print("\n═══ ORTHOGONALITÄT ═══")
    from quant.research.discovery import live_sleeve_returns, portfolio_delta
    ex = live_sleeve_returns()
    before, after, rhos = portfolio_delta(full, ex, sf["sharpe"])
    for nm, rho in rhos.items():
        flag = " ⚠ REDUNDANT (>0.5)" if abs(rho) > 0.5 else ""
        print(f"  ρ(BAB, {nm}) = {rho:+.3f}{flag}")
    print(f"  Portfolio-Sharpe {before:.2f} → {after:.2f} "
          f"(Δ {after-before:+.3f})")

    # ── Trial-Registry: alle 3 Horizont-Varianten ──
    print("\n═══ G5: Trials protokollieren ═══")
    from quant.research.trials_registry import log_trial
    for h in HORIZONS:
        try:
            log_trial("BAB", _sim(preds_by_h[h], h), variant=f"H={h}d",
                      verdict="KANDIDAT" if h == best else "Variante",
                      notes="Betting-Against-Beta, beta_63d-Rang, inverse-vol")
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
