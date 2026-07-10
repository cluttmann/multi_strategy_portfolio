"""Long-short portfolio simulation over walk-forward predictions.

    python3 -m quant.backtest.portfolio_sim --run-tag wf_v1

Construction: each day take the top/bottom N_SIDE names by model score within
the liquid universe, weight inverse-vol (capped), dollar-neutral. Positions
become effective at the next open; daily P&L uses fwd_ret_1d (open→open), so
timing matches what the live engine can actually do.

Costs: COST_BPS charged per side on turnover (|Δw| summed), default 5bps,
with a 10bps stress column. Turnover is controlled with a score-band rule:
a held name is only kicked out when it leaves the top/bottom BAND (hysteresis).
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd

from quant.config import STAGING_DIR

N_SIDE = 75           # names per side
BAND_MULT = 2.0       # keep holding until name falls out of top/bottom N*BAND
COST_BPS = 5.0
STRESS_BPS = 10.0
GROSS_LEVERAGE = 2.0  # 1x long + 1x short


def simulate(preds: pd.DataFrame, n_side=N_SIDE, band_mult=BAND_MULT,
             cost_bps=COST_BPS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (daily results, holdings snapshot of final day)."""
    preds = preds.sort_values(["date", "score"], ascending=[True, False])
    days = preds["date"].unique()

    prev_long: set = set()
    prev_short: set = set()
    prev_w: pd.Series = pd.Series(dtype="float64")
    rows = []

    for d, g in preds.groupby("date", sort=True):
        g = g.dropna(subset=["score", "fwd_ret_1d"])
        if len(g) < n_side * 4:
            continue
        g = g.sort_values("score", ascending=False).reset_index(drop=True)
        band = int(n_side * band_mult)

        top_new = set(g.head(n_side)["symbol"])
        top_band = set(g.head(band)["symbol"])
        bot_new = set(g.tail(n_side)["symbol"])
        bot_band = set(g.tail(band)["symbol"])

        # Hysteresis: keep incumbents while they stay inside the band.
        longs = (prev_long & top_band) | top_new
        shorts = (prev_short & bot_band) | bot_new
        longs -= shorts  # paranoia: a name can't be on both sides
        shorts -= longs

        gsym = g.set_index("symbol")
        # Inverse-vol weights, capped at 3x the equal weight.
        def weights(names, sign):
            vol = gsym.loc[list(names), "vol_63d"].clip(lower=0.10)
            w = (1.0 / vol)
            w = w / w.sum() * (GROSS_LEVERAGE / 2)
            w = w.clip(upper=3.0 / max(len(names), 1) * (GROSS_LEVERAGE / 2))
            w = w / w.sum() * (GROSS_LEVERAGE / 2)
            return sign * w

        w = pd.concat([weights(longs, +1.0), weights(shorts, -1.0)])

        # P&L: weights applied to next-open→next-next-open returns.
        ret = gsym["fwd_ret_1d"].reindex(w.index).fillna(0.0)
        gross_ret = float((w * ret).sum())

        # Turnover vs previous weights.
        union = w.index.union(prev_w.index)
        turnover = float((w.reindex(union).fillna(0.0)
                          - prev_w.reindex(union).fillna(0.0)).abs().sum())
        cost = turnover * cost_bps / 1e4
        stress_cost = turnover * STRESS_BPS / 1e4

        rows.append({
            "date": d,
            "gross_ret": gross_ret,
            "net_ret": gross_ret - cost,
            "net_ret_stress": gross_ret - stress_cost,
            "turnover": turnover,
            "n_long": len(longs),
            "n_short": len(shorts),
        })
        prev_long, prev_short, prev_w = longs, shorts, w

    res = pd.DataFrame(rows).set_index("date")
    final_holdings = pd.DataFrame({"weight": prev_w}).sort_values("weight")
    return res, final_holdings


def summarize(res: pd.DataFrame) -> str:
    def stats(col):
        r = res[col]
        ann = 252
        sharpe = r.mean() / r.std() * np.sqrt(ann) if r.std() > 0 else 0.0
        eq = (1 + r).cumprod()
        cagr = eq.iloc[-1] ** (ann / len(r)) - 1
        dd = (eq / eq.cummax() - 1).min()
        return sharpe, cagr, dd

    lines = [f"{'':16s}{'Sharpe':>8s}{'CAGR':>9s}{'MaxDD':>9s}"]
    for col, label in [("gross_ret", "gross"), ("net_ret", f"net@{COST_BPS:.0f}bp"),
                       ("net_ret_stress", f"net@{STRESS_BPS:.0f}bp")]:
        s, c, d = stats(col)
        lines.append(f"{label:16s}{s:8.2f}{c:9.1%}{d:9.1%}")
    lines.append(f"{'avg turnover':16s}{res['turnover'].mean():8.2f} /day "
                 f"(both sides, gross {GROSS_LEVERAGE}x)")

    yearly = res.groupby(res.index.year)["net_ret"].apply(
        lambda r: (1 + r).prod() - 1)
    pos = (yearly > 0).mean()
    lines.append(f"{'yearly net':16s}" + "  ".join(
        f"{y}:{v:+.0%}" for y, v in yearly.items()))
    lines.append(f"positive years: {pos:.0%}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run-tag", default="wf_v1")
    p.add_argument("--n-side", type=int, default=N_SIDE)
    p.add_argument("--cost-bps", type=float, default=COST_BPS)
    args = p.parse_args()

    path = os.path.join(STAGING_DIR, f"preds_{args.run_tag}.parquet")
    if not os.path.exists(path):
        print(f"missing {path} — run train_ranker first")
        sys.exit(1)
    preds = pd.read_parquet(path)
    res, holdings = simulate(preds, n_side=args.n_side, cost_bps=args.cost_bps)
    print(summarize(res))
    out = os.path.join(STAGING_DIR, f"sim_{args.run_tag}.parquet")
    res.to_parquet(out)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
