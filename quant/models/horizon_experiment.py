"""Horizont-Experiment — der #1-Befund der Deep Research.

    python3 -m quant.models.horizon_experiment --run

Testet, ob ein längerer Vorhersage-/Halte-Horizont den Netto-Ertrag hebt
(Blitz et al. 2023: Kurzhorizont-ML nettet ~0 nach Kosten, 1-6M restauriert
ihn über Turnover-Reduktion). Identische purged Walk-Forward-Folds, identische
Portfolio-Simulation; nur Trainingslabel-Horizont h ∈ {5,10,21} und passende
Tranchen-Länge k=h variieren. fwd_ret_21d wird frisch aus eod_bars berechnet
(open t+1 → open t+22), da nur bis 10d materialisiert.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.config import STAGING_DIR, T_EOD
from quant.data.bq import client, query
from quant.models.train_ranker import LGB_PARAMS, NUM_ROUNDS, V2_FEATURES
from quant.backtest.portfolio_sim import simulate_tranches

EMBARGO = {5: 10, 10: 15, 21: 26}


def load(h: int) -> pd.DataFrame:
    """Features (v2) + fwd_ret_1d für PnL + fwd_ret_h als Trainingslabel."""
    base_cols = ["date", "symbol"] + V2_FEATURES + ["fwd_ret_1d", "vol_63d", "adv63"]
    base_cols = list(dict.fromkeys(base_cols))
    if h in (5, 10):
        cols = base_cols + [f"fwd_ret_{h}d"]
        df = query(f"SELECT {', '.join(dict.fromkeys(cols))} FROM "
                   f"`trading-436516.quant.features_daily_v2` "
                   f"WHERE fwd_ret_{h}d IS NOT NULL")
        df = df.rename(columns={f"fwd_ret_{h}d": "label"})
    else:
        # fwd_ret_21d frisch: open(t+1) → open(t+22), execution-aligned
        lab = query(f"""
          WITH px AS (
            SELECT date, symbol,
              open * SAFE_DIVIDE(adjusted_close, close) AS ao
            FROM `{T_EOD}` WHERE close>0 AND adjusted_close>0)
          SELECT date, symbol,
            SAFE_DIVIDE(LEAD(ao,22) OVER w, LEAD(ao,1) OVER w)-1 AS label
          FROM px WINDOW w AS (PARTITION BY symbol ORDER BY date)""")
        feat = query(f"SELECT {', '.join(base_cols)} FROM "
                     f"`trading-436516.quant.features_daily_v2`")
        df = feat.merge(lab, on=["date", "symbol"], how="inner").dropna(
            subset=["label"])
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["label"].abs() < 1.0]
    return df.sort_values(["date", "symbol"]).reset_index(drop=True)


def walk_forward(df: pd.DataFrame, h: int) -> pd.DataFrame:
    import lightgbm as lgb
    df = df.copy()
    df["y"] = df.groupby("date")["label"].rank(pct=True)
    tdays = pd.Series(sorted(df["date"].unique()))
    emb = EMBARGO[h]
    out = []
    for year in range(2003, 2027):
        ts = pd.Timestamp(f"{year}-01-01")
        pre = tdays[tdays < ts]
        if len(pre) < 250:
            continue
        cut = pre.iloc[-emb]
        tr = df[df["date"] < cut]
        te = df[(df["date"] >= ts) & (df["date"] <= f"{year}-12-31")]
        if te.empty or len(tr) < 50000:
            continue
        m = lgb.train(LGB_PARAMS, lgb.Dataset(tr[V2_FEATURES], label=tr["y"]),
                      num_boost_round=NUM_ROUNDS)
        o = te[["date", "symbol", "fwd_ret_1d", "vol_63d", "adv63"]].copy()
        o["score"] = m.predict(te[V2_FEATURES]).astype("float32")
        # fwd_ret_5d wird von simulate_tranches als Artefakt-Guard erwartet
        o["fwd_ret_5d"] = o["fwd_ret_1d"]
        out.append(o)
    return pd.concat(out, ignore_index=True)


def run():
    print(f"{'h':>3s} {'k':>3s} {'IC':>8s} {'Sharpe@5bp':>11s} {'CAGR':>8s} "
          f"{'Sh@10bp':>8s} {'turnover':>9s}")
    for h in (5, 10, 21):
        df = load(h)
        preds = walk_forward(df, h)
        res = simulate_tranches(preds, k=h)
        r, rs = res["net_ret"], res["net_ret_stress"]
        sh = r.mean() / r.std() * np.sqrt(252)
        shs = rs.mean() / rs.std() * np.sqrt(252)
        cagr = (1 + r).cumprod().iloc[-1] ** (252 / len(r)) - 1
        preds.to_parquet(f"{STAGING_DIR}/preds_h{h}.parquet", index=False)
        print(f"{h:>3d} {h:>3d} {'':>8s} {sh:>11.2f} {cagr:>+8.1%} "
              f"{shs:>8.2f} {res['turnover'].mean():>9.2f}", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    run()
