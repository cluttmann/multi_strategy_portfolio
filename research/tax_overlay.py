"""tax_overlay.py — German tax drag overlay for the mega backtest.

Computes after-tax daily return series for a strategy whose intended weights
are given as a target_weights DataFrame. Uses per-rebalance realized-gain
accounting (proportional average-cost basis per asset), applies
Teilfreistellung from tax/config.py SYMBOL_TFS_RATE, adds Vorabpauschale on
Dec 31, carries forward losses (Verlustvortrag), and deducts Abgeltungsteuer
+ Soli + Kirchensteuer from NAV.

Constants are imported from tax/config.py so this module and the live tax
engine in tax/run_tax_engine.py cannot drift apart.

Simplifications vs the live engine:
- Average-cost basis per asset (not FIFO lots). The error is small because
  positions are continuously rebalanced; per-share lot mechanics matter most
  for sparse one-off trades, not for the rebalance-heavy strategies here.
- Tax is paid by proportionally shrinking positions (and their cost basis)
  at year-end — this is equivalent to a forced sale, but the second-order
  "tax on the forced sale" is not recursively realized (impact < 1% relative).
- Distributions are assumed = 0 for Vorabpauschale (EODHD adjusted_close
  already reinvests dividends, so we cannot separate distribution income).
- US dividend withholding tax is NOT modelled — by treaty, the 15% W-8BEN
  withholding is creditable against Abgeltungsteuer up to the German tax due,
  so on net it's roughly a wash for taxable accounts. The live engine
  ([tax/tax_calc.py:322]) does model it; the backtest skips it for simplicity.
"""

from __future__ import annotations
import os
import sys
from decimal import Decimal
from typing import Optional

import numpy as np
import pandas as pd

# Make tax/ importable when this file is loaded from research/
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tax.config import (
    SYMBOL_TFS_RATE, DEFAULT_TFS_RATE, BASISZINS,
    ABGELTUNGSTEUER_RATE, SOLI_RATE, KIRCHENSTEUER_RATE, SPARERPAUSCHBETRAG,
)


# ─── Tax-rate constants (float, for fast numpy math) ────────────────────

EFFECTIVE_TAX_RATE = float(
    ABGELTUNGSTEUER_RATE * (Decimal(1) + SOLI_RATE + KIRCHENSTEUER_RATE)
)
# Berlin non-church-member default: 0.25 × 1.055 = 0.26375

SPARERPAUSCHBETRAG_F = float(SPARERPAUSCHBETRAG)
# 0.0 per user setting (allowance assumed consumed at another broker)

_LATEST_BASISZINS_YEAR = max(BASISZINS.keys()) if BASISZINS else 2025
_LATEST_BASISZINS_VALUE = float(BASISZINS[_LATEST_BASISZINS_YEAR])


def tfs_rate(symbol: str) -> float:
    return float(SYMBOL_TFS_RATE.get(symbol, DEFAULT_TFS_RATE))


def basiszins(year: int) -> float:
    """Bundesbank Basiszins for the given year (used for Vorabpauschale).
    For years not yet in tax/config.py BASISZINS, falls back to the latest
    published value — keeps backtest comparable as time moves forward."""
    val = BASISZINS.get(year)
    return float(val) if val is not None else _LATEST_BASISZINS_VALUE


# ─── Main simulator ─────────────────────────────────────────────────────

