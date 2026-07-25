"""Familie 17 (RESID-MR): Residuale Mean-Reversion (Blitz/Huij/Lansdorp 2013).

    python3 -m quant.research.resid_mr_study --run

MECHANISMUS: Wer eine große Position schnell auflösen muss (Fonds-Abflüsse,
Risikolimits, Index-Umschichtung), drückt den Kurs unter den Fair Value und
zahlt dafür eine Prämie an den Liquiditätsgeber. Das ist eine
KOMPENSATION FÜR RISIKOTRANSFER, kein Informationsvorsprung — genau wie die
Trendprämie in DTRD, und deshalb nicht durch Publikation arbitrierbar. Wir
sind hier der Geduldige, der Gegenpart der Ungeduldige (Regel R1 erfüllt).

QUANTITATIVE VORHERSAGE (Regel R2 — das ist der Test, nicht das Backtest-
Ergebnis): Wenn der Effekt Liquiditätsprämie ist, muss er mit dem PREIS DER
LIQUIDITÄT skalieren:
  (a) stärker in illiquiden Namen (Amihud hoch) als in liquiden,
  (b) stärker in Hochvol-Phasen (Kapital knapp) als in ruhigen,
  (c) RESIDUALE Reversion > ROHE Reversion, weil rohe Umkehr zum Teil nur
      Faktor-Umkehr ist (und Faktorprämien sind KEINE Liquiditätsprämie).
Bricht (c), ist der Mechanismus widerlegt — dann sterben wir wie LETF_REBAL.

WARUM RESIDUAL: Rohe 1-Monats-Umkehr korreliert stark mit Value/Beta-Umkehr
und ist im liquiden Segment abgeerntet. Residualisiert man vorher gegen
Sektor, Beta und Größe, bleibt der idiosynkratische Teil — der laut Paper
einen ~2x höheren Sharpe hat und stabiler ist.

VORREGISTRIERT (Regel R6): genau 3 Varianten (Formation/Haltedauer 10/21/63
Tage), überlappende Tranchen, dollar-neutral, inverse-vol, Kosten 10bp/Seite
+ 200bp/Jahr Leihe. Training bis 2019, 2020-2026 striktes Holdout (Regel R7).
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.backtest.portfolio_sim import simulate_tranches
from quant.data.bq import query

HORIZONS = [10, 21, 63]          # genau 3 Varianten
MIN_ADV = 10e6                   # Regel R3: nur das handelbare Tier
TRAIN_END = "2019-12-31"
COST_BPS = 10.0
N_SIDE = 50


def load(start="2007-01-01") -> pd.DataFrame:
    """Liquides Universum mit allem, was für Residualisierung + Sim nötig ist."""
    return query(f"""
      SELECT date, symbol, sector_id, mcap, beta_63d, vol_63d, amihud_21d,
             ret_10d, ret_21d, ret_63d, fwd_ret_1d, fwd_ret_5d, adv63
      FROM `trading-436516.quant.features_daily_v2`
      WHERE date >= '{start}' AND adv63 >= {MIN_ADV}
        AND mcap > 0 AND beta_63d IS NOT NULL AND vol_63d > 0
        AND fwd_ret_1d IS NOT NULL
    """)


def residualize(df: pd.DataFrame, col: str) -> pd.Series:
    """Querschnittsresiduum von `col` gegen Sektor, Beta und Größe — je Tag.

    Sektor per Gruppen-Demeaning, dann exakte 2-Regressor-OLS (Beta, log-Größe)
    über Groupby-Summen. Vollvektorisiert, kein Schleifen-Fit pro Tag.
    """
    d = df[["date", "sector_id", col, "beta_63d", "mcap"]].copy()
    d["size"] = np.log(d["mcap"].clip(lower=1.0))
    # 1) Sektor-Demeaning (auch der Regressoren — sonst leckt Sektor über Beta rein)
    g_sec = d.groupby(["date", "sector_id"])
    for c in (col, "beta_63d", "size"):
        d[c] = d[c] - g_sec[c].transform("mean")
    # 2) Tages-Demeaning, damit kein Intercept nötig ist
    g_day = d.groupby("date")
    for c in (col, "beta_63d", "size"):
        d[c] = d[c] - g_day[c].transform("mean")
    # 3) exakte OLS je Tag: [b1,b2] = Sxx^-1 Sxy
    x1, x2, y = d["beta_63d"], d["size"], d[col]
    s = pd.DataFrame({"x11": x1 * x1, "x22": x2 * x2, "x12": x1 * x2,
                      "y1": x1 * y, "y2": x2 * y},
                     index=d.index).groupby(d["date"]).sum()
    det = s["x11"] * s["x22"] - s["x12"] ** 2
    det = det.where(det.abs() > 1e-12)
    b1 = (s["x22"] * s["y1"] - s["x12"] * s["y2"]) / det
    b2 = (s["x11"] * s["y2"] - s["x12"] * s["y1"]) / det
    resid = y - x1 * d["date"].map(b1).fillna(0.0) \
              - x2 * d["date"].map(b2).fillna(0.0)
    # 4) auf Tagesebene standardisieren → vergleichbare Scores über die Zeit
    m = resid.groupby(d["date"]).transform("mean")
    sd = resid.groupby(d["date"]).transform("std").replace(0, np.nan)
    return ((resid - m) / sd).clip(-4, 4)


def build_preds(df: pd.DataFrame, h: int, residual=True) -> pd.DataFrame:
    """Score = −Residuum der Formationsrendite (Umkehr: Verlierer long)."""
    col = f"ret_{h}d"
    out = df.dropna(subset=[col]).copy()
    if residual:
        out["score"] = -residualize(out, col)
    else:
        z = out.groupby("date")[col]
        out["score"] = -((out[col] - z.transform("mean"))
                         / z.transform("std").replace(0, np.nan)).clip(-4, 4)
    return out.dropna(subset=["score"])[
        ["date", "symbol", "score", "fwd_ret_1d", "fwd_ret_5d",
         "vol_63d", "amihud_21d", "adv63"]]


def stats(r: pd.Series, ann=252) -> dict:
    r = r.dropna()
    if len(r) < 200:
        return {}
    eq = (1 + r).cumprod()
    return {"n": len(r), "sharpe": r.mean() / r.std() * np.sqrt(ann),
            "cagr": eq.iloc[-1] ** (ann / len(r)) - 1,
            "maxdd": (eq / eq.cummax() - 1).min(),
            "vol": r.std() * np.sqrt(ann)}


def _sim(preds: pd.DataFrame, h: int) -> pd.Series:
    res = simulate_tranches(preds, n_side=N_SIDE, cost_bps=COST_BPS, k=h)
    s = res["net_ret"]
    s.index = pd.to_datetime(s.index)
    return s


def returns(h: int = 21, residual: bool = True, start="2007-01-01",
            n_side: int = N_SIDE) -> pd.Series:
    """Entry-Point für die Discovery-Pipeline."""
    df = load(start)
    preds = build_preds(df, h, residual=residual)
    res = simulate_tranches(preds, n_side=n_side, cost_bps=COST_BPS, k=h)
    s = res["net_ret"]
    s.index = pd.to_datetime(s.index)
    return s


def run():
    df = load()
    df["date"] = pd.to_datetime(df["date"])
    print(f"Universum: {df['symbol'].nunique():,} Namen, {len(df):,} Zeilen, "
          f"{df['date'].min():%Y-%m} → {df['date'].max():%Y-%m} "
          f"(ADV ≥ ${MIN_ADV/1e6:.0f}M)\n")

    # ── Vorhersage (c): residual muss roh schlagen, sonst Mechanismus widerlegt ──
    print("═══ VORHERSAGE (c): residual vs. roh, Training bis 2019 ═══")
    print(f"{'H':>4s} {'residual':>18s} {'roh':>18s}")
    tr = {}
    for h in HORIZONS:
        row = []
        for res_flag in (True, False):
            r = _sim(build_preds(df, h, residual=res_flag), h).loc[:TRAIN_END]
            s = stats(r)
            row.append(s)
            if res_flag:
                tr[h] = s
        print(f"{h:4d} {row[0].get('sharpe', float('nan')):8.2f} "
              f"{row[0].get('cagr', float('nan')):+8.1%} "
              f"{row[1].get('sharpe', float('nan')):8.2f} "
              f"{row[1].get('cagr', float('nan')):+8.1%}")
    best = max(tr, key=lambda k: tr[k].get("sharpe", -9))
    print(f"\nGewählte Variante: H={best} Tage")

    # ── Holdout ──
    print("\n═══ HOLDOUT 2020–2026 (nie gefittet) ═══")
    full = _sim(build_preds(df, best), best)
    ho = stats(full.loc["2020-01-01":])
    print(f"Sharpe {ho['sharpe']:.2f} | CAGR {ho['cagr']:+.1%} | "
          f"Vol {ho['vol']:.1%} | MaxDD {ho['maxdd']:.1%}")
    y = full.loc["2020-01-01":]
    print("Jahre: " + "  ".join(
        f"{k}:{v:+.0%}" for k, v in
        y.groupby(y.index.year).apply(lambda x: (1 + x).prod() - 1).items()))
    sf = stats(full)
    print(f"\nGESAMT: Sharpe {sf['sharpe']:.2f} | CAGR {sf['cagr']:+.1%} | "
          f"MaxDD {sf['maxdd']:.1%}")
    r22 = stats(full.loc["2022-01-01":])
    print(f"2022+ (Regel R4): Sharpe {r22.get('sharpe', float('nan')):.2f} | "
          f"CAGR {r22.get('cagr', float('nan')):+.1%}")

    # ── Vorhersage (a): Liquiditätstier — muss illiquide > liquide ──
    print("\n═══ VORHERSAGE (a): skaliert der Effekt mit Illiquidität? ═══")
    p = build_preds(df, best)
    p["tier"] = p.groupby("date")["adv63"].transform(
        lambda x: pd.qcut(x.rank(method="first"), 2, labels=["illiquid", "liquid"]))
    for t in ("illiquid", "liquid"):
        sub = p[p["tier"] == t]
        s = stats(_sim(sub, best))
        print(f"  {t:9s} Sharpe {s.get('sharpe', float('nan')):5.2f} | "
              f"CAGR {s.get('cagr', float('nan')):+7.1%} "
              f"({sub['symbol'].nunique():,} Namen)")

    # ── Vorhersage (b): Hochvol-Phasen stärker ──
    print("\n═══ VORHERSAGE (b): stärker wenn Kapital knapp (VIX-Terzile)? ═══")
    vix = query("""SELECT date, value AS vix FROM `trading-436516.quant.fred_series`
                   WHERE series = 'VIXCLS' AND value > 0""")
    vix["date"] = pd.to_datetime(vix["date"])
    v = vix.set_index("date")["vix"].reindex(full.index).ffill()
    terc = pd.qcut(v.dropna(), 3, labels=["ruhig", "mittel", "stress"])
    for lab in ("ruhig", "mittel", "stress"):
        s = stats(full.loc[terc[terc == lab].index])
        print(f"  VIX {lab:7s} Sharpe {s.get('sharpe', float('nan')):5.2f} | "
              f"CAGR {s.get('cagr', float('nan')):+7.1%}")

    # ── Orthogonalität ──
    print("\n═══ ORTHOGONALITÄT ═══")
    from quant.research.discovery import live_sleeve_returns, portfolio_delta
    ex = live_sleeve_returns()
    before, after, rhos = portfolio_delta(full, ex, sf["sharpe"])
    for nm, rho in rhos.items():
        print(f"  ρ(RESID-MR, {nm}) = {rho:+.3f}")
    print(f"  Portfolio-Sharpe {before:.2f} → {after:.2f} "
          f"(Δ {after-before:+.3f})")

    # ── Trial-Registry: alle 6 Läufe (3 Horizonte × residual/roh) ──
    print("\n═══ G5: Trials protokollieren ═══")
    from quant.research.trials_registry import log_trial
    for h in HORIZONS:
        for res_flag in (True, False):
            tag = "residual" if res_flag else "roh"
            try:
                log_trial("RESID-MR", _sim(build_preds(df, h, res_flag), h),
                          variant=f"H={h}d {tag}",
                          verdict="KANDIDAT" if (res_flag and h == best)
                                  else "Variante",
                          notes="Querschnitts-Umkehr auf Sektor/Beta/Größe-Residuum")
            except Exception as e:  # noqa: BLE001
                print(f"  H={h} {tag}: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    if not a.run:
        ap.print_help()
        sys.exit(1)
    run()
