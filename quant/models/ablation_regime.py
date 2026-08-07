"""v3-Ablation Block B: Makro-Regime × Name-Sensitivität (Interaktionen).

    python3 -m quant.models.ablation_regime --run

Reine markt-weite Regime-Serien (VIX-Termstruktur, HY-OAS, NFCI) sind pro
Datum für alle Namen gleich → für einen CROSS-SECTIONAL-Ranker wertlos.
Sie wirken nur als INTERAKTIONEN mit der Namens-Sensitivität:
  beta × vix_slope, beta × hy_oas_z, vol × vix_level, beta × nfci ...
So variiert das Feature innerhalb eines Datums (über beta/vol) UND über Zeit
(über das Regime). Protokoll: ≥ +0,02 OOS-Sharpe (net@5bp, k=5), sonst raus.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.config import STAGING_DIR
from quant.data.bq import query
from quant.models.train_ranker import (LGB_PARAMS, NUM_ROUNDS, V2_FEATURES,
                                       rank_label)
from quant.models.ablation_shortvol import load_features, walk_forward, sharpe

REGIME_FEATURES = ["ix_beta_vixslope", "ix_beta_hyoas", "ix_vol_vixlevel",
                   "ix_beta_nfci", "ix_vol_hyoas_chg"]


def load_regime() -> pd.DataFrame:
    """Markt-weite Regime-Serien aus fred_series, forward-filled je Tag."""
    df = query("""
      SELECT date, series, value FROM `trading-436516.quant.fred_series`
      WHERE series IN ('VIXCLS','VIX3MCLS','BAMLH0A0HYM2','NFCI')""")
    df["date"] = pd.to_datetime(df["date"])
    w = df.pivot(index="date", columns="series", values="value").sort_index()
    w = w.reindex(pd.date_range(w.index.min(), w.index.max())).ffill()
    # VIX3M in FRED lückenhaft/leer → aus OVX (Öl-VIX) als 2. Vol-Serie oder
    # Slope neutralisieren, wenn keine Termstruktur verfügbar
    if "VIX3MCLS" not in w.columns or w["VIX3MCLS"].notna().sum() < 100:
        w["VIX3MCLS"] = w["VIXCLS"]  # Slope = 0, Feature trägt dann nichts
    else:
        w["VIX3MCLS"] = w["VIX3MCLS"].fillna(w["VIXCLS"])
    reg = pd.DataFrame(index=w.index)
    reg["vix_slope"] = (w["VIX3MCLS"] / w["VIXCLS"] - 1)          # Contango>0
    reg["vix_level"] = (w["VIXCLS"] - w["VIXCLS"].rolling(252).mean()) \
        / w["VIXCLS"].rolling(252).std()
    reg["hy_oas"] = (w["BAMLH0A0HYM2"] - w["BAMLH0A0HYM2"].rolling(252).mean()) \
        / w["BAMLH0A0HYM2"].rolling(252).std()
    reg["hy_oas_chg"] = w["BAMLH0A0HYM2"].diff(5)
    reg["nfci"] = w["NFCI"]
    return reg


def build(base: pd.DataFrame) -> pd.DataFrame:
    reg = load_regime()
    b = base.copy()
    b = b.merge(reg, left_on="date", right_index=True, how="left")
    for c in ["vix_slope", "vix_level", "hy_oas", "hy_oas_chg", "nfci"]:
        b[c] = b[c].fillna(0.0)
    # Interaktionen: Name-Sensitivität (beta/vol) × Regime
    b["ix_beta_vixslope"] = (b["beta_63d"] * b["vix_slope"]).astype("float32")
    b["ix_beta_hyoas"] = (b["beta_63d"] * b["hy_oas"]).astype("float32")
    b["ix_vol_vixlevel"] = (b["vol_21d"] * b["vix_level"]).astype("float32")
    b["ix_beta_nfci"] = (b["beta_63d"] * b["nfci"]).astype("float32")
    b["ix_vol_hyoas_chg"] = (b["vol_21d"] * b["hy_oas_chg"]).astype("float32")
    return b


def run():
    base = load_features(v2=True)
    merged = build(base)

    print("\n=== Baseline (v2, 36) — OOS 2019-2026 ===")
    pb = walk_forward(base, V2_FEATURES)
    sb, cb = sharpe(pb)
    print(f"Baseline: Sharpe {sb:.3f}  CAGR {cb:+.1%}")

    print("\n=== + Regime-Interaktionen (41) — OOS 2019-2026 ===")
    ps = walk_forward(merged, V2_FEATURES + REGIME_FEATURES)
    ss, cs = sharpe(ps)
    print(f"+Regime: Sharpe {ss:.3f}  CAGR {cs:+.1%}")

    d = ss - sb
    print(f"\nΔ Sharpe = {d:+.3f}  → "
          f"{'AUFNEHMEN (≥+0.02)' if d >= 0.02 else 'VERWERFEN (<+0.02)'}")
    ps.to_parquet(f"{STAGING_DIR}/preds_v3_regime.parquet", index=False)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    run()
