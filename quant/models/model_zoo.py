"""Model zoo — is LightGBM leaving alpha on the table?

    python3 -m quant.models.model_zoo --run

Head-to-head on IDENTICAL purged walk-forward folds (test 2019–2026):
  GBM   LightGBM (production reference params)
  RIDGE ridge regression on the same features (the robust linear baseline)
  MLP   torch MLP (in→128→32→1) on Apple MPS, 2 epochs
  ENS   equal-weight of per-day rank-transformed scores of all three

Metrics: mean daily rank-IC per model, then the exact production tranche
portfolio simulation per model (net@5bp). The honest question is whether any
alternative or the ensemble beats GBM by ≥0.05 Sharpe on identical data.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.config import STAGING_DIR
from quant.models.train_ranker import (LGB_PARAMS, NUM_ROUNDS, V2_FEATURES,
                                       load_features, rank_label)

TEST_YEARS = list(range(2019, 2027))
EMBARGO_DAYS = 10


def fit_gbm(Xtr, ytr, Xte):
    import lightgbm as lgb
    m = lgb.train(LGB_PARAMS, lgb.Dataset(Xtr, label=ytr),
                  num_boost_round=NUM_ROUNDS)
    return m.predict(Xte)


def _mat(X) -> np.ndarray:
    return np.nan_to_num(
        X.to_numpy(dtype="float64", na_value=np.nan), nan=0.0
    ).astype("float32")


def fit_ridge(Xtr, ytr, Xte):
    from sklearn.linear_model import Ridge
    tr, te = _mat(Xtr), _mat(Xte)
    if len(tr) > 2_000_000:
        idx = np.random.default_rng(0).choice(len(tr), 2_000_000, replace=False)
        tr, ytr = tr[idx], ytr.iloc[idx]
    m = Ridge(alpha=10.0)
    m.fit(tr, ytr)
    return m.predict(te)


def fit_mlp(Xtr, ytr, Xte):
    import torch
    import torch.nn as nn
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    tr = torch.tensor(_mat(Xtr))
    te = torch.tensor(_mat(Xte))
    y = torch.tensor(ytr.to_numpy(dtype="float32")).unsqueeze(1)
    # standardize on train stats (many features are already z-scores)
    mu, sd = tr.mean(0, keepdim=True), tr.std(0, keepdim=True).clamp(min=1e-6)
    tr, te = (tr - mu) / sd, (te - mu) / sd
    net = nn.Sequential(nn.Linear(tr.shape[1], 128), nn.ReLU(), nn.Dropout(0.1),
                        nn.Linear(128, 32), nn.ReLU(), nn.Linear(32, 1)).to(dev)
    opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-5)
    lossf = nn.MSELoss()
    bs = 65536
    for epoch in range(2):
        perm = torch.randperm(len(tr))
        for i in range(0, len(tr), bs):
            idx = perm[i:i + bs]
            xb, yb = tr[idx].to(dev), y[idx].to(dev)
            opt.zero_grad()
            loss = lossf(net(xb), yb)
            loss.backward()
            opt.step()
    net.eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(te), bs):
            out.append(net(te[i:i + bs].to(dev)).cpu().numpy())
    return np.concatenate(out).ravel()


def run():
    df = load_features(v2=True)
    feats = V2_FEATURES
    df["y"] = rank_label(df)
    trading_days = pd.Series(sorted(df["date"].unique()))

    per_model_preds = {m: [] for m in ["GBM", "RIDGE", "MLP"]}
    print(f"{'year':>5s} {'GBM':>8s} {'RIDGE':>8s} {'MLP':>8s} {'ENS':>8s}")
    for year in TEST_YEARS:
        test_start = pd.Timestamp(f"{year}-01-01")
        pre = trading_days[trading_days < test_start]
        purge_cutoff = pre.iloc[-EMBARGO_DAYS]
        train = df[df["date"] < purge_cutoff]
        test = df[(df["date"] >= test_start) & (df["date"] <= f"{year}-12-31")]
        if test.empty:
            continue
        Xtr, ytr, Xte = train[feats], train["y"], test[feats]
        scores = {}
        scores["GBM"] = fit_gbm(Xtr, ytr, Xte)
        scores["RIDGE"] = fit_ridge(Xtr, ytr, Xte)
        scores["MLP"] = fit_mlp(Xtr, ytr, Xte)

        base = test[["date", "symbol", "fwd_ret_5d", "fwd_ret_1d",
                     "vol_63d", "adv63"]].copy()
        ics = {}
        ranked = {}
        for m, s in scores.items():
            out = base.copy()
            out["score"] = s
            per_model_preds[m].append(out)
            ranked[m] = out.groupby("date")["score"].rank(pct=True)
            ics[m] = (out.assign(r=s).groupby("date")
                      .apply(lambda g: g["score"].corr(g["fwd_ret_5d"],
                                                       method="spearman"))
                      .mean())
        ens_score = sum(ranked.values()) / 3
        out = base.copy()
        out["score"] = ens_score.values
        per_model_preds.setdefault("ENS", []).append(out)
        ic_ens = (out.groupby("date")
                  .apply(lambda g: g["score"].corr(g["fwd_ret_5d"],
                                                   method="spearman")).mean())
        print(f"{year:>5d} {ics['GBM']:>+8.4f} {ics['RIDGE']:>+8.4f} "
              f"{ics['MLP']:>+8.4f} {ic_ens:>+8.4f}", flush=True)

    # tranche portfolio per model
    from quant.backtest.portfolio_sim import simulate_tranches, summarize
    print("\n=== production tranche simulation per model (2019-2026 OOS) ===")
    for m, chunks in per_model_preds.items():
        preds = pd.concat(chunks, ignore_index=True)
        res = simulate_tranches(preds)
        r = res["net_ret"]
        sh = r.mean() / r.std() * np.sqrt(252)
        eq = (1 + r).cumprod()
        cagr = eq.iloc[-1] ** (252 / len(r)) - 1
        print(f"{m:6s} net@5bp Sharpe={sh:5.2f}  CAGR={cagr:+7.1%}")
        preds.to_parquet(f"{STAGING_DIR}/preds_zoo_{m}.parquet", index=False)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    run()
