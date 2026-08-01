"""v3-Ablation Block D: SEC Schedule 13D Aktivisten-Filings (neuer Datenblock).

    python3 -m quant.models.ablation_act13d --run

Protokoll identisch zu ablation_shortvol.py/ablation_regime.py/
ablation_insider.py (DATA_ROADMAP §5): identische purged Walk-Forward-
Folds, Baseline = XSR v2 (36 Features) vs. +13D-Block. Aufnahme-Kriterium
≥ +0.02 OOS-Sharpe (net@5bp, Tranche k=5).

WARUM DIESER VERSUCH ANDERS IST ALS ACT13D SELBST: die eigenständige
Long-Strategie (Familie ACT13D, verworfen 2026-07 UND nach R9-Korrektur
2026-08-01 endgültig bestätigt verworfen) hatte einen ROBUSTEN, hoch
signifikanten NEGATIVEN Drift nach 13D-Filings (-2.56% bei 63 Tagen,
t=-5.57, sogar nach Größen-Dezil-Korrektur). Als Long-Only-Strategie
wertlos (falsches Vorzeichen ggü. der Hypothese) — aber ein Baummodell ist
das Vorzeichen egal, es kann eine ROBUSTE, informative Beziehung nutzen,
in welche Richtung auch immer sie zeigt. Anders als bei INSIDER_TRANSAKTIONEN
(nur 395 Namen Abdeckung, verworfen) hat 13D eine breite Abdeckung
(4.566 Symbole, 2007-2026).

FEATURES (cross-sektional, variieren innerhalb Datum):
  z_days_since_13d   = Tage seit dem letzten 13D/13D-A-Filing, gedeckelt
                       bei 252 (nie/lange her → alle gleich "weit weg")
  z_13d_count_252d   = Anzahl 13D-Filings in den letzten 252 Handelstagen
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
DAYS_CAP = 252
ACT13D_FEATURES = ["z_days_since_13d", "z_13d_count_252d"]


def load_act13d_block() -> pd.DataFrame:
    print("13D-Block aus SEC Schedule 13D bauen ...")
    raw = query("""
      SELECT date, symbol FROM `trading-436516.quant.sec_13d_filings`""")
    raw["date"] = pd.to_datetime(raw["date"])
    # Mehrere Filings am selben Tag für dasselbe Symbol (z.B. 13D + 13D/A
    # am selben Tag, oder zwei Aktivisten) zu einer Zeile — verhindert
    # doppelte Index-Labels beim Reindexing (dieselbe Klasse Bug wie in
    # ablation_insider.py, 2026-08-01 gefunden).
    raw = raw.drop_duplicates(["symbol", "date"])
    frames = []
    for sym, g in raw.groupby("symbol"):
        g = g.set_index("date").sort_index()
        full_idx = pd.date_range(g.index.min(), g.index.max(), freq="D")
        has_filing = pd.Series(1, index=g.index).reindex(full_idx).fillna(0.0)
        # Tage seit letztem Filing: Lauflänge seit dem letzten 1er, gedeckelt
        last_filing_day = pd.Series(full_idx, index=full_idx).where(
            has_filing == 1).ffill()
        days_since = (pd.Series(full_idx, index=full_idx) - last_filing_day
                     ).dt.days.fillna(DAYS_CAP).clip(upper=DAYS_CAP)
        count_252d = has_filing.rolling(DAYS_CAP, min_periods=1).sum()
        frames.append(pd.DataFrame({"date": full_idx, "symbol": sym,
                                    "days_since": days_since.values,
                                    "count_252d": count_252d.values}))
    df = pd.concat(frames, ignore_index=True)
    for src, dst in [("days_since", "z_days_since_13d"),
                     ("count_252d", "z_13d_count_252d")]:
        g = df.groupby("date")[src]
        df[dst] = ((df[src] - g.transform("mean"))
                   / g.transform("std").replace(0, np.nan)).astype("float32")
    print(f"13D-Block: {len(df):,} Zeilen (Tagesraster), "
          f"{df['symbol'].nunique()} Symbole, ab {df['date'].min().date()}")
    return df[["date", "symbol"] + ACT13D_FEATURES]


def walk_forward(df: pd.DataFrame, feats: list) -> pd.DataFrame:
    import lightgbm as lgb
    df = df.copy()
    df["y"] = rank_label(df)
    tdays = pd.Series(sorted(df["date"].unique()))
    out = []
    for year in range(2019, 2027):
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
    act = load_act13d_block()
    merged = base.merge(act, on=["date", "symbol"], how="left")
    # fehlende 13D-Historie (Symbol nie in sec_13d_filings) = "weit weg"/0,
    # nicht fehlender Wert — kein 13D-Filing ist ökonomisch ein echter
    # Zustand (kein Aktivist aktiv), keine Datenlücke.
    merged["z_days_since_13d"] = merged["z_days_since_13d"].fillna(
        merged["z_days_since_13d"].max())
    merged["z_13d_count_252d"] = merged["z_13d_count_252d"].fillna(0.0)

    print("\n=== Baseline (v2, 36 Features) — OOS 2019-2026 ===")
    pb = walk_forward(base, V2_FEATURES)
    sb, cb = sharpe(pb)
    print(f"Baseline: Sharpe {sb:.3f}  CAGR {cb:+.1%}")

    print("\n=== + 13D-Block (38 Features) — OOS 2019-2026 ===")
    pa = walk_forward(merged, V2_FEATURES + ACT13D_FEATURES)
    sa, ca = sharpe(pa)
    print(f"+13D: Sharpe {sa:.3f}  CAGR {ca:+.1%}")

    delta = sa - sb
    print(f"\nΔ Sharpe = {delta:+.3f}  → "
          f"{'AUFNEHMEN (≥+0.02)' if delta >= 0.02 else 'VERWERFEN (<+0.02)'}")

    from quant.config import STAGING_DIR
    pa.to_parquet(f"{STAGING_DIR}/preds_v3_act13d.parquet", index=False)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    run()