def simulate_after_tax(
    target_weights: pd.DataFrame,
    asset_returns: pd.DataFrame,
    starting_nav: float = 1.0,
) -> tuple[pd.Series, pd.DataFrame]:
    """Walk daily NAV, applying tax on each rebalance + year-end Vorabpauschale.

    Args:
        target_weights: index=date, columns=ticker, target weights per row.
            Rows can be sparse (only on rebalance dates) — the function
            forward-fills between rows. Each row should ideally sum to 1.0;
            any "missing" weight is implicitly held as zero-return cash.
        asset_returns: index=date (DatetimeIndex), columns=tickers. Daily total
            returns. Must cover all tickers in target_weights.
        starting_nav: starting portfolio value (any positive number).

    Returns:
        after_tax_daily_returns: pd.Series indexed by date
        tax_log: pd.DataFrame indexed by year with columns
            [realized_gains_tfs, realized_losses_tfs, vorabpauschale,
             carryforward_used, taxable, tax_paid, drag_pp, nav_end]
    """
    if target_weights is None or target_weights.empty:
        return pd.Series(dtype=float), pd.DataFrame()

    tw = target_weights.fillna(0.0).copy()
    # Walk from the strategy's first weight date through the end of asset_returns
    # (forward-fill the last target weight if the strategy stops emitting rebalances)
    common_idx = asset_returns.index[asset_returns.index >= tw.index[0]]
    if len(common_idx) < 2:
        return pd.Series(dtype=float), pd.DataFrame()

    # Original rebalance dates — strategy explicitly traded on these.
    # Snap each original date to the nearest forward bd in common_idx.
    rebal_set = set()
    for d in tw.index:
        idx_pos = common_idx.searchsorted(d, side="left")
        if idx_pos < len(common_idx):
            rebal_set.add(common_idx[idx_pos])

    # Forward-fill targets across all daily dates so drift-day return is consistent
    tw_daily = tw.reindex(common_idx, method="ffill").fillna(0.0)

    # Keep only columns that exist in asset_returns
    cols = [c for c in tw_daily.columns if c in asset_returns.columns]
    if not cols:
        return pd.Series(dtype=float), pd.DataFrame()
    tw_daily = tw_daily[cols]
    rets = asset_returns[cols].reindex(common_idx).fillna(0.0)

    # State
    pos = {a: 0.0 for a in cols}      # current market value per asset
    cost = {a: 0.0 for a in cols}     # average-cost basis per asset
    cash = 0.0                         # zero-return cash bucket (no tax events)
    carryforward = 0.0                 # ≤ 0; absorbs future gains
    year_gains = 0.0                   # TFS-adjusted positive bucket
    year_losses = 0.0                  # TFS-adjusted negative bucket
    year_buy_volume = 0.0              # gross EUR-equiv buy volume this year
    after_tax_nav = np.empty(len(common_idx), dtype=float)
    log_rows: list[dict] = []

    # Day 0: initial buy at target weights
    nav = starting_nav
    cash = starting_nav
    first_target = tw_daily.iloc[0]
    for a in cols:
        v = nav * float(first_target[a])
        pos[a] = v
        cost[a] = v
        cash -= v
    after_tax_nav[0] = sum(pos.values()) + cash
    prev_target_vec = first_target.values.astype(float)
    nav_year_start = after_tax_nav[0]
    pos_year_start = dict(pos)

    rets_values = rets.values  # ndarray for speed
    tw_values = tw_daily.values

    for i in range(1, len(common_idx)):
        date = common_idx[i]

        # 1. Drift positions by today's per-asset return (cash earns 0%)
        for j, a in enumerate(cols):
            pos[a] *= (1.0 + float(rets_values[i, j]))
        nav = sum(pos.values()) + cash

        # 2. Rebalance if today is an explicit rebal date OR target values
        # changed since yesterday. The former covers strategies with static
        # targets that still rebalance back to fix drift (HFEA/F4 quarterly).
        today_target_vec = tw_values[i].astype(float)
        target_changed = not np.allclose(today_target_vec, prev_target_vec, atol=1e-12)
        is_rebal_day = date in rebal_set
        if target_changed or is_rebal_day:
            for j, a in enumerate(cols):
                target_val = nav * today_target_vec[j]
                cur_val = pos[a]
                if target_val < cur_val - 1e-12:
                    # SELL: realize proportional gain/loss; proceeds → cash
                    sold = cur_val - target_val
                    sell_frac = sold / cur_val if cur_val > 0 else 0.0
                    sold_basis = cost[a] * sell_frac
                    realized = sold - sold_basis
                    taxable = realized * (1.0 - tfs_rate(a))
                    if taxable >= 0:
                        year_gains += taxable
                    else:
                        year_losses += taxable
                    pos[a] = target_val
                    cost[a] -= sold_basis
                    cash += sold
                elif target_val > cur_val + 1e-12:
                    # BUY: spend cash; extend cost basis; no tax event
                    bought = target_val - cur_val
                    cost[a] += bought
                    pos[a] = target_val
                    cash -= bought
                    year_buy_volume += bought
            prev_target_vec = today_target_vec

        # 3. Year-end Vorabpauschale + tax deduction
        is_last_of_year = (i == len(common_idx) - 1) or (
            common_idx[i + 1].year != date.year
        )
        if is_last_of_year:
            year = int(date.year)
            bz = basiszins(year)
            vorab_total = 0.0
            for a in cols:
                start_val = pos_year_start.get(a, 0.0)
                end_val = pos[a]
                if start_val <= 0:
                    continue
                vp_raw = start_val * bz * 0.7
                actual_gain = end_val - start_val
                if actual_gain <= 0:
                    continue
                vp_capped = min(vp_raw, actual_gain)
                vorab_total += vp_capped * (1.0 - tfs_rate(a))

            year_income = year_gains + year_losses + vorab_total
            net_after_carry = year_income + carryforward
            if net_after_carry > 0:
                taxable_after_freibetrag = max(
                    0.0, net_after_carry - SPARERPAUSCHBETRAG_F
                )
                tax = taxable_after_freibetrag * EFFECTIVE_TAX_RATE
                used_carry = -carryforward if carryforward < 0 else 0.0
                new_carry = 0.0
            else:
                tax = 0.0
                taxable_after_freibetrag = 0.0
                used_carry = 0.0
                new_carry = net_after_carry

            # Apply tax: pay from cash first; if insufficient, shrink positions
            # proportionally. Positions and their cost basis are scaled together
            # so the unrealized-gain ratio is preserved (small tax-on-tax
            # second-order effect ignored — < 1% impact).
            if tax > 0:
                if cash >= tax:
                    cash -= tax
                else:
                    remaining = tax - max(0.0, cash)
                    cash = 0.0
                    pos_total = sum(pos.values())
                    if pos_total > 0:
                        scale = max(0.0, (pos_total - remaining) / pos_total)
                        for a in cols:
                            pos[a] *= scale
                            cost[a] *= scale
                nav = sum(pos.values()) + cash

            log_rows.append({
                "year": year,
                "realized_gains_tfs": year_gains,
                "realized_losses_tfs": year_losses,
                "vorabpauschale": vorab_total,
                "carryforward_used": used_carry,
                "taxable": taxable_after_freibetrag,
                "tax_paid": tax,
                "drag_pp": (tax / nav_year_start * 100.0) if nav_year_start > 0 else 0.0,
                "buy_volume": year_buy_volume,
                "turnover": (year_buy_volume / nav_year_start) if nav_year_start > 0 else 0.0,
                "nav_end": nav,
            })

            carryforward = new_carry
            year_gains = 0.0
            year_losses = 0.0
            year_buy_volume = 0.0
            nav_year_start = nav
            pos_year_start = dict(pos)

        after_tax_nav[i] = sum(pos.values()) + cash

    nav_series = pd.Series(after_tax_nav, index=common_idx)
    after_tax_rets = nav_series.pct_change().fillna(0.0)
    if log_rows:
        tax_log = pd.DataFrame(log_rows).set_index("year")
    else:
        tax_log = pd.DataFrame(
            columns=[
                "realized_gains_tfs", "realized_losses_tfs", "vorabpauschale",
                "carryforward_used", "taxable", "tax_paid", "drag_pp",
                "buy_volume", "turnover", "nav_end",
            ]
        )
    return after_tax_rets, tax_log


