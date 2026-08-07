"""v3-Ablation Block C: SEC Form 4 Insider-Transaktionen (neuer Datenblock).

    python3 -m quant.models.ablation_insider --run

Protokoll identisch zu ablation_shortvol.py/ablation_regime.py (DATA_ROADMAP
§5): identische purged Walk-Forward-Folds, Baseline = XSR v2 (36 Features)
vs. +Insider-Block. Aufnahme-Kriterium ≥ +0.02 OOS-Sharpe (net@5bp,
Tranche k=5).

MECHANISMUS (Lakonishok/Lee 1998, Seyhun 1998): Insider (Vorstand/Aufsichts-
rat) haben einen echten Informationsvorsprung über ihr eigenes Unternehmen.
Offene-Markt-Käufe (SEC-Form-4-Code P) sind das stärkere Signal als
Verkäufe (die auch aus Diversifikation/Liquiditätsbedarf kommen können,
Käufe praktisch nie). NETTO-Käufe über ein Quartal, nicht ein Einzeltag —
ein einzelnes Form 4 ist zu verrauscht.

Insider-Daten sind SPARSE (49k Zeilen, 395 Symbole, 2016-2026 — nicht jeder
Handelstag hat ein Filing). Ein rollierendes 63-Handelstage-Fenster
(≈1 Quartal) je Symbol macht daraus eine für jeden Tag definierte
Querschnitts-Größe, per pandas .rolling() auf reindiziertes Tagesraster
(nicht BQ ROWS BETWEEN — das würde bei Lücken über Kalendertage hinweg
falsch aggregieren, s. Kommentar in load_insider_block()).

FEATURES (cross-sektional, variieren innerhalb Datum):
  z_insider_net_63d     = 63-Tage-Netto-USD-Volumen (Käufe-Verkäufe), Z
  z_insider_buyers_63d  = 63-Tage-Netto-Insider-Zahl (n_buys-n_sells), Z
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.data.bq import query
from quant.models.train_ranker import (LGB_PARAMS, NUM_ROUNDS, V2_FEATURES,
                                       load_features, rank_label)
from quant.backtest.portfolio_sim import simulate_tranches

EMBARGO = 10
WINDOW = 63          # ≈1 Handelsquartal
INSIDER_FEATURES = ["z_insider_net_63d", "z_insider_buyers_63d"]


def load_insider_block() -> pd.DataFrame:
    print("Insider-Block aus SEC Form 4 bauen ...")
    raw = query("""
      SELECT date, symbol, net_value, n_buys, n_sells
      FROM `trading-436516.quant.insider_transactions`""")
    raw["date"] = pd.to_datetime(raw["date"])
    raw["net_insiders"] = raw["n_buys"] - raw["n_sells"]
    # Mehrere Form-4-Filings am selben Tag für dasselbe Symbol sind
    # SEPARATE Zeilen in der Rohtabelle (verschiedene Insider, dieselbe
    # Transaktion gemeldet) — erst zusammenfassen, sonst hat der Index pro
    # Symbol doppelte Daten und reindex() darunter bricht.
    raw = raw.groupby(["symbol", "date"], as_index=False)[
        ["net_value", "net_insiders"]].sum()
    # Vollständiges Tagesraster je Symbol (nicht nur Filing-Tage) — sonst
    # würde ein rollierendes Fenster über LÜCKEN hinweg falsch aggregieren
    # (ein Symbol mit 2 Filings in 10 Jahren bekäme sonst "63 Zeilen
    # zurück" = 63 FILINGS statt 63 TAGE, also Jahrzehnte Rückblick).
    frames = []
    for sym, g in raw.groupby("symbol"):
        g = g.set_index("date").sort_index()
        full_idx = pd.date_range(g.index.min(), g.index.max(), freq="D")
        g = g.reindex(full_idx).fillna(0.0)
        roll_val = g["net_value"].rolling(WINDOW, min_periods=1).sum()
        roll_n = g["net_insiders"].rolling(WINDOW, min_periods=1).sum()
        frames.append(pd.DataFrame({"date": full_idx, "symbol": sym,
                                    "insider_net": roll_val.values,
                                    "insider_buyers": roll_n.values}))
    df = pd.concat(frames, ignore_index=True)
    # cross-sektionale Z pro Datum
    for src, dst in [("insider_net", "z_insider_net_63d"),
                     ("insider_buyers", "z_insider_buyers_63d")]:
        g = df.groupby("date")[src]
        df[dst] = ((df[src] - g.transform("mean"))
                   / g.transform("std").replace(0, np.nan)).astype("float32")
    print(f"Insider-Block: {len(df):,} Zeilen (Tagesraster), "
          f"{df['symbol'].nunique()} Symbole, ab {df['date'].min().date()}")
    return df[["date", "symbol"] + INSIDER_FEATURES]


def walk_forward(df: pd.DataFrame, feats: list) -> pd.DataFrame:
    import lightgbm as lgb
    df = df.copy()
    df["y"] = rank_label(df)
    tdays = pd.Series(sorted(df["date"].unique()))
    out = []
    for year in range(2019, 2027):  # gleiches Testfenster wie ablation_shortvol
        ts = pd.Timestamp(f"{year}-01-01")
        pre = tdays[tdays < ts]
        if len(pre) < 250:
            continue
        cut = pre.iloc[-EMBARGO]
        tr = df[df["date"] < cut]
        te = df[(df["date"] >= ts) & (df["date"] <= f"{year}-12-31")]
        if te.empty or len(tr) < 50000:
            continue
        m = lgb.train(LGB_PARAMS, lgb.Dataset(tr[feats], label=tr["y"]),
                      num_boost_round=NUM_ROUNDS)
        o = te[["date", "symbol", "fwd_ret_1d", "fwd_ret_5d", "vol_63d",
                "adv63"]].copy()
        o["score"] = m.predict(te[feats]).astype("float32")
        out.append(o)
    return pd.concat(out, ignore_index=True)


def sharpe(preds):
    res = simulate_tranches(preds, k=5)
    r = res["net_ret"]
    return r.mean() / r.std() * np.sqrt(252), \
        (1 + r).cumprod().iloc[-1] ** (252 / len(r)) - 1


def run():
    base = load_features(v2=True)
    insider = load_insider_block()
    merged = base.merge(insider, on=["date", "symbol"], how="left")
    # fehlende Insider-Daten (Symbol nie im 395er-Backfill / vor 2016) = 0
    # (neutral nach Z) — kein Insider-Filing ist ökonomisch "kein Signal",
    # nicht "fehlender Wert".
    for f in INSIDER_FEATURES:
        merged[f] = merged[f].fillna(0.0)

    print("\n=== Baseline (v2, 36 Features) — OOS 2019-2026 ===")
    pb = walk_forward(base, V2_FEATURES)
    sb, cb = sharpe(pb)
    print(f"Baseline: Sharpe {sb:.3f}  CAGR {cb:+.1%}")

    print("\n=== + Insider-Block (38 Features) — OOS 2019-2026 ===")
    pi = walk_forward(merged, V2_FEATURES + INSIDER_FEATURES)
    si, ci = sharpe(pi)
    print(f"+Insider: Sharpe {si:.3f}  CAGR {ci:+.1%}")

    delta = si - sb
    print(f"\nΔ Sharpe = {delta:+.3f}  → "
          f"{'AUFNEHMEN (≥+0.02)' if delta >= 0.02 else 'VERWERFEN (<+0.02)'}")

    from quant.config import STAGING_DIR
    pi.to_parquet(f"{STAGING_DIR}/preds_v3_insider.parquet", index=False)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    run()
