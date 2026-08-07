"""Familie 16 (DTRD): Cross-Asset-Trendfolge über ETFs — ohne Aktien, ohne Krypto.

    python3 -m quant.research.dtrd_study --run

MECHANISMUS: Die Trendfolgeprämie ist eine Gebühr für Risikotransfer an Hedger
(Produzenten bei Rohstoffen, Duration-Hedger bei Anleihen) plus langsame
Makro-Informationsdiffusion. Es ist KEIN Informationsvorsprung, deshalb
verfällt sie nicht durch Publikation — anders als IMOM/GAP/PEAD, die wir
gekillt haben.

EVIDENZ (ehrlich die Live-Zahl, nicht der Prospekt): SG CTA Index Sharpe 0.61
seit 2000 — 25 Jahre echtes OOS einer 300-Mrd-Industrie, netto nach Gebühren.
DBMF live 2020-2024 ≈ Sharpe 0.35-0.40. Gegenevidenz eingepreist:
Huang/Li/Wang/Zhou (JFE 2020) finden Kurzfrist-Trend (<1 Woche) zerfallen,
6-12M-Trend nicht. Erwartung daher 0.35-0.45, NICHT die 0.72 der Replikations-
Papiere.

WARUM DAS ZU UNS PASST: monatliche Umschichtung → Turnover ~0.1/Monat statt
0.55/Tag. Unser Kostenwall (der IMOM/GAP/CAT/PEAD getötet hat) trifft
Hochfrequenz; ein Monats-Sleeve ist praktisch kostenimmun. Und: keine Aktien,
keine Vol-Prämie, kein Krypto → strukturell orthogonal zu XSR/ONX/VOLC/EOMT.

VORREGISTRIERT: genau 3 Varianten (Lookback 3/6/12 Monate), Vol-Target 10 %
je Position, long/flat (kein Short — Reg-T-schonend und ETF-Leihkosten-frei),
Kosten 5bp/Seite. Trainingssample bis 2019, 2020-2026 striktes Holdout.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.data.bq import query

COST_BPS = 5.0
LOOKBACKS = [63, 126, 252]        # ~3/6/12 Monate, genau 3 Varianten
VOL_TARGET = 0.10
TRAIN_END = "2019-12-31"

# Cross-Asset-Universum, bewusst OHNE US-Aktien und OHNE Krypto
UNIVERSE = {
    "Anleihen":   ["TLT", "IEF", "SHY", "LQD", "HYG", "TIP", "EMB"],
    "Rohstoffe":  ["GLD", "SLV", "DBC", "USO", "DBA", "UNG", "PPLT"],
    "Währungen":  ["UUP", "FXE", "FXY", "FXB", "FXF"],
    "Intl-Aktien": ["EFA", "EEM", "EWJ", "FXI", "EWZ", "EWG", "EWU", "VGK"],
    "Immobilien": ["VNQ", "RWX", "IYR"],
}
ALL = [s for v in UNIVERSE.values() for s in v]


def load() -> pd.DataFrame:
    q = ", ".join(repr(s) for s in ALL)
    df = query(f"""
      SELECT date, symbol, adjusted_close AS ac, close * volume AS dvol
      FROM `trading-436516.quant.eod_bars`
      WHERE symbol IN ({q}) AND adjusted_close > 0 AND date >= '2004-01-01'
      ORDER BY date""")
    df["date"] = pd.to_datetime(df["date"])
    px = df.pivot(index="date", columns="symbol", values="ac").sort_index()
    dv = df.pivot(index="date", columns="symbol", values="dvol").sort_index()
    # Liquiditätsfilter: nur Tage/Namen mit >= 5M$ ADV20
    adv = dv.rolling(20).mean()
    px = px.where(adv >= 5e6)
    return px.ffill(limit=3)


def sleeve(px: pd.DataFrame, lookback: int, cost_bps=COST_BPS) -> pd.Series:
    """Long/flat TSMOM, inverse-vol auf Vol-Target, monatliche Umschichtung."""
    ret = px.pct_change()
    mom = px / px.shift(lookback) - 1
    vol = ret.rolling(63).std() * np.sqrt(252)
    sig = (mom > 0) & px.notna() & vol.notna()
    w_raw = sig.astype(float) * (VOL_TARGET / vol.clip(lower=0.03))
    # Gross auf 1.0 begrenzen (kein Hebel)
    w_raw = w_raw.div(w_raw.sum(axis=1).clip(lower=1.0), axis=0)
    # monatliche Umschichtung: Gewichte nur am Monatsanfang neu setzen
    month = w_raw.index.to_period("M")
    reb = pd.Series(month, index=w_raw.index).ne(
        pd.Series(month, index=w_raw.index).shift(1))
    w = w_raw.where(reb).ffill().shift(1).fillna(0.0)
    gross = (w * ret).sum(axis=1)
    turn = w.diff().abs().sum(axis=1).fillna(0.0)
    return (gross - turn * cost_bps / 1e4).dropna()


def stats(r: pd.Series) -> dict:
    r = r.dropna()
    if len(r) < 250:
        return {}
    yrs = len(r) / 252
    eq = (1 + r).cumprod()
    return {"n": len(r), "sharpe": r.mean() / r.std() * np.sqrt(252),
            "cagr": eq.iloc[-1] ** (1 / yrs) - 1,
            "maxdd": (eq / eq.cummax() - 1).min(),
            "vol": r.std() * np.sqrt(252), "skew": r.skew()}


def run():
    px = load()
    print(f"Universum: {px.shape[1]} ETFs, {px.index.min():%Y-%m} → "
          f"{px.index.max():%Y-%m}\n")

    print("═══ TRAINING bis 2019 (Variantenwahl) ═══")
    print(f"{'Lookback':>9s} {'Sharpe':>7s} {'CAGR':>7s} {'Vol':>6s} "
          f"{'MaxDD':>7s} {'Schiefe':>8s}")
    tr_res = {}
    for lb in LOOKBACKS:
        s = stats(sleeve(px, lb).loc[:TRAIN_END])
        if s:
            tr_res[lb] = s
            print(f"{lb:9d} {s['sharpe']:7.2f} {s['cagr']:+7.1%} "
                  f"{s['vol']:6.1%} {s['maxdd']:7.1%} {s['skew']:+8.2f}")
    best = max(tr_res, key=lambda k: tr_res[k]["sharpe"])
    print(f"\nGewählte Variante: Lookback {best} Tage")

    print("\n═══ HOLDOUT 2020–2026 (nie gefittet) ═══")
    r_ho = sleeve(px, best).loc["2020-01-01":]
    s = stats(r_ho)
    print(f"Sharpe {s['sharpe']:.2f} | CAGR {s['cagr']:+.1%} | "
          f"Vol {s['vol']:.1%} | MaxDD {s['maxdd']:.1%} | "
          f"Schiefe {s['skew']:+.2f}")
    yearly = r_ho.groupby(r_ho.index.year).apply(lambda x: (1 + x).prod() - 1)
    print("Jahre: " + "  ".join(f"{y}:{v:+.0%}" for y, v in yearly.items()))

    full = sleeve(px, best)
    sf = stats(full)
    print(f"\nGESAMT 2004–2026: Sharpe {sf['sharpe']:.2f} | "
          f"CAGR {sf['cagr']:+.1%} | MaxDD {sf['maxdd']:.1%} | "
          f"Turnover-Kosten sind einkalkuliert")

    # Orthogonalität — der eigentliche Portfolio-Wert
    print("\n═══ ORTHOGONALITÄT zu den bestehenden Sleeves ═══")
    import os
    from quant.config import STAGING_DIR
    from quant.ops.sleeve_health import onx_returns, volc_returns
    others = {}
    try:
        sim = pd.read_parquet(os.path.join(STAGING_DIR, "sim_wf_v2_full.parquet"))
        others["XSR"] = sim["net_ret"]
    except Exception:  # noqa: BLE001
        pass
    for nm, fn in (("ONX", onx_returns), ("VOLC", volc_returns)):
        try:
            others[nm] = fn()
        except Exception:  # noqa: BLE001
            pass
    for nm, o in others.items():
        o.index = pd.to_datetime(o.index)
        j = pd.DataFrame({"dtrd": full, nm: o}).dropna()
        if len(j) > 100:
            print(f"  ρ(DTRD, {nm}) = {j.corr().iloc[0,1]:+.3f}  "
                  f"({len(j):,} gemeinsame Tage)")

    # Trial-Registry: alle 3 Varianten (ehrliche Versuchszahl für den DSR)
    print("\n═══ G5: Deflated Sharpe ═══")
    from quant.research.trials_registry import log_trial
    for lb in LOOKBACKS:
        try:
            log_trial("DTRD", sleeve(px, lb), variant=f"Lookback {lb}d",
                      verdict="KANDIDAT" if lb == best else "Variante",
                      notes="Cross-Asset-TSMOM ETFs, monatlich, long/flat")
        except Exception as e:  # noqa: BLE001
            print(f"  lb={lb}: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    if not a.run:
        ap.print_help()
        sys.exit(1)
    run()
