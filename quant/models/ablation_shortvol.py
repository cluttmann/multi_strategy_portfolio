"""v3-Ablation Block A: FINRA Daily Short Volume (erster neuer Datenblock).

    python3 -m quant.models.ablation_shortvol --run

Protokoll (DATA_ROADMAP §5): identische purged Walk-Forward-Folds, Baseline
= XSR v2 (36 Features) vs. +Short-Volume-Block. Aufnahme-Kriterium ≥ +0,02
OOS-Sharpe (net@5bp, Tranche k=5). Short-Features (cross-sektional, variieren
innerhalb Datum):
  short_ratio      = short_volume / total_volume            (Level)
  z_short_ratio    = cross-sektionaler Z pro Datum
  z_short_ratio_5d = 5d-Änderung des Ratios, cross-sektional gezscored
  z_shortvol_spike = ratio vs eigener 21d-Schnitt (Symbol-Z)
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.config import STAGING_DIR
from quant.data.bq import query
from quant.models.train_ranker import (LGB_PARAMS, NUM_ROUNDS, V2_FEATURES,
                                       load_features, rank_label)
from quant.backtest.portfolio_sim import simulate_tranches

EMBARGO = 10
SHORT_FEATURES = ["z_short_ratio", "z_short_ratio_chg5", "z_shortvol_spike"]


def load_short_block() -> pd.DataFrame:
    print("Short-Volume-Block aus FINRA bauen ...")
    df = query("""
      WITH s AS (
        SELECT date, symbol,
          SAFE_DIVIDE(short_volume, NULLIF(total_volume,0)) AS short_ratio
        FROM `trading-436516.quant.finra_short_volume`
        WHERE total_volume > 0
      ),
      w AS (
        SELECT date, symbol, short_ratio,
          short_ratio - LAG(short_ratio,5) OVER (PARTITION BY symbol ORDER BY date)
            AS short_ratio_chg5,
          SAFE_DIVIDE(
            short_ratio - AVG(short_ratio) OVER (PARTITION BY symbol ORDER BY date
              ROWS BETWEEN 21 PRECEDING AND 1 PRECEDING),
            NULLIF(STDDEV(short_ratio) OVER (PARTITION BY symbol ORDER BY date
              ROWS BETWEEN 21 PRECEDING AND 1 PRECEDING),0)) AS shortvol_spike
        FROM s
      )
      SELECT date, symbol, short_ratio, short_ratio_chg5, shortvol_spike
      FROM w""")
    df["date"] = pd.to_datetime(df["date"])
    # cross-sektionale Z pro Datum
    for src, dst in [("short_ratio", "z_short_ratio"),
                     ("short_ratio_chg5", "z_short_ratio_chg5"),
                     ("shortvol_spike", "z_shortvol_spike")]:
        g = df.groupby("date")[src]
        df[dst] = ((df[src] - g.transform("mean"))
                   / g.transform("std").replace(0, np.nan)).astype("float32")
    print(f"Short-Block: {len(df):,} Zeilen, ab {df['date'].min().date()}")
    return df[["date", "symbol"] + SHORT_FEATURES]


def walk_forward(df: pd.DataFrame, feats: list) -> pd.DataFrame:
    import lightgbm as lgb
    df = df.copy()
    df["y"] = rank_label(df)
    tdays = pd.Series(sorted(df["date"].unique()))
    out = []
    for year in range(2019, 2027):  # FINRA-Abdeckung ab 2017 → Test ab 2019
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
    short = load_short_block()
    merged = base.merge(short, on=["date", "symbol"], how="left")
    # fehlende Short-Daten (vor 2017 / Lücken) = 0 (neutral nach Z)
    for f in SHORT_FEATURES:
        merged[f] = merged[f].fillna(0.0)
    # Volle Trainingshistorie (2003+); Short-Features vor 2017 = 0 (neutral).
    # Test-Fenster in walk_forward ist ohnehin 2019+ (FINRA-Abdeckung).
    print("\n=== Baseline (v2, 36 Features) — OOS 2019-2026 ===")
    pb = walk_forward(base, V2_FEATURES)
    sb, cb = sharpe(pb)
    print(f"Baseline: Sharpe {sb:.3f}  CAGR {cb:+.1%}")

    print("\n=== + Short-Volume-Block (39 Features) — OOS 2019-2026 ===")
    ps = walk_forward(merged, V2_FEATURES + SHORT_FEATURES)
    ss, cs = sharpe(ps)
    print(f"+Shortvol: Sharpe {ss:.3f}  CAGR {cs:+.1%}")

    delta = ss - sb
    print(f"\nΔ Sharpe = {delta:+.3f}  → "
          f"{'AUFNEHMEN (≥+0.02)' if delta >= 0.02 else 'VERWERFEN (<+0.02)'}")
    ps.to_parquet(f"{STAGING_DIR}/preds_v3_shortvol.parquet", index=False)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    run()