# ─── Helpers for the report ─────────────────────────────────────────────

def weighted_tfs(weights: dict) -> float:
    """Allocation-weighted TFS rate across a weights dict."""
    total = sum(abs(v) for v in weights.values())
    if total <= 0:
        return 0.0
    return sum(tfs_rate(a) * abs(v) for a, v in weights.items()) / total


def annualized_turnover(target_weights: pd.DataFrame) -> float:
    """One-side annual turnover (0..∞ where 1.0 = the whole portfolio
    rotates once per year on average). Initial buy is excluded so the
    metric reflects steady-state behaviour."""
    if target_weights is None or target_weights.empty or len(target_weights) < 2:
        return 0.0
    tw = target_weights.fillna(0.0)
    diffs = tw.diff().abs().sum(axis=1)
    diffs.iloc[0] = 0.0
    steady = float(diffs.sum()) * 0.5
    years = (tw.index[-1] - tw.index[0]).days / 365.25
    return steady / years if years > 0 else 0.0


def time_weighted_tfs(target_weights: pd.DataFrame) -> float:
    """Time-weighted TFS rate across the strategy's life."""
    if target_weights is None or target_weights.empty:
        return 0.0
    tw = target_weights.fillna(0.0)
    tfs_per_asset = {a: tfs_rate(a) for a in tw.columns}
    weighted = tw.copy()
    for a in tw.columns:
        weighted[a] = tw[a] * tfs_per_asset[a]
    daily_tfs = weighted.sum(axis=1) / tw.sum(axis=1).replace(0, np.nan)
    return float(daily_tfs.mean()) if len(daily_tfs.dropna()) > 0 else 0.0


