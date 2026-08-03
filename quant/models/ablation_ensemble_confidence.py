"""v3-Ablation: Ensemble-Uneinigkeit als Meta-Signal (kein neuer Datenblock).

    python3 -m quant.models.ablation_ensemble_confidence --run

Anders als die bisherigen v3-Ablationen (Short-Volume, Insider, 13D) testet
dies kein externes Datum, sondern ob XSRs EIGENES Modell brauchbare
Information in seiner eigenen Uneinigkeit trägt (Meta-Labeling, Lopez de
Prado): pro Tag/Symbol werden N_MODELS LightGBM-Ranker mit unterschiedlichen
Seeds auf denselben Features trainiert; die Streuung ihrer Scores
(z_disagreement) misst, wie sicher sich das Modell-Ensemble selbst ist.

Vorhersagen:
  (a) Der Rang-IC (Score vs. fwd_ret_5d) ist im niedrigsten Uneinigkeits-
      Quintil am höchsten und fällt monoton mit steigender Uneinigkeit.
  (b) Ein auf die uneinigkeitsärmste Hälfte gefiltertes Buch (gleiche
      N_SIDE=75, gleiche Konstruktion) verbessert die Netto-Sharpe (k=5) um
      ≥ +0.02 ggü. dem ungefilterten Ensemble-Mean-Buch.
  (c) ρ(z_disagreement, z_log_adv) ist niedrig — sonst misst man nur
      "dünn gehandelte Namen sind unsicherer", kein echtes Meta-Signal.

Aufnahmekriterium identisch zu den anderen v3-Ablationen: ΔSharpe ≥ +0.02.
"""

import argparse
import sys

import lightgbm as lgb
import numpy as np
import pandas as pd

from quant.config import STAGING_DIR
from quant.backtest.portfolio_sim import simulate_tranches
from quant.models.train_ranker import (LGB_PARAMS, NUM_ROUNDS, V2_FEATURES,
                                       load_features, rank_label)

EMBARGO = 10
N_MODELS = 5
KEEP_FRAC = 0.5  # confidence-gefiltertes Buch behält die sichersten 50%
CACHE_PATH = f"{STAGING_DIR}/ensemble_confidence_preds.parquet"


def train_ensemble_walk_forward(df: pd.DataFrame, feats: list,
                                 start_year=2019, end_year=2026,
                                 n_models=N_MODELS) -> pd.DataFrame:
    df = df.copy()
    df["y"] = rank_label(df)
    tdays = pd.Series(sorted(df["date"].unique()))
    out = []
    for year in range(start_year, end_year + 1):
        ts = pd.Timestamp(f"{year}-01-01")
        pre = tdays[tdays < ts]
        if len(pre) < 250:
            continue
        cut = pre.iloc[-EMBARGO]
        tr = df[df["date"] < cut]
        te = df[(df["date"] >= ts) & (df["date"] <= f"{year}-12-31")]
        if te.empty or len(tr) < 50000:
            continue

        scores = np.zeros((n_models, len(te)), dtype="float32")
        for i in range(n_models):
            params = dict(LGB_PARAMS)
            params["bagging_seed"] = 1000 + i
            params["feature_fraction_seed"] = 2000 + i
            params["seed"] = 3000 + i
            m = lgb.train(params, lgb.Dataset(tr[feats], label=tr["y"]),
                          num_boost_round=NUM_ROUNDS)
            scores[i] = m.predict(te[feats]).astype("float32")

        o = te[["date", "symbol", "fwd_ret_1d", "fwd_ret_5d", "vol_63d",
                "adv63"]].copy()
        o["score"] = scores.mean(axis=0)
        o["disagreement"] = scores.std(axis=0)
        out.append(o)
        print(f"{year}: {len(te):,} rows, {n_models} models trained", flush=True)
    res = pd.concat(out, ignore_index=True)
    g = res.groupby("date")["disagreement"]
    res["z_disagreement"] = ((res["disagreement"] - g.transform("mean"))
                              / g.transform("std").replace(0, np.nan)
                              ).astype("float32")
    return res


def sharpe(preds):
    res = simulate_tranches(preds, k=5)
    r = res["net_ret"]
    return r.mean() / r.std() * np.sqrt(252), \
        (1 + r).cumprod().iloc[-1] ** (252 / len(r)) - 1


def prediction_a_ic_by_quintile(preds: pd.DataFrame) -> pd.Series:
    p = preds.dropna(subset=["z_disagreement", "score", "fwd_ret_5d"]).copy()
    p["disagree_q"] = p.groupby("date")["z_disagreement"].transform(
        lambda x: pd.qcut(x, 5, labels=False, duplicates="drop"))

    def ic_for_q(q):
        sub = p[p["disagree_q"] == q]
        return (sub.groupby("date")
                   .apply(lambda g: g["score"].corr(g["fwd_ret_5d"],
                                                     method="spearman"))
                   .mean())
    return pd.Series({q: ic_for_q(q) for q in sorted(p["disagree_q"].dropna().unique())})