def compute_after_tax_metrics(
    after_tax_rets: pd.Series, gross_rets: pd.Series, rf: float = 0.02,
) -> dict:
    """After-tax CAGR/Sharpe/MaxDD + tax drag in pp/yr + tax-cost ratio."""
    if after_tax_rets is None or gross_rets is None:
        return {}
    if len(after_tax_rets) < 2 or len(gross_rets) < 2:
        return {}
    common = after_tax_rets.index.intersection(gross_rets.index)
    a = after_tax_rets.reindex(common).dropna()
    g = gross_rets.reindex(common).dropna()
    if len(a) < 2 or len(g) < 2:
        return {}
    ann = 252
    years = len(a) / ann
    total_a = float((1 + a).prod())
    total_g = float((1 + g).prod())
    cagr_a = total_a ** (1 / years) - 1 if total_a > 0 else -1.0
    cagr_g = total_g ** (1 / years) - 1 if total_g > 0 else -1.0
    drag_pp = (cagr_g - cagr_a) * 100.0
    vol_a = float(a.std() * np.sqrt(ann))
    sharpe_a = (cagr_a - rf) / vol_a if vol_a > 0 else float("nan")
    cum_a = (1 + a).cumprod()
    dd_a = float(((cum_a - cum_a.cummax()) / cum_a.cummax()).min())
    tcr = (drag_pp / (cagr_g * 100.0)) if cagr_g > 0 else float("nan")
    return {
        "After-Tax CAGR": cagr_a,
        "After-Tax Sharpe": sharpe_a,
        "After-Tax MaxDD": dd_a,
        "Tax Drag (pp)": drag_pp,
        "Tax-Cost Ratio": tcr,
    }


# ─── Smoke tests ────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"EFFECTIVE_TAX_RATE = {EFFECTIVE_TAX_RATE:.5f}")
    print(f"latest BASISZINS   = {_LATEST_BASISZINS_VALUE:.4f} ({_LATEST_BASISZINS_YEAR})")

    # Test 1: zero-turnover buy-and-hold SPY → tax drag only from Vorabpauschale
    idx = pd.bdate_range("2020-01-01", "2023-12-29")
    rets_spy = pd.DataFrame({"SPY": [0.0004] * len(idx)}, index=idx)  # ~10%/yr
    tw_spy = pd.DataFrame({"SPY": [1.0]}, index=[idx[0]])
    aft_spy, log_spy = simulate_after_tax(tw_spy, rets_spy)
    cagr_g = (1 + rets_spy["SPY"]).prod() ** (252 / len(idx)) - 1
    cagr_a = (1 + aft_spy).prod() ** (252 / len(aft_spy)) - 1
    print(f"\nTest 1 — buy-and-hold SPY (4y, ~10% gross/yr):")
    print(f"  Gross CAGR     = {cagr_g*100:.2f}%")
    print(f"  After-tax CAGR = {cagr_a*100:.2f}%")
    print(f"  Drag           = {(cagr_g-cagr_a)*100:.2f}pp/yr")
    print(f"  Tax log:\n{log_spy.to_string()}")

    # Test 2: full annual rotation between SPY and BIL → tax on every Dec/Jan flip
    idx2 = pd.bdate_range("2020-01-01", "2023-12-29")
    rets2 = pd.DataFrame({"SPY": [0.0004]*len(idx2), "BIL": [0.0001]*len(idx2)}, index=idx2)
    rebal_dates = [pd.Timestamp(f"{y}-01-02") for y in (2020, 2021, 2022, 2023)] \
                 + [pd.Timestamp(f"{y}-07-01") for y in (2020, 2021, 2022, 2023)]
    rebal_dates = sorted(rebal_dates)
    tw2 = pd.DataFrame(
        {"SPY": [1.0, 0.0]*4, "BIL": [0.0, 1.0]*4},
        index=rebal_dates,
    )
    aft2, log2 = simulate_after_tax(tw2, rets2)
    cagr_g2 = (1 + rets2["SPY"]).prod() ** (252 / len(idx2)) - 1
    cagr_a2 = (1 + aft2).prod() ** (252 / len(aft2)) - 1
    print(f"\nTest 2 — semi-annual SPY ↔ BIL rotation:")
    print(f"  Gross CAGR (SPY) = {cagr_g2*100:.2f}%")
    print(f"  After-tax CAGR   = {cagr_a2*100:.2f}%")
    print(f"  Drag             = {(cagr_g2-cagr_a2)*100:.2f}pp/yr")
    print(f"  Tax log:\n{log2.to_string()}")

    # Test 3: loss year + recovery → carryforward should absorb the gain
    idx3 = pd.bdate_range("2020-01-01", "2022-12-30")
    n3 = len(idx3)
    rs = np.zeros(n3)
    by_year = pd.Series(idx3.year, index=idx3)
    rs[by_year == 2020] = -0.001  # ~-22%/yr
    rs[by_year == 2021] = +0.001
    rs[by_year == 2022] = +0.001
    rets3 = pd.DataFrame({"SPY": rs}, index=idx3)
    # Force a sell + buyback at each year-end to realize the loss/gain
    rebal3 = [pd.Timestamp("2020-01-02"),
              pd.Timestamp("2020-12-28"), pd.Timestamp("2020-12-30"),
              pd.Timestamp("2021-12-28"), pd.Timestamp("2021-12-30")]
    tw3 = pd.DataFrame({"SPY": [1.0, 0.0, 1.0, 0.0, 1.0]}, index=rebal3)
    aft3, log3 = simulate_after_tax(tw3, rets3)
    print(f"\nTest 3 — loss-year then gain-year (carryforward check):")
    print(f"  Tax log:\n{log3.to_string()}")
    assert log3.loc[2020, "tax_paid"] == 0.0, "2020 loss should pay no tax"
    print("  ✓ 2020 paid no tax")
    if 2021 in log3.index:
        print(f"  2021 carryforward used: {log3.loc[2021, 'carryforward_used']:.4f}")

    print("\nAll smoke tests completed.")