def prediction_c_liquidity_corr(preds: pd.DataFrame) -> float:
    p = preds.dropna(subset=["z_disagreement", "adv63"]).copy()
    p["log_adv"] = np.log(p["adv63"].clip(lower=1.0))
    p["z_log_adv"] = p.groupby("date")["log_adv"].transform(
        lambda x: (x - x.mean()) / x.std() if x.std() > 0 else 0.0)
    return p["z_disagreement"].corr(p["z_log_adv"])


def confidence_filtered_book(preds: pd.DataFrame, keep_frac=KEEP_FRAC,
                              keep_highest=False) -> pd.DataFrame:
    """Pro Tag: nur ein uneinigkeits-Quantil bleibt im handelbaren Universum;
    simulate_tranches waehlt daraus wie gewohnt top/bottom N_SIDE.

    keep_highest=False (Vorhersage b, vorregistriert): behält die
    uneinigkeitsärmste Hälfte (Confidence-Filter).
    keep_highest=True (explorativer Folgetest nach dem invertierten
    Quintil-Befund, siehe kill_registry.yaml ENSEMBLE_UNEINIGKEIT_ALS_
    CONFIDENCE_FILTER): behält die uneinigkeitsREICHSTE Hälfte — Spiegelbild
    derselben Konstruktion, eigener Versuchszähler, gleiche OOS-Prädiktionen.
    """
    p = preds.dropna(subset=["z_disagreement"]).copy()
    pct = p.groupby("date")["z_disagreement"].rank(pct=True)
    keep = (pct >= 1 - keep_frac) if keep_highest else (pct <= keep_frac)
    return p[keep]


def run(from_cache=False):
    import os
    if from_cache and os.path.exists(CACHE_PATH):
        print(f"Lade gecachte Ensemble-Predictions aus {CACHE_PATH} ...")
        ens = pd.read_parquet(CACHE_PATH)
    else:
        base = load_features(v2=True)
        print("\n=== Ensemble-Training (5 Seeds, walk-forward 2019-2026) ===")
        ens = train_ensemble_walk_forward(base, V2_FEATURES)
        ens.to_parquet(CACHE_PATH, index=False)
        print(f"Ensemble-Predictions gecacht nach {CACHE_PATH}")

    print("\n=== Vorhersage (a): Rang-IC nach Uneinigkeits-Quintil ===")
    ic_by_q = prediction_a_ic_by_quintile(ens)
    for q, ic in ic_by_q.items():
        print(f"  Quintil {int(q)} (0=niedrigste Uneinigkeit): IC {ic:+.4f}")
    monotone = all(ic_by_q.iloc[i] >= ic_by_q.iloc[i + 1] - 0.01
                   for i in range(len(ic_by_q) - 1))
    print(f"  monoton fallend (mit Toleranz): {monotone}")

    print("\n=== Vorhersage (c): ρ(z_disagreement, z_log_adv) ===")
    rho_liq = prediction_c_liquidity_corr(ens)
    print(f"  ρ = {rho_liq:+.3f}")

    print("\n=== Baseline: Ensemble-Mean-Buch (ungefiltert) ===")
    sb, cb = sharpe(ens.rename(columns={"score": "score"}))
    print(f"Baseline: Sharpe {sb:.3f}  CAGR {cb:+.1%}")

    print(f"\n=== Vorhersage (b): Confidence-gefiltertes Buch (behalte {KEEP_FRAC:.0%}) ===")
    filtered = confidence_filtered_book(ens)
    sf, cf = sharpe(filtered)
    print(f"Gefiltert: Sharpe {sf:.3f}  CAGR {cf:+.1%}")

    delta = sf - sb
    print(f"\nΔ Sharpe (Confidence-Filter) = {delta:+.3f}  → "
          f"{'AUFNEHMEN (≥+0.02)' if delta >= 0.02 else 'VERWERFEN (<+0.02)'}")

    print(f"\n=== Explorativer Folgetest: Uneinigkeits-TILT (behalte {KEEP_FRAC:.0%} höchste) ===")
    tilted = confidence_filtered_book(ens, keep_highest=True)
    st, ct = sharpe(tilted)
    print(f"Uneinigkeits-Tilt: Sharpe {st:.3f}  CAGR {ct:+.1%}")
    delta_t = st - sb
    print(f"Δ Sharpe (Uneinigkeits-Tilt) = {delta_t:+.3f}  → "
          f"{'AUFNEHMEN (≥+0.02)' if delta_t >= 0.02 else 'VERWERFEN (<+0.02)'}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    p.add_argument("--from-cache", action="store_true",
                   help="Ensemble-Predictions aus vorherigem Lauf wiederverwenden")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    run(from_cache=args.from_cache)
