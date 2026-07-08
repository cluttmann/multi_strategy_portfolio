"""
Mega Backtest — all strategies in the production codebase vs SPY and MSCI World.

The 7 currently DEPLOYED strategies (see DEPLOYED_STRATEGIES below; weights and
tickers mirror main.py's strategy_allocations + STRATEGY_SYMBOLS):
  HFEA (15%)              UPRO/TMF/KMLM 45/25/30, quarterly rebalance
  SPXL SMA (15%)          SPXL when SPY > 200-SMA × 1.01, SGOV otherwise
  9-Sig (5%)              TQQQ/AGG 60/40 with 9% quarterly signal-line targeting + crash protection
  DM 2× best-of-3 (20%)   SPUU/QLD/EFO rotation via blended 6m/12m momentum + DD30 + vol25
  Regime SSO (12%)        7-signal composite regime detector, SSO ↔ USFR rotation
  7-Asset Rotator (15%)   Adaptive Asset Allocation over NTSD/SAA/EET/UBT/UST/UGL/DBC, top-3 momentum, inverse-vol + DD30 + vol25
  World 40/30/30 (18%)    40% WLDU + 30% GOLY + 30% TLT, quarterly rebalance (intl diversifier)

Discontinued (kept in the HISTORIC universe only, NOT deployed): RSSB/WTIP
(retired 2026-05-11), Regime World (retired 2026-05-12), Sector Momentum (never
promoted). The engine also carries ~80 historic strategies as a re-mining universe.

Plus aggregate portfolio at current weights and pure SPY / pure URTH benchmarks.

Data layer (research/extended_data.py): tiered splice of Testfolio SIM CSVs +
real Alpaca IEX + EODHD feeds, so each strategy is backtested on its longest
available native window (SPXL SMA reaches back to 1970).

Output: results.csv, equity_curves.png, drawdowns.png, rolling_sharpe.png, report.html
"""

from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
import requests
import matplotlib.pyplot as plt
import warnings
warnings.filterwarnings('ignore')


# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════

START_DATE = "1970-01-02"  # Extended further — captures 1973-74 stagflation bear, 1980-82 Volcker recession, 1987 crash, etc.
END_DATE = "2026-05-09"
RISK_FREE_RATE = 0.02

# Data source toggle: "eodhd" (preferred — long history) or "alpaca" (fallback)
DATA_SOURCE = "eodhd"
EODHD_TOKEN_ENV = "EODHD_TOKEN"

# Synthetic leveraged-ETF reconstruction matches Testfolio's public ?L=N formula:
#   daily_return ≈ leverage × index_daily_return
#                  − SW × (leverage−1) × (short_rate_daily + SP/252)   ← swap-financed borrow
#                  − (leverage−1) × E / 252                            ← LETF expense (Testfolio default)
#
# Testfolio defaults (matched here):
#   SW = 1.1     swap exposure multiplier — LETFs over-collateralize their swaps
#   SP = 0.4%    spread above FFR — counterparties charge LIBOR/SOFR + spread
#   E  = 0.5% × (leverage − 1)   default LETF expense (1% for 3× ETFs, 0.5% for 2×)
#
# Short rate comes from FRED DGS3MO via the BIL series (extended pre-1981 via TB3MS).
# Per-ticker stated ER in SYNTH_LEV_ETFS is intentionally ignored: Testfolio's E formula
# is the standard, and the underlying SIMs (SPYSIM/TLTSIM/etc.) already include their
# own native ER. Using a single E formula keeps every synthetic LETF directly comparable
# to Testfolio's `?L=N` output on the same underlying.
SYNTH_FINANCING_RATE = 0.04            # fallback constant if BIL missing
SYNTH_LETF_SW = 1.1                    # swap exposure multiplier (Testfolio default)
SYNTH_LETF_SP = 0.004                  # 40bp spread above FFR (Testfolio default)
SYNTH_LETF_E_PER_LEV = 0.005           # 0.5%/yr per unit of incremental positive leverage
SYNTH_LEV_ETFS = {
    # ticker:  (leverage, underlying, expense_ratio, live_inception)
    "UPRO":   (3.0, "SPY", 0.0091, "2009-06-25"),
    "TMF":    (3.0, "TLT", 0.0105, "2009-04-16"),
    "TQQQ":   (3.0, "QQQ", 0.0086, "2010-02-11"),
    "SPUU":   (2.0, "SPY", 0.0064, "2014-05-28"),
    "EFO":    (2.0, "EFA", 0.0095, "2009-06-04"),
    "QLD":    (2.0, "QQQ", 0.0095, "2006-06-19"),   # 2× Nasdaq-100
    "SAA":    (2.0, "IWM", 0.0095, "2007-02-09"),   # 2× Russell 2000 small-cap
    "EET":    (2.0, "EEM", 0.0095, "2009-06-18"),   # 2× emerging markets
    "UBT":    (2.0, "TLT", 0.0094, "2010-01-21"),   # 2× 20+yr Treasuries
    "UST":    (2.0, "IEF", 0.0094, "2010-01-21"),   # 2× 7-10yr Treasuries
    "UGL":    (2.0, "GLD", 0.0095, "2008-12-03"),   # 2× gold
    "SOXL":   (3.0, "QQQ", 0.0094, "2010-03-11"),   # 3× semis; underlying SOXX best, falling back to QQQ for synth
    "EDC":    (3.0, "EEM", 0.0098, "2008-12-17"),   # 3× emerging markets
    "TYD":    (3.0, "IEF", 0.0094, "2009-04-16"),   # 3× 7-10yr Treasuries (free for new strategies)
    "TNA":    (3.0, "IWM", 0.0094, "2008-11-05"),   # 3× Russell 2000 (free for new strategies)
    "SPXL":   (3.0, "SPY", 0.0091, "2008-11-05"),   # 3× S&P 500 (used by bt_spxl_sma; same family as UPRO)
    "SSO":    (2.0, "SPY", 0.0089, "2006-06-21"),   # 2× S&P 500 (used by Regime SSO + sector_swap)
}
USFR_LIVE_FROM_DATE = "2014-02-04"   # before this, splice with SHY (1-3yr Treasury)
URTH_LIVE_FROM_DATE = "2012-01-12"   # before this, splice with VT (Vanguard Total World)
KMLM_LIVE_FROM_DATE = "2020-12-02"   # KMLM inception; before this, no real managed-futures data

# Strategy intra-allocations (matches main.py production)
HFEA_WEIGHTS = {"UPRO": 0.45, "TMF": 0.25, "KMLM": 0.30}
RSSB_WTIP_WEIGHTS = {"RSSB": 0.70, "WTIP": 0.30}

# HFEA recipe history: pre-KMLM-launch the classic HFEA was 55/45 UPRO/TMF;
# the 3-asset 45/25/30 recipe (with KMLM) only became viable after 2020-12.
HFEA_PRE_KMLM_WEIGHTS = {"UPRO": 0.55, "TMF": 0.45}
HFEA_POST_KMLM_WEIGHTS = HFEA_WEIGHTS  # 45/25/30

# Sector momentum sector ETF universe
SECTOR_ETFS = ["ROM", "UYG", "DIG", "RXL", "UXI", "UGE", "UCC", "UPW", "UYM", "URE", "LTL"]
SECTOR_TOP_N = 3
SECTOR_BOND_ETF = "SCHZ"
SECTOR_HOLDING_FUND = "SHV"
SECTOR_LOOKBACKS_DAYS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}
SECTOR_WEIGHTS = {"1m": 0.40, "3m": 0.20, "6m": 0.20, "12m": 0.20}

# 9-Sig — Jason Kelly canonical (60/40 TQQQ/AGG)
NINE_SIG_TARGET = {"TQQQ": 0.60, "AGG": 0.40}
NINE_SIG_QUARTERLY_GROWTH = 0.09
NINE_SIG_TOLERANCE_PCT = 0.025      # hold band = 2.5% of sleeve NAV (micro-trade guard)
NINE_SIG_DRAWDOWN_THRESHOLD = 0.30  # 30-Down: TQQQ ≤ 70% of its rolling 8-quarter high
NINE_SIG_LOOKBACK_QUARTERS = 8      # rolling 8-quarter (~504 trading-day) high
NINE_SIG_MAX_SELL_IGNORES = 2       # Kelly 4→2: ignore at most 2 consecutive sell signals
NINE_SIG_SPIKE_GAIN = 1.00          # TQQQ +100% in a quarter → reset to 60%
NINE_SIG_THROTTLE = 0.90            # a BUY may spend at most 90% of the bond holdings
NINE_SIG_BOND_FLOOR = 0.10          # bonds never drop below 10% of NAV
# NOTE: a bond-drift base reset was considered but dropped — the legacy 0.30 threshold
# is the 80/20 number and there is no clear canonical 60/40 value (mirrors main.py).

# KMLM extension: KMLM Alpaca data starts ~Feb-2021. Use DBMF (similar managed-
# futures trend-following ETF, history back to May-2019) for the pre-KMLM window.
KMLM_LIVE_FROM = "2021-02-08"

# WLDU (Leverage Shares 2x Long World Stock Daily ETF) launched in early 2026
# — only ~2 months of live data. Synthetic = daily 2× URTH return − financing
# drag − expense ratio (matches the daily-reset mechanic of Leverage Shares ETPs).
WLDU_LIVE_FROM = "2026-03-12"
WLDU_FINANCING_RATE = 0.04    # rough avg short rate over the window
WLDU_EXPENSE_RATIO = 0.0075   # typical for Leverage Shares 2x ETPs

# RSSB/WTIP (synthetic recipes — match earlier work)
RSSB_SYNTH = {"VT": 1.00, "IEF": 1.00}
WTIP_SYNTH = {"TIP": 0.85, "BIL": 0.10, "BTC/USD": 0.075, "DBC": 0.7125, "GLD": 0.0712, "SLV": 0.0712}
RSSB_LIVE_FROM = "2023-12-01"
WTIP_LIVE_FROM = "2025-06-25"

# Regime SSO (production-equivalent thresholds; news signal omitted in backtest)
REGIME_CFG = {
    "spy_sma_period": 200,
    "breadth_high": 0.60,
    "breadth_low": 0.40,
    "vix_low": 18.0,
    "vix_high": 25.0,
    "vix_5d_change_high": 0.20,
    "adx_period": 14,
    "adx_strong": 25.0,
    "credit_sma_period": 50,
    "canary_sma_period": 50,
    "fast_exit_days": 3,
    "fast_exit_score": -3,
    "slow_exit_days": 15,
    "slow_exit_score": 0,
    "reentry_score": 3,
    "standard_reentry_days": 15,
    "fed_hike_threshold_bps": 50,
    "fed_hike_lookback_days": 90,
}

# Regime World variant — same logic but on URTH (MSCI World) with a 255-day SMA.
# Longer SMA is well-known to work better for international/global indices vs the
# US-style 200-day. Risk asset is URTH (1× World) — no clean leveraged world ETF
# exists on Alpaca, so this sleeve is more conservative than regime_sso.
REGIME_WORLD_CFG = {
    **REGIME_CFG,
    "world_index": "URTH",
    "world_sma_period": 255,
    "risk_asset": "URTH",
    "safe_asset": "USFR",
}

# Production allocation weights for aggregate portfolio
# Updated 2026-05-11: rebalanced toward regime sleeves (35% combined) and away
# from RSSB/WTIP + 9-Sig. Regime World bumped to 20% — note synthetic data risk.
AGGREGATE_WEIGHTS = {
    # Updated 2026-05-12: F4 (WLDU+GOLY+TLT) promoted from CANDIDATE → DEPLOYED.
    # Rebalanced allocation across 7 sleeves with tax-aware caps in mind:
    #   • HFEA / SPXL SMA: ceiling 15% each (US-equity concentration)
    #   • Regime SSO: ceiling 12% (high turnover via SSO↔USFR switching)
    #   • AAA Free 2× + NTSD: ceiling 15% (highest turnover via monthly 7-asset rotation
    #     — limits short-term cap-gains tax drag in this taxable account)
    #   • DM 2× best-of-3: ceiling 20% (medium turnover, has EFO intl exposure)
    #   • 9-Sig: 5% (high MaxDD; small allocation deliberately)
    #   • F4 (new): 18% — most tax-efficient sleeve (quarterly rebal, 3 fixed assets),
    #     becomes the primary intl-diversifier role.
    "HFEA": 0.15,
    "SPXL SMA": 0.15,
    "9-Sig": 0.05,
    "DM 2× best-of-3 (SPUU/QLD/EFO) + DD30 + vol25": 0.20,
    "Regime SSO": 0.12,
    "7-Asset Rotator": 0.15,
    "🌐 World 40/30/30": 0.18,
}


# ═══════════════════════════════════════════════════════════════════════
# RETIREMENT PROJECTION CONFIG
# ═══════════════════════════════════════════════════════════════════════
# Forward-projects the deployed aggregate to answer "when can I retire?".
# All math runs in REAL dollars (today's purchasing power):
#   real_cagr = (1 + after_tax_cagr) / (1 + inflation) - 1
# Contributions are treated as constant in real terms (i.e. you index them
# to inflation each year — the realistic case).
RETIREMENT_BIRTH_DATE = "1993-07-16"
RETIREMENT_TARGET_REAL = 1_500_000        # $1.5M in 2026 purchasing power
RETIREMENT_DEFAULT_MONTHLY = 300          # current monthly contribution (real $)
RETIREMENT_INFLATION = 0.025              # 2.5% long-term US inflation
RETIREMENT_SWR = 0.035                    # 3.5% safe withdrawal rate
RETIREMENT_MAX_YEARS = 50
RETIREMENT_AGE_BUCKETS = [40, 45, 50, 55, 60, 65]
RETIREMENT_MONTHLY_SCENARIOS = [300, 500, 1000, 1500, 2000, 3000]
RETIREMENT_TARGET_SCENARIOS = [1_000_000, 1_500_000, 2_000_000, 3_000_000]
RETIREMENT_MC_SIMS = 5000


# ═══════════════════════════════════════════════════════════════════════
# STRATEGY REGISTRY — three tiers
# ═══════════════════════════════════════════════════════════════════════
#
# Lifecycle of a strategy (always explicit user action — never automatic):
#
#   New idea  ─►  HISTORIC  (initial backtest only)
#                     │
#                     │ User explicitly promotes after analysis
#                     ▼
#                CANDIDATE  (active evaluation, deep MC + stress testing)
#                     │
#                     ├──► DEPLOYED  (if approved; also add to main.py)
#                     │
#                     └──► HISTORIC  (if rejected; reason_demoted noted)
#
# Today's state: 7 DEPLOYED, 0 CANDIDATE (no active candidates), ~80 HISTORIC.
# Historic strategies are preserved as a "universe to re-mine" when looking
# for new candidate ideas — nothing is ever deleted.

DEPLOYED_STRATEGIES = {
    # Strategies currently running in production (main.py strategy_allocations).
    # name → metadata for orchestration + reporting
    #
    # Quality tiers reflect the lowest-quality input the strategy depends on:
    #   A = Real ETF / Testfolio sim / clean proxy (corr ≥ 0.90)
    #   B = Acceptable proxy (corr 0.75-0.90 or modest basket/duration mismatch)
    #   C = Caveated proxy (corr < 0.75 or active-management contamination)
    #   D = Unreliable for the pre-real window (do not trust pre-splice metrics)
    "HFEA": {
        "fn": "bt_hfea", "needs": ["returns"],
        "earliest": "1988-04-01",
        "quality": "A",
        "data_sources": "SPYSIM→SPY (1993), TLTSIM→TLT (2002), KMLMSIM→KMLM (2020)",
        "alloc": 0.15,
        "description": "Aggressive 3× UPRO/TMF/KMLM (45/25/30 with managed futures)",
    },
    "SPXL SMA": {
        "fn": "bt_spxl_sma", "needs": ["returns", "prices"],
        "earliest": "1970-10-01",
        "quality": "A",
        "data_sources": "SPYSIM→SPY (1993) + synthetic SPXL pre-2008-11-05",
        "alloc": 0.15,
        "description": "3× SPY with 200-SMA trend gate",
    },
    "9-Sig": {
        "fn": "bt_nine_sig", "needs": ["returns", "prices"],
        "earliest": "1987-01-02",
        "quality": "A",
        "data_sources": "QQQSIM→QQQ (1999) for TQQQ synth, BNDSIM (via AGG routing) for defensive",
        "alloc": 0.05,
        "description": "Jason Kelly TQQQ/AGG with quarterly signal + crash protection",
    },
    "DM 2× best-of-3 (SPUU/QLD/EFO) + DD30 + vol25": {
        "fn": "bt_dm_2x_best_of_3_dd30_vol25", "needs": ["returns", "prices"],
        "earliest": "1987-04-01",
        "quality": "A",
        "data_sources": "SPYSIM, QQQSIM, EFASIM, BNDSIM",
        "alloc": 0.20,
        "description": "Best-of-3 rotation among 2× equity sleeves + DD-stop + vol-target",
    },
    "Regime SSO": {
        "fn": "bt_regime_sso", "needs": ["returns", "prices", "vix", "fed"],
        "earliest": "1990-10-02",
        "quality": "A",
        "data_sources": "SPYSIM→SPY, FRED VIX (1990+), FRED DFEDTARU (1990+)",
        "alloc": 0.12,
        "description": "7-signal composite regime detector — SSO ↔ USFR rotation",
    },
    "7-Asset Rotator": {
        "fn": "bt_aaa_free_2x_plus_ntsd", "needs": ["returns", "prices"],
        "earliest": "2006-08-01",
        "quality": "A",
        "data_sources": "NTSDSIM, GLDSIM (for UGL synth), TLTSIM (UBT/UST synth), real DBC (≥2006-02-03), 2× ETFs spliced",
        "alloc": 0.15,
        "description": "Adaptive Asset Allocation on 7-asset universe (NTSD/SAA/EET/UBT/UST/UGL/DBC) — top-3 momentum, inverse-vol weighted, DD30 + vol25. Capped at 15% due to high turnover (taxable account).",
    },
    "🌐 World 40/30/30": {
        "fn": "bt_w8_f4_wldu_goly_tlt", "needs": ["returns"],
        "earliest": "2002-07-30",
        "quality": "A",
        "data_sources": "URTHSIM→URTH (2012) for WLDU synth, GLD/DBMFSIM/LQD for GOLY synth, TLTSIM→TLT (2002)",
        "alloc": 0.18,
        "description": "International diversifier — 40% WLDU (2× MSCI World) + 30% GOLY (gold+MF+corp-bonds triple-stack) + 30% TLT (unleveraged long Treasury). Quarterly rebalance, 3 fixed assets — most tax-efficient sleeve. Promoted 2026-05-12 from Wave 8.",
    },
}


CANDIDATE_STRATEGIES = {
    # No active candidates. F4 promoted to DEPLOYED 2026-05-12.
    # E7 and F2 moved to DISCONTINUED.
}




# ═══════════════════════════════════════════════════════════════════════
# DISCONTINUED — strategies actively REJECTED, not preserved as universe
# ═══════════════════════════════════════════════════════════════════════
# Unlike HISTORIC (which is a "universe to re-mine"), DISCONTINUED is a
# permanent rejection record. These won't be re-considered as candidates
# in the future. They show up in the report as a clearly-flagged section
# so we don't accidentally re-test the same losers.
DISCONTINUED_STRATEGIES = {
    # Defensive-only — high Sharpe but unacceptably low CAGR for an aggressive sleeve
    "Permanent Portfolio (Browne 25/25/25/25)": {
        "fn": "bt_permanent_portfolio", "needs": ["returns"],
        "earliest": "1988-01-04", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "7.18% CAGR / 0.78 Sharpe — defensive-only, CAGR way too low for any deployment role",
    },
    "Bridgewater All-Weather (30/40/15/7.5/7.5)": {
        "fn": "bt_bridgewater_all_weather", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "7.59% CAGR / 0.72 Sharpe — defensive-only profile, unsuitable for aggressive portfolio",
    },
    "Golden Butterfly (Treadway 20×5)": {
        "fn": "bt_golden_butterfly", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "7.34% CAGR / 0.68 Sharpe — same defensive-only pattern",
    },
    "Faber GTAA 5-asset (per-asset 200-SMA)": {
        "fn": "bt_faber_gtaa_5asset", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "6.89% CAGR / 0.62 Sharpe — too conservative; 1× with trend filter sacrifices too much upside",
    },
    "NTSX 100% (US 90/60 capital-efficient)": {
        "fn": "bt_ntsx_buyhold", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "11.17% CAGR / 0.57 Sharpe — lower than expected for capital-efficient; doesn't beat unleveraged SPY+TLT",
    },
    "Keller VAA (Vigilant Asset Allocation)": {
        "fn": "bt_keller_vaa", "needs": ["returns", "prices"],
        "earliest": "1988-01-04", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "9.62% CAGR / 0.62 Sharpe — momentum-canary combo underperformed expectations",
    },
    "TQQQ + 200-SMA gate": {
        "fn": "bt_tqqq_sma_gated", "needs": ["returns", "prices"],
        "earliest": "1987-10-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "19.18% CAGR / 0.32 Sharpe / -94% MaxDD — TQQQ daily-decay during corrections eats the gate's benefit",
    },
    "HFEA Classic 55/45 (Hedgefundie original)": {
        "fn": "bt_hfea_classic_55_45", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "17.17% CAGR / 0.50 Sharpe — bond-heavy variant looks dated post-2022; our modern HFEA (45/25/30+KMLM) dominates on both CAGR and Sharpe",
    },
    "HFEA No-Bonds 60/40 UPRO/KMLM": {
        "fn": "bt_hfea_no_bonds_60_40", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "17.09% CAGR / 0.51 Sharpe / -72% MaxDD — removing TMF entirely doesn't help; bonds still add value",
    },
    "HFEA monthly rebal": {
        "fn": "bt_hfea_monthly_rebal", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "14.99% CAGR / 0.57 Sharpe vs production 18.53%/0.57 — quarterly clearly better; monthly rebal eats trend-capture",
    },
    "HFEA 40/60 UPRO/TMF (bond-heavy)": {
        "fn": "bt_hfea_40_60", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "14.85% CAGR / 0.45 Sharpe — too defensive; bond-heavy weight punishes 2020+ era performance",
    },

    # ─── Auto-discontinued 2026-05-12 (CAGR<10% AND Sharpe≤0.5) ───
    "Regime SSO + sector swap": {
        "fn": "bt_regime_sso_with_sector_swap", "needs": ["returns", "prices", "vix", "fed"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 3.96% / Sharpe 0.08 / MaxDD -44.56% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Sector Momentum 2× top-1": {
        "fn": "bt_sector_momentum_top1", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 4.59% / Sharpe 0.09 / MaxDD -64.20% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "SPXL SMA + sector swap": {
        "fn": "bt_spxl_sma_with_sector_swap", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 9.65% / Sharpe 0.28 / MaxDD -42.56% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Cross-Asset DM Free 2× (no NTSD)": {
        "fn": "bt_cross_asset_dm_free_2x", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 9.62% / Sharpe 0.28 / MaxDD -60.11% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Sector Momentum optimized": {
        "fn": "bt_sector_momentum_optimized", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 8.85% / Sharpe 0.28 / MaxDD -58.96% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Sector Momentum 1× top-1": {
        "fn": "bt_sector_momentum_1x_top1", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 7.27% / Sharpe 0.30 / MaxDD -31.15% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Sector tiered leverage": {
        "fn": "bt_sector_momentum_tiered_leverage", "needs": ["returns", "prices", "vix"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 7.83% / Sharpe 0.31 / MaxDD -31.15% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Sector vs SPY hybrid": {
        "fn": "bt_sector_momentum_vs_spy", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 7.62% / Sharpe 0.32 / MaxDD -31.15% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Sector Momentum strong-signal 2×": {
        "fn": "bt_sector_momentum_strong_signal_2x", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 9.45% / Sharpe 0.32 / MaxDD -59.11% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "HFEA + sector swap (equity sleeve)": {
        "fn": "bt_hfea_with_sector_swap", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 9.64% / Sharpe 0.33 / MaxDD -37.32% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Cross-Asset Dual Momentum (Antonacci)": {
        "fn": "bt_cross_asset_dual_momentum", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 8.18% / Sharpe 0.33 / MaxDD -48.17% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Sector Momentum 70 SPY / 30 sector overlay": {
        "fn": "bt_sector_momentum_overlay", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 8.66% / Sharpe 0.41 / MaxDD -42.66% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Sector Momentum 1× threshold-gated": {
        "fn": "bt_sector_momentum_threshold", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 7.97% / Sharpe 0.44 / MaxDD -30.20% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "SPY Mean Reversion (De Bondt-Thaler)": {
        "fn": "bt_spy_mean_reversion", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 5.99% / Sharpe 0.44 / MaxDD -28.42% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Sector Momentum + TMF hedge": {
        "fn": "bt_sector_momentum_with_tmf_hedge", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 8.66% / Sharpe 0.46 / MaxDD -43.05% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Sector Momentum 1× vol-scaled": {
        "fn": "bt_sector_momentum_1x_volscaled", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 8.26% / Sharpe 0.47 / MaxDD -29.49% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Sector Momentum 1× top-5": {
        "fn": "bt_sector_momentum_1x_top5", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 8.06% / Sharpe 0.47 / MaxDD -33.43% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Sector Momentum 1× Sharpe-ranked": {
        "fn": "bt_sector_momentum_sharpe_ranked", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 8.14% / Sharpe 0.48 / MaxDD -30.20% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Sector Momentum 1× SPDR": {
        "fn": "bt_sector_momentum_1x", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 8.68% / Sharpe 0.49 / MaxDD -30.20% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Sector Momentum 1× indiv-trend": {
        "fn": "bt_sector_momentum_1x_indiv_trend", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 8.68% / Sharpe 0.49 / MaxDD -30.20% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "NTSD/UBT/UGL/DBC risk parity": {
        "fn": "bt_ntsd_risk_parity", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 9.06% / Sharpe 0.49 / MaxDD -32.32% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },
    "Sector Momentum VIX-gated leverage": {
        "fn": "bt_sector_momentum_vix_gated_lev", "needs": ["returns", "prices", "vix"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12: CAGR 8.75% / Sharpe 0.50 / MaxDD -30.20% — fails both performance and risk-adjusted thresholds (CAGR<10% AND Sharpe≤0.5)",
    },

    # ─── Auto-discontinued 2026-05-12 (CAGR<11% floor) ───
    "Time-Series Momentum (Moskowitz)": {
        "fn": "bt_time_series_momentum", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (CAGR floor): CAGR 5.92% / Sharpe 0.52 / MaxDD -14.39% — CAGR below 11% threshold",
    },
    "Faber GTAA (7-asset)": {
        "fn": "bt_faber_gtaa_7asset", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (CAGR floor): CAGR 6.24% / Sharpe 0.57 / MaxDD -14.14% — CAGR below 11% threshold",
    },
    "Keller DAA (Defensive Asset Allocation)": {
        "fn": "bt_keller_daa", "needs": ["returns", "prices"],
        "earliest": "1988-01-04", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (CAGR floor): CAGR 7.51% / Sharpe 0.63 / MaxDD -24.79% — CAGR below 11% threshold",
    },
    "Risk Parity 4-asset (Maillard)": {
        "fn": "bt_risk_parity_4asset", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (CAGR floor): CAGR 7.81% / Sharpe 0.62 / MaxDD -20.29% — CAGR below 11% threshold",
    },
    "DM DD20 SPUU fast 3m/6m": {
        "fn": "bt_dm_dd20_spuu_vol20_fast", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (CAGR floor): CAGR 10.07% / Sharpe 0.46 / MaxDD -42.79% — CAGR below 11% threshold",
    },
    "Sector Momentum (original)": {
        "fn": "bt_sector_momentum", "needs": ["returns", "prices"],
        "earliest": "1999-12-31", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (CAGR floor): CAGR 10.08% / Sharpe 0.33 / MaxDD -58.31% — CAGR below 11% threshold",
    },
    "DM abs + DD-stop 10%": {
        "fn": "bt_dual_momentum_abs_dd10", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (CAGR floor): CAGR 10.33% / Sharpe 0.39 / MaxDD -46.06% — CAGR below 11% threshold",
    },
    "DM abs + SMA overlay": {
        "fn": "bt_dual_momentum_abs_sma_overlay", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (CAGR floor): CAGR 10.45% / Sharpe 0.33 / MaxDD -66.78% — CAGR below 11% threshold",
    },
    "NTSD + DD25 + vol18": {
        "fn": "bt_ntsd_dd25_vol18", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (CAGR floor): CAGR 10.48% / Sharpe 0.48 / MaxDD -59.92% — CAGR below 11% threshold",
    },
    "NTSD + vol-target 18%": {
        "fn": "bt_ntsd_voltarget", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (CAGR floor): CAGR 10.67% / Sharpe 0.46 / MaxDD -51.47% — CAGR below 11% threshold",
    },
    "NTSD/UBT 60/40 leveraged barbell": {
        "fn": "bt_ntsd_ubt_barbell", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (CAGR floor): CAGR 10.86% / Sharpe 0.53 / MaxDD -48.85% — CAGR below 11% threshold",
    },
    "DM DD20 SPUU vol-15": {
        "fn": "bt_dm_dd20_spuu_vol15", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (CAGR floor): CAGR 10.97% / Sharpe 0.64 / MaxDD -34.38% — CAGR below 11% threshold",
    },

    # ─── Auto-discontinued 2026-05-12 (Sharpe<0.5 floor) ───
    "DM DD20 + TQQQ": {
        "fn": "bt_dm_dd20_tqqq", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 12.93% / Sharpe 0.29 / MaxDD -76.94% — Sharpe below 0.5 threshold",
    },
    "DM DD30 + TQQQ + TMF": {
        "fn": "bt_dm_dd30_tqqq_tmf", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 16.61% / Sharpe 0.30 / MaxDD -90.37% — Sharpe below 0.5 threshold",
    },
    "DM DD30 + UPRO + 3m/6m": {
        "fn": "bt_dm_dd30_upro_3m6m", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 12.75% / Sharpe 0.31 / MaxDD -68.26% — Sharpe below 0.5 threshold",
    },
    "DM DD30 + TQQQ": {
        "fn": "bt_dm_dd30_tqqq", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 15.66% / Sharpe 0.32 / MaxDD -67.55% — Sharpe below 0.5 threshold",
    },
    "DM DD30 best-of-4 (+EDC)": {
        "fn": "bt_dm_dd30_best_of_4", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 17.00% / Sharpe 0.32 / MaxDD -78.87% — Sharpe below 0.5 threshold",
    },
    "DM DD30 + UPRO + 1m/3m": {
        "fn": "bt_dm_dd30_upro_1m3m", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 13.16% / Sharpe 0.33 / MaxDD -66.71% — Sharpe below 0.5 threshold",
    },
    "DM DD20 + UPRO": {
        "fn": "bt_dm_dd20_upro", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 14.67% / Sharpe 0.38 / MaxDD -66.31% — Sharpe below 0.5 threshold",
    },
    "NTSD B&H (baseline)": {
        "fn": "bt_ntsd_buyhold", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 11.94% / Sharpe 0.40 / MaxDD -74.41% — Sharpe below 0.5 threshold",
    },
    "Cross-Asset DM 2× + DD30 + vol25": {
        "fn": "bt_cross_asset_dual_momentum_levered", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 12.40% / Sharpe 0.40 / MaxDD -53.70% — Sharpe below 0.5 threshold",
    },
    "DM conditional 2×/3× leverage": {
        "fn": "bt_dm_conditional_leverage", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 14.32% / Sharpe 0.42 / MaxDD -55.72% — Sharpe below 0.5 threshold",
    },
    "DM Faber 200-SMA filter": {
        "fn": "bt_dual_momentum_faber", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 12.39% / Sharpe 0.42 / MaxDD -59.35% — Sharpe below 0.5 threshold",
    },
    "DM abs + VIX gate >28": {
        "fn": "bt_dual_momentum_abs_vix_gate", "needs": ["returns", "prices", "vix"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 13.57% / Sharpe 0.42 / MaxDD -60.64% — Sharpe below 0.5 threshold",
    },
    "DM DD20 best-of-3 (UPRO/TQQQ/EFO)": {
        "fn": "bt_dm_dd20_best_of_3", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 18.65% / Sharpe 0.42 / MaxDD -66.31% — Sharpe below 0.5 threshold",
    },
    "DM DD30 best-of-3": {
        "fn": "bt_dm_dd30_best_of_3", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 20.20% / Sharpe 0.42 / MaxDD -67.55% — Sharpe below 0.5 threshold",
    },
    "Dual Momentum (original 2-asset)": {
        "fn": "bt_dual_momentum", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 13.47% / Sharpe 0.43 / MaxDD -59.35% — Sharpe below 0.5 threshold",
    },
    "DM 6m+12m multi-lookback": {
        "fn": "bt_dual_momentum_multi_lookback", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 13.75% / Sharpe 0.44 / MaxDD -59.87% — Sharpe below 0.5 threshold",
    },
    "DM abs + DD-stop 15%": {
        "fn": "bt_dual_momentum_abs_dd15", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 12.15% / Sharpe 0.44 / MaxDD -54.55% — Sharpe below 0.5 threshold",
    },
    "DM absolute-only": {
        "fn": "bt_dual_momentum_abs_only", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 13.97% / Sharpe 0.44 / MaxDD -60.78% — Sharpe below 0.5 threshold",
    },
    "NTSX + NTSD 50/50 blend": {
        "fn": "bt_ntsx_ntsd_blend", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 11.36% / Sharpe 0.45 / MaxDD -61.96% — Sharpe below 0.5 threshold",
    },
    "DM DD30 + UPRO": {
        "fn": "bt_dm_dd30_upro", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 18.26% / Sharpe 0.46 / MaxDD -62.42% — Sharpe below 0.5 threshold",
    },
    "DM DD30 + UPRO + VIX>35 kill": {
        "fn": "bt_dm_dd30_upro_vix_kill", "needs": ["returns", "prices", "vix"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 18.19% / Sharpe 0.46 / MaxDD -62.42% — Sharpe below 0.5 threshold",
    },
    "DM optimized": {
        "fn": "bt_dual_momentum_optimized", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 14.92% / Sharpe 0.48 / MaxDD -60.24% — Sharpe below 0.5 threshold",
    },
    "DM DD30 + UPRO + TMF": {
        "fn": "bt_dm_dd30_upro_tmf", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 21.90% / Sharpe 0.48 / MaxDD -73.97% — Sharpe below 0.5 threshold",
    },
    "DM DD30 UPRO vol-30": {
        "fn": "bt_dm_dd30_upro_vol30", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 14.67% / Sharpe 0.48 / MaxDD -55.11% — Sharpe below 0.5 threshold",
    },
    "TQQQ + UBT 70/30": {
        "fn": "bt_tqqq_ubt_70_30", "needs": ["returns"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 25.63% / Sharpe 0.49 / MaxDD -92.45% — Sharpe below 0.5 threshold",
    },
    "DM abs + TMF hedge sleeve": {
        "fn": "bt_dual_momentum_abs_tmf_hedge", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 12.96% / Sharpe 0.49 / MaxDD -54.43% — Sharpe below 0.5 threshold",
    },
    "DM abs + DD-stop 30%": {
        "fn": "bt_dual_momentum_abs_dd30", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (Sharpe floor): CAGR 14.28% / Sharpe 0.49 / MaxDD -48.72% — Sharpe below 0.5 threshold",
    },

    # ─── User-discontinued 2026-05-12 contender review ───
    "Adaptive Asset Allocation 1× (BPG)": {
        "fn": "bt_adaptive_asset_allocation", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: kept current solution. Metrics: CAGR 11.03% / Sharpe 0.72 / MaxDD -26.28%.",
    },
    "HFEA + 200-SMA gate": {
        "fn": "bt_hfea_sma_gated", "needs": ["returns", "prices"],
        "earliest": "1988-04-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: happy with current HFEA solution. Metrics: CAGR 14.79% / Sharpe 0.72 / MaxDD -36.16%.",
    },
    "AAA 3×/2× asymmetric": {
        "fn": "bt_aaa_3x_us_only_levered", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: too much leverage. Metrics: CAGR 15.65% / Sharpe 0.69 / MaxDD -33.12%.",
    },
    "DM 1× unleveraged": {
        "fn": "bt_dual_momentum_1x", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: kept current solution. Metrics: CAGR 11.44% / Sharpe 0.69 / MaxDD -33.72%.",
    },
    "TQQQ-HFEA 45/25/30 TQQQ/TMF/KMLM": {
        "fn": "bt_tqqq_tmf_kmlm", "needs": ["returns"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: happy with current HFEA / 9-Sig sleeves. Metrics: CAGR 23.51% / Sharpe 0.69 / MaxDD -72.64%.",
    },
    "GDE + KMLM 70/30": {
        "fn": "bt_gde_kmlm_70_30", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: kept current solution. Metrics: CAGR 12.42% / Sharpe 0.68 / MaxDD -31.64%.",
    },
    "NTSD wide universe (+SLV)": {
        "fn": "bt_ntsd_aaa_wide_universe", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: too many tickers. Metrics: CAGR 15.41% / Sharpe 0.66 / MaxDD -32.93%.",
    },
    "Antonacci GEM strict (12m SPY/EFA/AGG)": {
        "fn": "bt_antonacci_gem_strict", "needs": ["returns", "prices"],
        "earliest": "1988-01-04", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: kept current solution. Metrics: CAGR 11.94% / Sharpe 0.66 / MaxDD -33.72%.",
    },
    "HFEA + DD-30 stop": {
        "fn": "bt_hfea_dd30_stop", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: happy with current HFEA solution. Metrics: CAGR 16.61% / Sharpe 0.65 / MaxDD -48.17%.",
    },
    "Composite Dual Momentum (3/6/9/12m blend)": {
        "fn": "bt_composite_dual_momentum", "needs": ["returns", "prices"],
        "earliest": "1988-04-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: kept current solution. Metrics: CAGR 11.40% / Sharpe 0.64 / MaxDD -33.72%.",
    },
    "DM DD30 SPUU vol-25 + TLT defensive": {
        "fn": "bt_dm_dd30_spuu_vol25_tlt", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 15.81% / Sharpe 0.63 / MaxDD -47.09%.",
    },
    "HFEA Diversified 4-asset 35/25/20/20": {
        "fn": "bt_hfea_diversified_4asset", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 14.84% / Sharpe 0.63 / MaxDD -44.77%.",
    },
    "HFEA + vol-target 25%": {
        "fn": "bt_hfea_vol_target_25", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 15.12% / Sharpe 0.62 / MaxDD -56.83%.",
    },
    "HFEA 30/40/30 UPRO/TMF/KMLM (conservative)": {
        "fn": "bt_hfea_30_40_30_kmlm", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 14.24% / Sharpe 0.61 / MaxDD -53.16%.",
    },
    "AAA Free 3× aggressive": {
        "fn": "bt_aaa_free_3x_aggressive", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 15.13% / Sharpe 0.60 / MaxDD -36.62%.",
    },
    "HFEA 50/30/20 UPRO/TMF/KMLM (modern bond-light)": {
        "fn": "bt_hfea_50_30_20_kmlm", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 17.06% / Sharpe 0.59 / MaxDD -58.52%.",
    },
    "Leveraged Permanent Portfolio (Browne 25/25/25/25)": {
        "fn": "bt_leveraged_permanent_portfolio", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 11.24% / Sharpe 0.57 / MaxDD -38.90%.",
    },
    "HFEA + Gold Overlay 40/30/30 UPRO/TMF/UGL": {
        "fn": "bt_hfea_gold_overlay", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 14.95% / Sharpe 0.53 / MaxDD -57.20%.",
    },
    "DM DD20 SPUU vol-18": {
        "fn": "bt_dm_dd20_spuu_vol18", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 11.89% / Sharpe 0.60 / MaxDD -36.89%.",
    },
    "DM DD20 SPUU vol-20": {
        "fn": "bt_dm_dd20_spuu_vol20", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 12.58% / Sharpe 0.59 / MaxDD -40.24%.",
    },
    "DM DD15 SPUU vol-20": {
        "fn": "bt_dm_dd15_spuu_vol20", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 12.09% / Sharpe 0.58 / MaxDD -40.44%.",
    },
    "DM DD25 SPUU vol-20": {
        "fn": "bt_dm_dd25_spuu_vol20", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 12.50% / Sharpe 0.58 / MaxDD -40.22%.",
    },
    "DM DD30 SPUU vol-20": {
        "fn": "bt_dm_dd30_spuu_vol20", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 12.51% / Sharpe 0.58 / MaxDD -40.22%.",
    },
    "DM DD20 conditional 1×/2× + vol-20": {
        "fn": "bt_dm_dd20_conditional_1x2x_vol20", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 11.45% / Sharpe 0.58 / MaxDD -35.70%.",
    },
    "DM noDD SPUU vol-22": {
        "fn": "bt_dm_nodd_spuu_vol22", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 13.13% / Sharpe 0.57 / MaxDD -42.60%.",
    },
    "DM DD30 SPUU vol-25": {
        "fn": "bt_dm_dd30_spuu_vol25", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 13.70% / Sharpe 0.56 / MaxDD -45.57%.",
    },
    "DM DD30 UPRO vol-20": {
        "fn": "bt_dm_dd30_upro_vol20", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 12.40% / Sharpe 0.56 / MaxDD -42.89%.",
    },
    "DM DD20 SPUU vol-25": {
        "fn": "bt_dm_dd20_spuu_vol25", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 13.40% / Sharpe 0.55 / MaxDD -44.36%.",
    },
    "DM DD40 SPUU vol-25": {
        "fn": "bt_dm_dd40_spuu_vol25", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 13.66% / Sharpe 0.55 / MaxDD -45.57%.",
    },
    "DM abs + DD-stop 20%": {
        "fn": "bt_dual_momentum_abs_dd20", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 14.52% / Sharpe 0.52 / MaxDD -51.11%.",
    },
    "DM abs + DD-stop 25%": {
        "fn": "bt_dual_momentum_abs_dd25", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 14.87% / Sharpe 0.52 / MaxDD -49.68%.",
    },
    "DM DD30 SPUU vol-30": {
        "fn": "bt_dm_dd30_spuu_vol30", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 13.64% / Sharpe 0.51 / MaxDD -47.30%.",
    },
    "DM abs + VIX gate >24": {
        "fn": "bt_dual_momentum_abs_dd20_vix", "needs": ["returns", "prices", "vix"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 13.87% / Sharpe 0.50 / MaxDD -51.11%.",
    },
    "AAA Free 2× + NTSD top-2": {
        "fn": "bt_aaa_free_2x_plus_ntsd_top2", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 13.65% / Sharpe 0.55 / MaxDD -40.73%.",
    },
    "NTSD top-1 concentrated": {
        "fn": "bt_ntsd_top1_concentrated", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 14.15% / Sharpe 0.55 / MaxDD -32.81%.",
    },
    "DM abs + TMF 30% hedge": {
        "fn": "bt_dual_momentum_abs_dd20_tmf", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 13.02% / Sharpe 0.53 / MaxDD -56.25%.",
    },
    "DM 2× QLD only": {
        "fn": "bt_dm_2x_qld_only_dd30_vol25", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 12.23% / Sharpe 0.53 / MaxDD -36.47%.",
    },
    "DM DD20 + TMF defensive": {
        "fn": "bt_dm_dd20_tmf_defensive", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued after 2026-05-12 contender review. Metrics: CAGR 18.45% / Sharpe 0.51 / MaxDD -67.81%.",
    },

    # ─── Candidate-review discontinued 2026-05-12 ───
    "Keller PAA (Protective Asset Allocation)": {
        "fn": "bt_keller_paa", "needs": ["returns", "prices"],
        "earliest": "1988-01-04", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: Sharpe is great (0.77) but CAGR too low (9.14%). Might be useful in retirement / withdrawal phase, not for accumulation portfolio. Metrics: CAGR 9.14% / Sharpe 0.77 / MaxDD -21.64%.",
    },
    "DM 1× SPY/EFA/BND vol-15": {
        "fn": "bt_dm_1x_spy_vol_target", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: SPY-driven alpha not strong enough. Dual Momentum signal dominated by SPY which makes it functionally too similar to SPY 1× exposure. Metrics: CAGR 11.44% / Sharpe 0.79 / MaxDD -21.32%.",
    },
    "NTSX + KMLM 80/20": {
        "fn": "bt_ntsx_kmlm_80_20", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: Buy-and-hold essentially same profile but with worse drawdowns and lower Sharpe than alternatives. Not enough differentiation. Metrics: CAGR 10.71% / Sharpe 0.70 / MaxDD -30.91%.",
    },
    "🥇 Gold: NTSD core+satellite": {
        "fn": "bt_ntsd_core_satellite", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: Not convinced of 60/40 core+satellite setup. -49% MaxDD is wider than what Bronze achieves for similar CAGR. Metrics: CAGR 15.54% / Sharpe 0.65 / MaxDD -48.55%.",
    },
    "AAA Free 2× (SAA/EET/UBT/UST/UGL/DBC)": {
        "fn": "bt_aaa_free_2x", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: Performance not good enough. CAGR 13.92% / Sharpe 0.65 — Bronze + AAA Free 2× + NTSD both beat this profile. Metrics: CAGR 13.92% / Sharpe 0.65 / MaxDD -31.61%.",
    },
    "NTSD + 200-SMA (Faber)": {
        "fn": "bt_ntsd_sma200_trend", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: Performance not good enough. NTSD with single trend filter underperforms multi-asset NTSD variants. Metrics: CAGR 11.65% / Sharpe 0.50 / MaxDD -46.94%.",
    },
    "🥈 Silver: NTSD AAA multi-horizon (1m/3m/6m/12m)": {
        "fn": "bt_ntsd_aaa_multi_horizon", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: Strong profile but compared to Bronze it doesn't have enough CAGR. Bronze's 6m-only signal achieves higher CAGR (14.28% vs 13.19%) with similar Sharpe. Metrics: CAGR 13.19% / Sharpe 0.66 / MaxDD -29.22%.",
    },

    # ─── Deployed→Discontinued 2026-05-12 ───
    "RSSB/WTIP": {
        "fn": "bt_rssb_wtip", "needs": ["returns"],
        "earliest": "2001-07-14", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: poor performance. CAGR 7.92% / Sharpe 0.39 / MaxDD -46.41% — both absolute return and risk-adjusted profile inadequate vs deployed alternatives. RSSB and WTIP positions will be liquidated when market reopens.",
    },

    # ─── Candidate-tier cleanup 2026-05-12 (AAA Free 2× + NTSD promoted; rest discontinued) ───
    "🥉 Bronze: NTSD AAA top-2 vol30": {
        "fn": "bt_ntsd_ubt_ugl_dbc_3x_overlay", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "Superseded by AAA Free 2× + NTSD (promoted to DEPLOYED 2026-05-12). Bronze is strictly dominated on CAGR (14.28% vs 15.97%) and Sharpe (0.72 vs 0.74) by AAA Free 2× + NTSD with comparable MaxDD. Metrics: CAGR 14.28% / Sharpe 0.72 / MaxDD -26.85%.",
    },
    "DM 2× best-of-4 (+GLD)": {
        "fn": "bt_dm_2x_best_of_4_gold_dd30_vol25", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: kept current production DM 2× best-of-3 instead. Wider universe DM variants add complexity without sufficient improvement over deployed DM. Metrics: CAGR 17.36% / Sharpe 0.65 / MaxDD -54.16%.",
    },
    "DM 2× best-of-5 (+gold)": {
        "fn": "bt_dm_2x_best_of_5_gold_dd30_vol25", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: same family as DM best-of-4 (+GLD); kept current production DM. Metrics: CAGR 17.12% / Sharpe 0.63 / MaxDD -54.16%.",
    },
    "AAA Free 3× + NTSD (3× variant)": {
        "fn": "bt_aaa_free_3x_plus_ntsd", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: too much leverage — exceeds ≤2× leverage rule. Metrics: CAGR 17.25% / Sharpe 0.70 / MaxDD -31.45%.",
    },
    "AAA 2× + DD30 + vol25 (uses SPUU/EFO)": {
        "fn": "bt_adaptive_asset_allocation_levered", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: ticker conflict with production DM (which already uses SPUU/EFO). Concentration risk if both deployed. Metrics: CAGR 15.33% / Sharpe 0.70 / MaxDD -31.56%.",
    },
    "DM 2× best-of-4 (+SAA)": {
        "fn": "bt_dm_2x_best_of_4_dd30_vol25", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: kept current production DM instead. Small-cap variant doesn't justify added complexity. Metrics: CAGR 17.43% / Sharpe 0.65 / MaxDD -39.20%.",
    },
    "DM 2× best-of-5 (+EET)": {
        "fn": "bt_dm_2x_best_of_5_dd30_vol25", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: same — production DM is the chosen DM strategy. EM rotation adds whipsaw risk. Metrics: CAGR 16.79% / Sharpe 0.61 / MaxDD -39.20%.",
    },
    "DM 2× best-of-3 + vol-30": {
        "fn": "bt_dm_2x_best_of_3_dd30_vol30", "needs": ["returns", "prices"],
        "earliest": "1987-01-02", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: production DM uses vol-25 which is the chosen risk profile. Wider vol target produces wider drawdowns. Metrics: CAGR 18.69% / Sharpe 0.61 / MaxDD -45.75%.",
    },
    "NTSD cross-asset DM": {
        "fn": "bt_ntsd_cross_asset_dm", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: superseded by AAA Free 2× + NTSD (multi-asset rotation with broader universe + better risk control). Metrics: CAGR 15.42% / Sharpe 0.65 / MaxDD -33.80%.",
    },
    "Leveraged All-Weather UPRO/UBT/UGL/DBC": {
        "fn": "bt_leveraged_all_weather", "needs": ["returns"],
        "earliest": "2006-08-01", "tested": "earlier", "discontinued": "2026-05-12",
        "reason": "User decision: static Dalio-style risk parity less appealing than dynamic AAA rotation. Replaced by AAA Free 2× + NTSD for the multi-asset diversifier slot. Metrics: CAGR 13.45% / Sharpe 0.58 / MaxDD -46.69%.",
    },

    # ─── Deployed→Discontinued 2026-05-12: Regime World ───
    "Regime World (WLDU/USFR)": {
        "fn": "bt_regime_world", "needs": ["returns", "prices", "vix", "fed"],
        "earliest": "1990-10-02", "tested": "deployed-since-2026-04", "discontinued": "2026-05-12",
        "reason": "User decision: discontinued as part of WLDU portfolio re-evaluation. Regime World had CAGR 11.47% / Sharpe 0.55 / MaxDD -29.5% — solid risk profile but the 7-signal composite engine on URTH was not producing differentiated value vs simpler WLDU implementations. Replaced by exploration of 21 alternative WLDU-based strategies (Global HFEA variants, trend-managed WLDU, AAA-global, DM rotation, etc.).",
    },

    # ─── WLDU exploration failures 2026-05-12 (Sharpe<0.5 OR CAGR<11%) ───
    "🌐 Global HFEA Diversified 4-asset (35/25/20/20)": {
        "fn": "bt_wldu_diversified_4asset", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (WLDU exploration failure): CAGR 10.66% < 11%. Full metrics: CAGR 10.66% / Sharpe 0.54 / MaxDD -40.13%.",
    },
    "🌐 WLDU AAA top-3 (5-asset universe)": {
        "fn": "bt_wldu_aaa_top3_dd30_vol25", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (WLDU exploration failure): CAGR 10.46% < 11% AND Sharpe 0.48 < 0.5. Full metrics: CAGR 10.46% / Sharpe 0.48 / MaxDD -30.57%.",
    },
    "🌐 Leveraged Global Permanent Portfolio": {
        "fn": "bt_wldu_permanent_portfolio", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (WLDU exploration failure): CAGR 8.20% < 11% AND Sharpe 0.47 < 0.5. Full metrics: CAGR 8.20% / Sharpe 0.47 / MaxDD -35.38%.",
    },
    "🌐 Leveraged Global All-Weather (Dalio)": {
        "fn": "bt_wldu_all_weather", "needs": ["returns"],
        "earliest": "2006-08-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (WLDU exploration failure): CAGR 9.07% < 11% AND Sharpe 0.44 < 0.5. Full metrics: CAGR 9.07% / Sharpe 0.44 / MaxDD -47.11%.",
    },
    "🌐 WLDU + 200-SMA gate": {
        "fn": "bt_wldu_sma200_gate", "needs": ["returns", "prices"],
        "earliest": "1991-10-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (WLDU exploration failure): Sharpe 0.42 < 0.5. Full metrics: CAGR 11.39% / Sharpe 0.42 / MaxDD -40.91%.",
    },
    "🌐 Global HFEA Gold (45/25/30 WLDU/TMF/UGL)": {
        "fn": "bt_global_hfea_gold", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (WLDU exploration failure): CAGR 10.46% < 11% AND Sharpe 0.42 < 0.5. Full metrics: CAGR 10.46% / Sharpe 0.42 / MaxDD -50.54%.",
    },
    "🌐 Global HFEA Classic (55/45 WLDU/TMF)": {
        "fn": "bt_global_hfea_classic", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (WLDU exploration failure): Sharpe 0.41 < 0.5. Full metrics: CAGR 11.33% / Sharpe 0.41 / MaxDD -65.60%.",
    },
    "🌐 WLDU cross-asset DM": {
        "fn": "bt_wldu_cross_asset_dm", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (WLDU exploration failure): Sharpe 0.31 < 0.5. Full metrics: CAGR 12.71% / Sharpe 0.31 / MaxDD -52.96%.",
    },
    "🌐 WLDU + vol-target 20%": {
        "fn": "bt_wldu_vol_target_20", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (WLDU exploration failure): CAGR 8.22% < 11% AND Sharpe 0.31 < 0.5. Full metrics: CAGR 8.22% / Sharpe 0.31 / MaxDD -65.67%.",
    },
    "🌐 WLDU + vol-target 25%": {
        "fn": "bt_wldu_vol_target_25", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (WLDU exploration failure): CAGR 8.35% < 11% AND Sharpe 0.26 < 0.5. Full metrics: CAGR 8.35% / Sharpe 0.26 / MaxDD -73.99%.",
    },
    "🌐 WLDU + DD-30 stop": {
        "fn": "bt_wldu_dd30_stop", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Auto-discontinued 2026-05-12 (WLDU exploration failure): CAGR 7.15% < 11% AND Sharpe 0.17 < 0.5. Full metrics: CAGR 7.15% / Sharpe 0.17 / MaxDD -83.58%.",
    },

    # ─── WLDU exploration shutdown 2026-05-12 (entire batch discontinued by user) ───
    "🌐 DM best-of-3: WLDU/QLD/EFO": {
        "fn": "bt_wldu_qld_efo_dm", "needs": ["returns", "prices"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "DM rotation strategy that duplicates WLDU's internal equity exposure. WLDU = 2× MSCI World which already contains S&P 500, Nasdaq weight, and EFA — rotating among WLDU/SPUU/QLD/EFO is effectively concentrating in past winners of the same underlying index. Not a genuine diversifier. Metrics: CAGR 18.84% / Sharpe 0.73 / MaxDD -33.60%.",
    },
    "🌐 DM best-of-4: WLDU/SPUU/QLD/EFO": {
        "fn": "bt_wldu_spuu_qld_efo_dm", "needs": ["returns", "prices"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "DM rotation strategy that duplicates WLDU's internal equity exposure. WLDU = 2× MSCI World which already contains S&P 500, Nasdaq weight, and EFA — rotating among WLDU/SPUU/QLD/EFO is effectively concentrating in past winners of the same underlying index. Not a genuine diversifier. Metrics: CAGR 18.19% / Sharpe 0.69 / MaxDD -34.10%.",
    },
    "🌐 WLDU AAA top-2 (WLDU/UBT/UGL/DBC)": {
        "fn": "bt_wldu_aaa_top2_dd25_vol20", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Design rejected as part of the 2026-05-12 WLDU exploration sweep. User decided to discontinue all 10 candidates from this batch and move on. Metrics: CAGR 13.91% / Sharpe 0.67 / MaxDD -28.80%.",
    },
    "🌐 DM best-of-3: WLDU/SPUU/QLD": {
        "fn": "bt_wldu_spuu_qld_dm", "needs": ["returns", "prices"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "DM rotation strategy that duplicates WLDU's internal equity exposure. WLDU = 2× MSCI World which already contains S&P 500, Nasdaq weight, and EFA — rotating among WLDU/SPUU/QLD/EFO is effectively concentrating in past winners of the same underlying index. Not a genuine diversifier. Metrics: CAGR 15.67% / Sharpe 0.64 / MaxDD -33.60%.",
    },
    "🌐 Hybrid Global+US HFEA (30/15/25/30)": {
        "fn": "bt_wldu_upro_hybrid", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "HFEA-style static rebalanced portfolio with WLDU as equity sleeve. User already runs an HFEA-like portfolio with the world ETF in another account — duplicating that pattern here adds no value. Metrics: CAGR 13.28% / Sharpe 0.64 / MaxDD -40.40%.",
    },
    "🌐 Global HFEA Modern (45/25/30 WLDU/TMF/KMLM)": {
        "fn": "bt_global_hfea_modern", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "HFEA-style static rebalanced portfolio with WLDU as equity sleeve. User already runs an HFEA-like portfolio with the world ETF in another account — duplicating that pattern here adds no value. Metrics: CAGR 11.53% / Sharpe 0.59 / MaxDD -38.80%.",
    },
    "🌐 Global HFEA UBT (50/25/25 WLDU/UBT/KMLM)": {
        "fn": "bt_global_hfea_ubt", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "HFEA-style static rebalanced portfolio with WLDU as equity sleeve. User already runs an HFEA-like portfolio with the world ETF in another account — duplicating that pattern here adds no value. Metrics: CAGR 11.28% / Sharpe 0.58 / MaxDD -43.80%.",
    },
    "🌐 WLDU core+satellite (60% WLDU + 40% rotate)": {
        "fn": "bt_wldu_core_satellite", "needs": ["returns", "prices"],
        "earliest": "2006-08-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Design rejected as part of the 2026-05-12 WLDU exploration sweep. User decided to discontinue all 10 candidates from this batch and move on. Metrics: CAGR 16.12% / Sharpe 0.56 / MaxDD -61.40%.",
    },
    "🌐 Global HFEA Bond-Light (50/30/20)": {
        "fn": "bt_global_hfea_bond_light", "needs": ["returns"],
        "earliest": "1988-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "HFEA-style static rebalanced portfolio with WLDU as equity sleeve. User already runs an HFEA-like portfolio with the world ETF in another account — duplicating that pattern here adds no value. Metrics: CAGR 11.63% / Sharpe 0.53 / MaxDD -47.70%.",
    },
    "🌐 WLDU + 255-SMA gate": {
        "fn": "bt_wldu_sma255_gate", "needs": ["returns", "prices"],
        "earliest": "1991-12-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Design rejected as part of the 2026-05-12 WLDU exploration sweep. User decided to discontinue all 10 candidates from this batch and move on. Metrics: CAGR 13.83% / Sharpe 0.53 / MaxDD -40.00%.",
    },

    # ─── R1 + R6 discontinued after Wave 4 backtest 2026-05-12 ───
    "🌐 R1: WLDU + KMLM 60/40 vol-targeted": {
        "fn": "bt_wldu_kmlm_voltarget", "needs": ["returns"],
        "earliest": "1992-01-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed quality bar: CAGR 8.09% / Sharpe 0.49. The 12% vol-target was too aggressive — base WLDU+KMLM blend has ~17-18% vol so the strategy scaled exposure down to ~65-70%, capping returns. Replaced by R1b (static, no vol target) and R1c (looser 18% vol target).",
    },
    "🌐 R6: WLDU + EM stack (70/30 WLDU/EET)": {
        "fn": "bt_wldu_em_stack", "needs": ["returns"],
        "earliest": "2004-04-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed quality bar dramatically: CAGR 10.50% / Sharpe 0.22 / MaxDD -87.45%. Stacking two 2× leveraged equity sleeves (WLDU + EET) with correlated drawdowns (2008 GFC, 2015-16 China devaluation) was catastrophic. The forward GMO/EM-value thesis doesn't survive a historical backtest. Would need redesign with unleveraged EM (EEM 1× instead of EET 2×) to be viable.",
    },
    "🌐 R1b: WLDU + KMLM 60/40 (static, no vol target)": {
        "fn": "bt_wldu_kmlm_static", "needs": ["returns"],
        "earliest": "1992-01-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Barely cleared quality bar: CAGR 11.30% / Sharpe 0.50 / MaxDD -54.49%. Despite passing the gates, user discontinued in favor of focusing on R3 (the stronger 16.59%/0.54 performer). The static 60/40 thesis is intact but doesn't add enough vs simpler alternatives to justify a slot.",
    },
    "🌐 R1c: WLDU + KMLM 60/40 vol-target 18%": {
        "fn": "bt_wldu_kmlm_voltarget18", "needs": ["returns"],
        "earliest": "1992-01-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed quality bar: CAGR 9.35% / Sharpe 0.46. Loosening R1's vol-target from 12% to 18% still over-damped — the 18% target is right at the blend's natural vol so the gate triggers often enough to clip returns without meaningfully tightening drawdown vs R1b static. Confirms vol-targeting WLDU+KMLM is a dead end.",
    },
    "🌐 R3: WLDU GEM-rotation (Antonacci US-vs-Intl)": {
        "fn": "bt_wldu_gem_rotation", "needs": ["returns", "prices"],
        "earliest": "1971-01-01", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Two reasons: (1) SPUU overlap — R3's US-wins branch holds SPUU, which is already part of deployed DM 2× best-of-3 (SPUU/QLD/EFO). When DM is also in SPUU the exposure double-counts. (2) Weak risk profile — Sharpe 0.54 / MaxDD -59.35% don't justify a new sleeve; the 2× leverage on the US side blows out drawdowns when the 12m momentum signal flips late (notably 2020 COVID -58.91% stress reading).",
    },

    # ─── Wave 5 failures 2026-05-12 (10 strategies) ───
    "🌐 C1: WLDU AAA top-2 (WLDU/UBT/UGL/DBC)": {
        "fn": "bt_wldu_aaa_top2_dd25_vol20", "needs": ["returns", "prices"],
        "earliest": "1988-01-04", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed both gates: CAGR 9.25% / Sharpe 0.44 / MaxDD -48.6%. Revival of bundled-rejected design did NOT reproduce prior metrics (13.91%/0.67/-28.8%) — data layer has been updated since (KMLM splice, SPYSIM refresh), so the strategy's earlier strong reading was stale. Top-2 momentum rotation over WLDU/UBT/UGL/DBC is no longer competitive against simpler static blends.",
    },
    "🌐 C5: WLDU+WTIP (50/50)": {
        "fn": "bt_w5_c5_wldu_wtip", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed both gates: CAGR 9.62% / Sharpe 0.38 / MaxDD -68.2%. Pure WLDU+WTIP barbell didn't deliver — WTIP's 1.88× notional with TIPS+commodities+gold+BTC produced too much vol drag without proportional return.",
    },
    "🌐 C6: WLDU+WTIP+KMLM (50/25/25)": {
        "fn": "bt_w5_c6_wldu_wtip_kmlm", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed CAGR gate: CAGR 10.57% / Sharpe 0.50 / MaxDD -55.7%. Triple diversifier was right at Sharpe bar but CAGR fell short. WTIP drag confirmed across all C5-C7 variants.",
    },
    "🌐 C7: WLDU+WTIP+UGL (50/25/25)": {
        "fn": "bt_w5_c7_wldu_wtip_ugl", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed both gates: CAGR 10.74% / Sharpe 0.42 / MaxDD -62.6%. All-inflation-defense design without bond duration produced wide drawdowns.",
    },
    "🌐 C10: WLDU/UBT/UGL/DBC inverse-vol (monthly)": {
        "fn": "bt_w5_c10_wldu_ubt_ugl_dbc_invvol", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed both gates: CAGR 7.90% / Sharpe 0.44 / MaxDD -37.9%. Pure inverse-vol weighting without momentum produced too-defensive exposure — CAGR floor too low for an aggressive sleeve.",
    },
    "🌐 C11: WLDU/UBT/KMLM/UGL inverse-vol (monthly)": {
        "fn": "bt_w5_c11_wldu_ubt_kmlm_ugl_invvol", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed CAGR gate by 1.6pp: CAGR 9.37% / Sharpe 0.65 / MaxDD -23.2%. Best MaxDD and Sharpe near 0.65 — defensive standout — but CAGR too low. Worth noting: if the bar is relaxed, this is the lowest-risk WLDU candidate from the batch.",
    },
    "🌐 C12: WLDU + DD20 → WTIP defensive": {
        "fn": "bt_w5_c12_wldu_dd20_wtip", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Catastrophic: CAGR 4.92% / Sharpe 0.11 / MaxDD -78.0%. DD-stop logic on 2× equity creates whipsaw — the 63-day recovery window misses re-entry rallies and the WTIP defensive sleeve adds vol without protection.",
    },
    "🌐 C13: WLDU + DD25 → UBT defensive": {
        "fn": "bt_w5_c13_wldu_dd25_ubt", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Catastrophic: CAGR 6.04% / Sharpe 0.14 / MaxDD -72.4%. Same DD-stop whipsaw failure mode as C12 — switching to UBT during drawdowns didn't help; the equity sleeve missed the post-stop recovery.",
    },
    "🌐 C14: WLDU + VIX regime (≥25 → 50/50 WLDU/UBT)": {
        "fn": "bt_w5_c14_wldu_vix_regime", "needs": ["returns", "vix"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Catastrophic: CAGR 10.30% / Sharpe 0.25 / MaxDD -85.0%. Single-VIX-signal gate is far inferior to Regime SSO's 6-signal composite — VIX-25 threshold is too coarse and the 50/50 defensive allocation isn't defensive enough during GFC-scale drawdowns.",
    },
    "🌐 C15: WLDU AAA top-3 (5-asset, adds KMLM)": {
        "fn": "bt_w5_c15_wldu_aaa_top3_5asset", "needs": ["returns", "prices"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed CAGR gate: CAGR 9.72% / Sharpe 0.53 / MaxDD -30.2%. Adding KMLM to AAA universe and going top-3 didn't improve over C1 baseline; momentum-based rotation pattern is underperforming simple static blends in this WLDU context.",
    },

    # ─── Wave 5/6 redundant or dominated 2026-05-12 (9 strategies) ───
    # All passed the bar but were superseded by stronger shortlist members.
    "🌐 C2: WLDU+UBT+KMLM (50/30/20)": {
        "fn": "bt_w5_c2_wldu_ubt_kmlm", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Passed bar (11.24% / 0.58 / -44.2%) but dominated by C9 (same KMLM+duration structure, better Sharpe via TYD intermediate Treasury). UBT 2× long-duration adds drawdown without commensurate return.",
    },
    "🌐 C3: WLDU+KMLM+UGL (50/30/20)": {
        "fn": "bt_w5_c3_wldu_kmlm_ugl", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Passed bar (11.53% / 0.55 / -47.7%) but no-bonds variant has too wide a MaxDD. Replacing duration with gold+MF didn't survive stress periods.",
    },
    "🌐 C4: WLDU+UBT+UGL+KMLM (40/30/15/15)": {
        "fn": "bt_w5_c4_wldu_ubt_ugl_kmlm", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Passed bar (11.10% / 0.60 / -38.4%) — best 4-asset static blend with FULL 34-year window. Dominated by D8 on every dimension: D8 has higher CAGR (14.51% vs 11.10%), higher Sharpe (0.71 vs 0.60), and tighter MaxDD. The intermediate-Treasury (TYD) sleeve in D8 outperforms UBT 2× long-Treasury.",
    },
    "🌐 C8: WLDU+TYD (50/50)": {
        "fn": "bt_w5_c8_wldu_tyd", "needs": ["returns"],
        "earliest": "2009-04-16", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Passed bar (13.01% / 0.60 / -47%) but dominated by C9 — adding KMLM (3rd leg) keeps Sharpe similar with materially tighter MaxDD (-33% vs -47%).",
    },
    "🌐 D1: WLDU+TYD+DBMF (50/30/20)": {
        "fn": "bt_w6_d1_wldu_tyd_dbmf", "needs": ["returns"],
        "earliest": "2019-05-08", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed Sharpe gate: CAGR 12.69% / Sharpe 0.56 / MaxDD -35.0%. DBMF substitute for KMLM produced lower Sharpe in the 2019+ window. MF replicator choice is not the alpha source — KMLM is fine.",
    },
    "🌐 D2: WLDU+TYD+KMLM+DBMF (50/30/10/10)": {
        "fn": "bt_w6_d2_wldu_tyd_kmlm_dbmf", "needs": ["returns"],
        "earliest": "2019-05-08", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed Sharpe gate: CAGR 12.36% / Sharpe 0.56. Splitting MF allocation across KMLM + DBMF didn't add manager-diversification benefit — both replicators move together.",
    },
    "🌐 D3: WLDU+TLT+KMLM (50/30/20)": {
        "fn": "bt_w6_d3_wldu_tlt_kmlm", "needs": ["returns"],
        "earliest": "2002-07-30", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed Sharpe gate: CAGR 11.41% / Sharpe 0.59 / MaxDD -47.8%. TLT unleveraged duration produced wider MaxDD than leveraged variants — the 3× in TYD is contributing return that you need at this weight.",
    },
    "🌐 D4: WLDU+EDV+KMLM (50/30/20)": {
        "fn": "bt_w6_d4_wldu_edv_kmlm", "needs": ["returns"],
        "earliest": "2007-12-06", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed both gates: CAGR 9.89% / Sharpe 0.47 / MaxDD -44.9%. EDV zero-coupon duration is TOO conservative for an aggressive WLDU sleeve — the leveraged duration in TYD/UBT was contributing return, not just decay.",
    },
    "🌐 D10: WLDU+EDV+KMLM+UGL (45/25/20/10)": {
        "fn": "bt_w6_d10_wldu_edv_kmlm_ugl", "needs": ["returns"],
        "earliest": "2007-12-06", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed CAGR gate: CAGR 10.53% / Sharpe 0.52 / MaxDD -41.1%. EDV variant of D8 — same gold-diversification benefit but EDV duration underperforms TYD, so the strategy lands below the bar even with the longer 2007+ window.",
    },

    # ─── 2026-05-12 — Six W5/W6 shortlist disqualified by user constraint
    # update: (1) per-ticker leverage cap raised to ≤2× (TYD is 3× IEF),
    # and (2) zero overlap with deployed tickers (KMLM in HFEA-fixed; UGL in
    # AAA rotation). All six candidates touched at least one disqualified
    # ticker. Backtest metrics retained for reference. ───
    "🌐 D8: WLDU+TYD+KMLM+UGL (50/25/15/10)": {
        "fn": "bt_w6_d8_wldu_tyd_kmlm_ugl", "needs": ["returns"],
        "earliest": "2009-04-16", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Disqualified by rule update: TYD is 3× (violates ≤2× per-ticker rule), KMLM is in deployed HFEA (zero-overlap rule), UGL is in deployed AAA rotation. Metrics for reference: 14.51% / 0.71 / -34.6% (would have been the shortlist leader).",
    },
    "🌐 D7: WLDU+TYD+GLD (50/30/20)": {
        "fn": "bt_w6_d7_wldu_tyd_gld", "needs": ["returns"],
        "earliest": "2009-04-16", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Disqualified by rule update: TYD is 3× IEF (violates ≤2× per-ticker rule). Otherwise had zero deployed-ticker overlap. Metrics for reference: 14.60% / 0.70 / -40.7%.",
    },
    "🌐 D6: WLDU+TYD+UGL (50/30/20)": {
        "fn": "bt_w6_d6_wldu_tyd_ugl", "needs": ["returns"],
        "earliest": "2009-04-16", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Disqualified by rule update: TYD is 3× (≤2× rule), UGL is in deployed AAA rotation (zero-overlap rule). Metrics for reference: 15.63% / 0.70 / -42.7% (highest CAGR of W6 shortlist).",
    },
    "🌐 D9: WLDU+TYD+KMLM+GLD+SLV (50/25/15/5/5)": {
        "fn": "bt_w6_d9_wldu_tyd_kmlm_gld_slv", "needs": ["returns"],
        "earliest": "2009-04-16", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Disqualified by rule update: TYD is 3× (≤2× rule), KMLM is in deployed HFEA fixed 30% (zero-overlap rule). Metrics for reference: 14.07% / 0.69 / -34.1%.",
    },
    "🌐 C9: WLDU+TYD+KMLM (50/30/20)": {
        "fn": "bt_w5_c9_wldu_tyd_kmlm", "needs": ["returns"],
        "earliest": "2009-04-16", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Disqualified by rule update: TYD is 3× (≤2× rule), KMLM is in deployed HFEA fixed 30% (zero-overlap rule). Metrics for reference: 13.21% / 0.67 / -33% (W5/W6 shortlist baseline).",
    },
    "🌐 D5: WLDU+EDV+TYD+KMLM (40/25/15/20)": {
        "fn": "bt_w6_d5_wldu_edv_tyd_kmlm", "needs": ["returns"],
        "earliest": "2009-04-16", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Disqualified by rule update: TYD is 3× (≤2× rule), KMLM is in deployed HFEA fixed 30% (zero-overlap rule). Metrics for reference: 11.44% / 0.67 / -33.0%.",
    },

    # ─── 2026-05-12 — Wave 7/8 candidates dominated by F4/E7/F2 shortlist.
    # All passed leverage and overlap rules but were beaten on Sharpe / CAGR
    # by the three survivors. Metrics from the combined W7+W8 run with
    # DBMFSIM-extended DBMF history. ───
    "🌐 E1: WLDU+TLT+GLD (50/30/20)": {
        "fn": "bt_w7_e1_wldu_tlt_gld", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed CAGR gate by 0.5pp: 10.49% / 0.52 / -52.1% over 34-year window. Pure 1× baseline without capital-efficient stacks doesn't generate enough notional.",
    },
    "🌐 E2: WLDU+EDV+GLD (50/30/20)": {
        "fn": "bt_w7_e2_wldu_edv_gld", "needs": ["returns"],
        "earliest": "2007-12-06", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed both gates: 10.87% / 0.48 / -49.1%. EDV duration alone doesn't fix the low-notional problem.",
    },
    "🌐 E3: WLDU+TLT+DBMF (50/30/20)": {
        "fn": "bt_w7_e3_wldu_tlt_dbmf", "needs": ["returns"],
        "earliest": "2000-01-31", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed both gates: 9.04% / 0.43 / -52.2%. Pure DBMF without gold drags through dot-com and GFC; the W7-only 7-year window flattered MF performance.",
    },
    "🌐 E4: WLDU+EDV+DBMF (50/30/20)": {
        "fn": "bt_w7_e4_wldu_edv_dbmf", "needs": ["returns"],
        "earliest": "2000-01-31", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed both gates: 9.78% / 0.47 / -49.6%. Same DBMF-without-gold problem as E3, with EDV instead of TLT.",
    },
    "🌐 E5: WLDU+TLT+DBMF+GLD (50/25/15/10)": {
        "fn": "bt_w7_e5_wldu_tlt_dbmf_gld", "needs": ["returns"],
        "earliest": "2000-01-31", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed both gates: 9.51% / 0.45 / -52.3%. Even with gold added, the 50/25/15/10 split with 1× duration and 1× MF can't clear the bar over 26 years.",
    },
    "🌐 E6: WLDU+EDV+DBMF+GLD (50/25/15/10)": {
        "fn": "bt_w7_e6_wldu_edv_dbmf_gld", "needs": ["returns"],
        "earliest": "2000-01-31", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed both gates: 10.15% / 0.49 / -50.1%. EDV+DBMF+GLD doesn't generate enough CAGR at low notional.",
    },
    "🌐 E8: WLDU+RSSB+GLD (40/40/20)": {
        "fn": "bt_w7_e8_wldu_rssb_gld", "needs": ["returns"],
        "earliest": "2002-07-30", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed Sharpe gate: 12.02% / 0.49 / -58.7%. RSSB's internal global-stocks exposure overlaps WLDU economically — too much equity beta, drove the -58% MaxDD.",
    },
    "🌐 E9: WLDU+NTSX+GLD (40/40/20)": {
        "fn": "bt_w7_e9_wldu_ntsx_gld", "needs": ["returns"],
        "earliest": "1992-01-02", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed Sharpe gate: 11.36% / 0.49 / -59.3%. NTSX's US-stocks exposure adds correlation without enough diversification benefit.",
    },
    "🌐 E10: WLDU+GDE+DBMF (40/30/30)": {
        "fn": "bt_w7_e10_wldu_gde_dbmf", "needs": ["returns"],
        "earliest": "2000-01-31", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Cleared bar but barely (11.63% / 0.50 / -55.5% over 26y) — dominated by F4 (better Sharpe 0.68, tighter MaxDD). Original W7-only result was 22.23% / 0.91 — that was a regime-flattery artifact of the 7-year DBMF-only window. DBMFSIM correction made the true picture visible.",
    },
    "🌐 F1: WLDU+NTSI+GLD (40/40/20)": {
        "fn": "bt_w8_f1_wldu_ntsi_gld", "needs": ["returns"],
        "earliest": "2002-07-30", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed Sharpe gate: 12.17% / 0.49 / -61.5%. NTSI's intl-equity overlap with WLDU produced too much equity concentration. Pure-intl thesis didn't survive the equity beta concentration.",
    },
    "🌐 F3: WLDU+RSIT+GLD (40/40/20)": {
        "fn": "bt_w8_f3_wldu_rsit_gld", "needs": ["returns"],
        "earliest": "2002-07-30", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Passed bar but dominated: 13.29% / 0.53 / -59.4% over 24y. F4's GOLY-based design (gold+MF+credit triple-stack) delivers better Sharpe and tighter MaxDD with similar CAGR.",
    },
    "🌐 F5: WLDU+NTSI+RSIT (40/40/20)": {
        "fn": "bt_w8_f5_wldu_ntsi_rsit", "needs": ["returns"],
        "earliest": "2002-07-30", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Failed Sharpe gate: 11.48% / 0.40 / -67.8%. All-stacks design with internal-equity overlap between WLDU/NTSI/RSIT produced the widest MaxDD of any candidate.",
    },
    "🌐 F6: WLDU+NTSI+GDT (40/30/30)": {
        "fn": "bt_w8_f6_wldu_ntsi_gdt", "needs": ["returns"],
        "earliest": "2002-07-30", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Passed bar but dominated: 12.96% / 0.55 / -57.9% over 24y. F2 delivers the same inflation-defense thesis (via GDT) with better Sharpe (0.63) and tighter MaxDD (-52.9%) by replacing NTSI with TLT (less intl-equity concentration).",
    },

    # ─── 2026-05-12 — Final shortlist runners-up (F4 promoted, E7 + F2 retired) ───
    "🌐 E7: WLDU+GDE+TLT (40/30/30)": {
        "fn": "bt_w7_e7_wldu_gde_tlt", "needs": ["returns"],
        "earliest": "2002-07-30", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Final shortlist runner-up. Highest CAGR (14.32%) but wider MaxDD (-54.2%) and GDE's internal US-equity exposure economically overlaps HFEA's UPRO. F4 promoted instead — same diversification thesis with tighter MaxDD (-43.4%) and cleaner non-overlap profile.",
    },
    "🌐 F2: WLDU+GDT+TLT (50/30/20)": {
        "fn": "bt_w8_f2_wldu_gdt_tlt", "needs": ["returns"],
        "earliest": "2002-07-30", "tested": "2026-05-12", "discontinued": "2026-05-12",
        "reason": "Final shortlist runner-up. Inflation-defense angle via GDT (TIPS+gold stack) but Sharpe 0.63 < F4's 0.68 and MaxDD -52.9% > F4's -43.4%. F4's GOLY triple-stack provides similar inflation defense plus crisis-alpha and credit carry in one ticker.",
    },
}


HISTORIC_STRATEGIES = {
    # All strategies ever tested but not promoted to deployment. Preserved
    # as a universe to re-mine when looking for new candidate ideas.

    # ── NTSD-family experiments (current evaluation round, 2026-05) ──

    # ── Earlier candidate exploration (academic theory survey, 2026-04) ──

    # ── Pre-NTSD Dual Momentum optimization round (2026-03) ──

    # ── Pre-NTSD: leveraged DM variants ──

    # ── Pre-NTSD: SPUU vol-target sweep (2026-03-25) ──

    # ── Pre-NTSD: 2× best-of-N multi-asset (predecessors to production) ──

    # ── Sector Momentum family (removed from production 2026-05) ──

    # ─── RESEARCH WAVE 2026-05-12: HFEA family variants (8) ───

    # ─── Capital-efficient stacks (4) — WisdomTree NTSX-family + GDE ───

    # ─── Risk Parity / All-Weather (4) ───

    # ─── Tactical / Trend-Following (6) ───
    # ─── HFEA Risk-Managed (4) ───
    # ─── TQQQ variants (3) ───
}


# ═══════════════════════════════════════════════════════════════════════
# STRESS WINDOWS — 24 historical regimes covering 1987 → 2026
# ═══════════════════════════════════════════════════════════════════════
# Each tuple: (label, start_date, end_date, plain-English description)
# Used by the stress-period section of the report. Spans every major
# macro event in the 39-year sample.
STRESS_WINDOWS = [
    ("1973-74 Oil shock + stagflation bear", "1973-01-01", "1974-12-31",
        "SPY -48% peak-to-trough over 21 months. Oil embargo + recession."),
    ("1976-80 Inflationary bull", "1976-01-01", "1980-12-31",
        "Gold +600%, SPY +30% nominal but flat real. Pre-Volcker monetary expansion."),
    ("1980 Volcker shock", "1980-02-01", "1980-04-30",
        "Sharp recession + silver corner blowoff. Fed funds 20%."),
    ("1981-82 Volcker recession", "1981-08-01", "1982-08-12",
        "SPY -27%, deepest post-war recession. Disinflation regime change."),
    ("1987 Black Monday + aftermath", "1987-08-01", "1987-12-31",
        "SPY -22% on Oct 19. Full peak-trough -33% over 100 days."),
    ("1990 recession / Gulf War", "1990-07-01", "1990-10-31",
        "Iraq invasion of Kuwait + recession. SPY -20%."),
    ("1994 bond market massacre", "1994-02-01", "1994-12-31",
        "Greenspan tightening shock. Long bonds -10%, worst since 1981."),
    ("1995-2000 dot-com melt-up", "1995-01-01", "2000-03-24",
        "Tech-led bubble. NASDAQ +600%, SPY +245%, gold flat."),
    ("1997 Asian Financial Crisis", "1997-07-01", "1998-01-31",
        "EM crash, flight to quality. SPY +5%, EM -40%, gold spiked."),
    ("1998 LTCM / Russian default", "1998-08-01", "1998-10-31",
        "Credit blowout, vol explosion. SPY -19% before V-recovery."),
    ("2000 dot-com peak (Mar)", "2000-03-01", "2000-09-30",
        "First leg down. NASDAQ -45%."),
    ("2000-2002 dot-com bear (full)", "2000-03-24", "2002-10-09",
        "30 months. SPY -49%, NASDAQ -78% peak-to-trough."),
    ("2001 9/11 shock", "2001-09-10", "2001-09-30",
        "Markets closed 9/11-9/17. Reopened -7%."),
    ("2003-07 recovery + housing bull", "2003-01-01", "2007-10-08",
        "5-year bull. SPY +95%."),
    ("2007-09 GFC peak-to-trough", "2007-10-09", "2009-03-09",
        "17 months. SPY -55%. Worst since 1930s."),
    ("2009 GFC initial recovery", "2009-03-09", "2009-12-31",
        "V-shape recovery. SPY +65% from March bottom."),
    ("2011 European debt + US downgrade", "2011-05-01", "2011-10-04",
        "Greece + S&P downgrade. SPY -19%, gold spike."),
    ("2013 Taper tantrum", "2013-05-01", "2013-09-30",
        "Bernanke 'taper' speech. Long bonds -10%."),
    ("2014-16 Oil crash", "2014-09-01", "2016-02-11",
        "Crude $107 → $26. Commodities devastated, DBC -50%."),
    ("2015-16 China devaluation", "2015-08-01", "2016-02-11",
        "Yuan devaluation + EM rout. SPY -14% intra-period."),
    ("2018 Q4 Powell pivot", "2018-10-01", "2018-12-26",
        "SPY -20% on Fed tightening fears."),
    ("2020 COVID crash", "2020-02-19", "2020-03-23",
        "Fastest -34% in SPY history. 23 trading days."),
    ("2020 COVID recovery", "2020-03-23", "2020-12-31",
        "V-shape recovery. SPY +71% off the bottom."),
    ("2022 inflation/rates shock", "2022-01-01", "2022-10-12",
        "Worst stocks+bonds year on record. SPY -25%, AGG -16%."),
    ("2023 Q1 banking crisis", "2023-03-01", "2023-05-15",
        "SVB collapse, Credit Suisse takeover."),
    ("2023 AI rally year", "2023-01-01", "2023-12-31",
        "ChatGPT-driven tech rally. SPY +26%."),
    ("2024 full year", "2024-01-01", "2024-12-31",
        "Mag 7 AI continues + Fed pivot. SPY +25%."),
    ("2025 YTD", "2025-01-01", "2026-05-09",
        "Gold supercycle + AI infrastructure boom."),
]


# ═══════════════════════════════════════════════════════════════════════
# DATA LAYER (extended history via extended_data.py + Alpaca/EODHD fallback)
# ═══════════════════════════════════════════════════════════════════════

CACHE_DIR = "/tmp/mega_backtest/_cache"
os.makedirs(CACHE_DIR, exist_ok=True)


def alpaca_creds():
    api_key = subprocess.check_output(
        ["gcloud", "secrets", "versions", "access", "latest",
         "--secret=ALPACA_API_KEY_LIVE",
         "--project=trading-436516",
         "--account=cayookenz@gmail.com"],
    ).decode().strip()
    secret = subprocess.check_output(
        ["gcloud", "secrets", "versions", "access", "latest",
         "--secret=ALPACA_SECRET_KEY_LIVE",
         "--project=trading-436516",
         "--account=cayookenz@gmail.com"],
    ).decode().strip()
    return {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret}


def fetch_alpaca_live_equity() -> float | None:
    """
    Fetch current LIVE Alpaca portfolio_value via the same gcloud-secrets
    pattern as alpaca_creds(). Used as the starting value for the retirement
    projection. Returns None on any failure (caller falls back to override).
    """
    try:
        r = requests.get("https://api.alpaca.markets/v2/account",
                         headers=alpaca_creds(), timeout=15)
        r.raise_for_status()
        return float(r.json().get("portfolio_value", 0))
    except Exception as e:
        print(f"  ⚠ Could not fetch live Alpaca equity: {e}")
        return None


def eodhd_token():
    tok = os.environ.get(EODHD_TOKEN_ENV)
    if not tok:
        raise RuntimeError(f"Set {EODHD_TOKEN_ENV} env var with your EODHD API token")
    return tok


def fetch_eodhd_bars(symbol, start, end, asset_type="us"):
    """
    Fetch EOD bars from EODHD.
      asset_type='us' for stocks/ETFs (suffix .US)
      asset_type='cc' for crypto (suffix .CC, e.g. BTC-USD.CC)
    Returns DataFrame indexed by date with adjusted close.
    """
    cache_path = os.path.join(CACHE_DIR, f"eodhd_{symbol.replace('/', '_').replace('-', '_')}.parquet")
    if os.path.exists(cache_path):
        return pd.read_parquet(cache_path)
    suffix = ".CC" if asset_type == "cc" else ".US"
    eod_sym = symbol if symbol.endswith(suffix) else f"{symbol}{suffix}"
    url = f"https://eodhd.com/api/eod/{eod_sym}"
    params = {"api_token": eodhd_token(), "fmt": "json", "from": start, "to": end}
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"  ✗ {symbol}: {e}")
        return None
    if not isinstance(rows, list) or not rows:
        return None
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    df = df.set_index("t").sort_index()
    # EODHD's 'adjusted_close' applies splits + dividends; use as our 'close'.
    keep = {"open": "open", "high": "high", "low": "low", "adjusted_close": "close"}
    df = df[list(keep.keys())].rename(columns=keep)
    df.to_parquet(cache_path)
    return df


def fetch_stock_bars(headers, symbol, start, end):
    """Stock/ETF bar fetcher — routes to EODHD or Alpaca per DATA_SOURCE."""
    if DATA_SOURCE == "eodhd":
        return fetch_eodhd_bars(symbol, start, end, asset_type="us")
    cache_path = os.path.join(CACHE_DIR, f"stock_{symbol.replace('/', '_')}.parquet")
    if os.path.exists(cache_path):
        return pd.read_parquet(cache_path)
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    bars = []
    next_page = None
    while True:
        params = {"timeframe": "1Day", "start": f"{start}T00:00:00Z", "end": f"{end}T00:00:00Z",
                  "limit": 10000, "feed": "iex", "adjustment": "all"}
        if next_page:
            params["page_token"] = next_page
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        bars.extend(data.get("bars") or [])
        next_page = data.get("next_page_token")
        if not next_page:
            break
    if not bars:
        return None
    df = pd.DataFrame(bars)
    df["t"] = pd.to_datetime(df["t"]).dt.tz_localize(None).dt.normalize()
    df = df.set_index("t").sort_index()
    df = df[["o", "h", "l", "c"]].rename(columns={"o": "open", "h": "high", "l": "low", "c": "close"})
    df.to_parquet(cache_path)
    return df


def fetch_crypto_bars(headers, symbol, start, end):
    """Crypto bar fetcher — routes to EODHD (longer history) or Alpaca per DATA_SOURCE."""
    if DATA_SOURCE == "eodhd":
        # EODHD uses BTC-USD.CC format
        eod_sym = symbol.replace("/", "-")  # BTC/USD → BTC-USD
        return fetch_eodhd_bars(eod_sym, start, end, asset_type="cc")
    cache_path = os.path.join(CACHE_DIR, f"crypto_{symbol.replace('/', '_')}.parquet")
    if os.path.exists(cache_path):
        return pd.read_parquet(cache_path)
    url = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
    bars = []
    next_page = None
    while True:
        params = {"symbols": symbol, "timeframe": "1Day",
                  "start": f"{start}T00:00:00Z", "end": f"{end}T00:00:00Z", "limit": 10000}
        if next_page:
            params["page_token"] = next_page
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        chunk = (data.get("bars") or {}).get(symbol, [])
        bars.extend(chunk)
        next_page = data.get("next_page_token")
        if not next_page:
            break
    if not bars:
        return None
    df = pd.DataFrame(bars)
    df["t"] = pd.to_datetime(df["t"]).dt.tz_localize(None).dt.normalize()
    df = df.set_index("t").sort_index()
    df = df[["o", "h", "l", "c"]].rename(columns={"o": "open", "h": "high", "l": "low", "c": "close"})
    df.to_parquet(cache_path)
    return df


def fetch_fred_series(series_id, days=2200):
    cache_path = os.path.join(CACHE_DIR, f"fred_{series_id}.parquet")
    if os.path.exists(cache_path):
        return pd.read_parquet(cache_path)
    fred_key = subprocess.check_output(
        ["gcloud", "secrets", "versions", "access", "latest",
         "--secret=FREDKEY", "--project=trading-436516",
         "--account=cayookenz@gmail.com"],
    ).decode().strip()
    url = (f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}"
           f"&api_key={fred_key}&file_type=json&sort_order=asc&limit={days}")
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    rows = r.json().get("observations", [])
    parsed = []
    for o in rows:
        try:
            parsed.append((pd.Timestamp(o["date"]), float(o["value"])))
        except (ValueError, KeyError):
            continue
    if not parsed:
        return None
    df = pd.DataFrame(parsed, columns=["t", "value"]).set_index("t").sort_index()
    df.to_parquet(cache_path)
    return df


def fetch_all_data(start=START_DATE, end=END_DATE):
    """
    Unified data fetch with extended-history coverage back to 1987-01-02.

    Combines two data sources:
      1. Long-history SPLICED underlyings via extended_data module:
         VFINX→SPY (1979+), AEPGX→EFA (1984+), VUSTX→TLT (1986+),
         VFITX→IEF (1991+), XAU→GLD (1983+), SPGSCI→BCOM→DBC (1991+),
         VBMFX→AGG (1986+), VWEHX→HYG (1978+), VWESX→LQD (1986+),
         KMLMSIM→KMLM (1988+), FRED 3m T-bill→cash (1987+).
      2. Real ETFs from EODHD for their LIVE periods only (UPRO, TMF, KMLM,
         SPUU, QLD, EFO, SSO, USFR, etc.). The existing
         `spliced_leveraged_etf()` machinery uses these post-inception and
         falls back to synthetic-from-underlying pre-inception — the
         underlying now has extended history so synthetics extend too.

    Returns (closes, bars, vix, fed) — same shape as the original API so all
    downstream strategy / report code works unchanged.
    """
    import sys as _sys
    from pathlib import Path as _Path
    _here = str(_Path(__file__).resolve().parent)
    if _here not in _sys.path:
        _sys.path.insert(0, _here)
    import extended_data as ed

    print("Fetching extended-history spliced underlyings (1987+ via VFINX/VUSTX/AEPGX/XAU/SPGSCI/KMLMSIM)...")
    ext = ed.fetch_extended_data()

    closes = {}
    bars = {}
    # Map extended_data keys to the ticker symbols strategies expect.
    EXT_MAP = {
        "spy_tr": "SPY",  "efa_tr": "EFA",  "tlt_tr": "TLT",  "ief_tr": "IEF",
        "gld_tr": "GLD",  "dbc_tr": "DBC",  "qqq_tr": "QQQ",  "agg_tr": "AGG",
        "hyg_tr": "HYG",  "lqd_tr": "LQD",  "tip_tr": "TIP",  "kmlm_tr": "KMLM",
        "dbmf_tr": "DBMF",
        "ntsd_tr": "NTSD",
        "urth_tr": "URTH",
        "bnd_tr":  "BND",
        "slv_tr":  "SLV",
    }
    for key, ticker in EXT_MAP.items():
        s = ext.get(key)
        if s is not None:
            closes[ticker] = s

    # Cash (BIL/SHV/SGOV/USFR/SHY are all short-Treasury equivalents). Build a
    # synthetic price level from FRED 3m T-bill daily yields.
    bil_daily = ext["bil_daily_return"]
    bil_level = (1 + bil_daily).cumprod() * 100.0
    for cash_ticker in ("BIL", "SHV", "SGOV", "USFR", "SHY"):
        closes[cash_ticker] = bil_level.copy()

    # Now fetch REAL ETFs that aren't covered by extended_data, plus the
    # leveraged ETFs (UPRO/TMF/SPUU/QLD/EFO/SSO/etc.) for their live periods.
    # The spliced_leveraged_etf() helpers use these post-inception and fall
    # back to synthetic from underlying for earlier periods.
    headers = alpaca_creds()
    real_tickers = sorted({
        "URTH", "WLDU",                  # benchmarks / regime world
        "UPRO", "TMF", "DBMF",           # HFEA (KMLM already from extended_data)
        "SPXL",                          # SPXL SMA
        "VT", "RSSB", "WTIP",            # RSSB/WTIP live components
        "TQQQ",                          # 9-Sig
        "SPUU", "EFO", "BND",            # Dual Momentum
        "SLV",                           # WTIP component
        "SSO",                           # Regime SSO
        *SECTOR_ETFS, *SECTOR_ETFS_1X, SECTOR_BOND_ETF,  # Sector Momentum (historic)
        "EEM", "IWM",                    # Canary / momentum
        "SOXL", "EDC",                   # Wider-universe DM
        "SAA", "EET", "UBT", "UST", "UGL",  # 2× ETFs for AAA Free 2× + NTSD
        "TNA", "TYD",                    # 3× ETFs for AAA Free 3×
        "EDV",                           # Vanguard Extended Duration Treasury (zero-coupon, ~25y) — Wave 6 alt-duration
    })
    print(f"Fetching {len(real_tickers)} real ETFs for post-inception live data...")
    for sym in real_tickers:
        if sym in closes:
            continue
        try:
            df = fetch_stock_bars(headers, sym, start, end)
            if df is None or df.empty:
                continue
            closes[sym] = df["close"]
            bars[sym] = df
        except Exception:
            continue

    # BTC for WTIP synthetic
    try:
        btc = fetch_crypto_bars(headers, "BTC/USD", start, end)
        if btc is not None:
            closes["BTC/USD"] = btc["close"]
            bars["BTC/USD"] = btc
    except Exception:
        pass

    vix = fetch_fred_series("VIXCLS")
    fed = fetch_fred_series("DFEDTARU")
    earliest = min(s.index[0] for s in closes.values() if hasattr(s, 'index') and len(s) > 0)
    print(f"  ✓ {len(closes)} price series, VIX {len(vix) if vix is not None else 0} rows. "
           f"Earliest data: {earliest.date()}")
    return closes, bars, vix, fed


def daily_returns(closes_dict, idx):
    """Reindex all close series onto a common business-day index, return the daily-returns frame."""
    px = pd.DataFrame(closes_dict).reindex(idx).ffill()
    return px.pct_change().fillna(0)


# ═══════════════════════════════════════════════════════════════════════
# SYNTHETIC FUNDS — RSSB & WTIP
# ═══════════════════════════════════════════════════════════════════════

def synth_leveraged_etf(returns: pd.DataFrame, ticker: str) -> pd.Series:
    """
    Synthetic daily-reset leveraged ETF — matches Testfolio's `?L=N` formula:

        daily ≈ L × underlying_daily
                − SW × (L−1) × (short_rate_daily + SP/252)
                − (L−1) × E / 252

    The per-ticker stated ER in SYNTH_LEV_ETFS is intentionally ignored — Testfolio's
    standardized E = 0.5%×(L−1) takes its place, ensuring every synthetic LETF is
    directly comparable to Testfolio's `?L=N` output on the same underlying.
    """
    spec = SYNTH_LEV_ETFS[ticker]
    leverage, underlying, _er, _live = spec
    if underlying not in returns.columns:
        return pd.Series(0.0, index=returns.index)
    u = returns[underlying].fillna(0)
    if "BIL" in returns.columns:
        bil_daily = returns["BIL"].fillna(0)
        borrow_daily = SYNTH_LETF_SW * (leverage - 1) * (bil_daily + SYNTH_LETF_SP / 252)
    else:
        borrow_daily = SYNTH_LETF_SW * (leverage - 1) * (SYNTH_FINANCING_RATE + SYNTH_LETF_SP) / 252
    extra_drag_daily = (leverage - 1) * SYNTH_LETF_E_PER_LEV / 252
    return leverage * u - borrow_daily - extra_drag_daily


def spliced_leveraged_etf(returns: pd.DataFrame, ticker: str) -> pd.Series:
    """Synthetic before live inception, real ETF returns after."""
    synth = synth_leveraged_etf(returns, ticker)
    if ticker not in returns.columns:
        return synth
    live = returns[ticker].fillna(0)
    splice = pd.Timestamp(SYNTH_LEV_ETFS[ticker][3])
    out = synth.copy()
    out.loc[splice:] = live.loc[splice:].reindex(out.index).fillna(0).loc[splice:]
    return out


def spliced_usfr(returns: pd.DataFrame) -> pd.Series:
    """USFR (2014+) live; SHY for the pre-USFR window (2002-2014)."""
    if "USFR" not in returns.columns:
        return returns.get("SHY", pd.Series(0.0, index=returns.index)).fillna(0)
    usfr_live = returns["USFR"].fillna(0)
    splice = pd.Timestamp(USFR_LIVE_FROM_DATE)
    if "SHY" in returns.columns:
        out = returns["SHY"].fillna(0).copy()
    else:
        out = pd.Series(0.0, index=returns.index)
    out.loc[splice:] = usfr_live.loc[splice:].reindex(out.index).fillna(0).loc[splice:]
    return out


def spliced_urth(returns: pd.DataFrame) -> pd.Series:
    """MSCI World return series with maximum history.

    Preferred source: returns["URTH"] = URTHSIM (Testfolio MSCI World 1970+)
    spliced with real URTH at 2012-01-12, populated by extended_data.

    Fallback (old behaviour): real URTH from 2012-01-12 + VT proxy 2008-2012.
    """
    urth = returns.get("URTH")
    # If the URTH column has long history (URTHSIM-extended), use it directly.
    if urth is not None and urth.notna().sum() > 252 * 30:
        first = urth.first_valid_index()
        if first is not None and first <= pd.Timestamp("2000-01-01"):
            return urth.fillna(0)
    # Fallback path: real-URTH-from-2012 + VT proxy
    if urth is None:
        return returns.get("VT", pd.Series(0.0, index=returns.index)).fillna(0)
    urth_live = urth.fillna(0)
    splice = pd.Timestamp(URTH_LIVE_FROM_DATE)
    if "VT" in returns.columns:
        out = returns["VT"].fillna(0).copy()
    else:
        out = pd.Series(0.0, index=returns.index)
    out.loc[splice:] = urth_live.loc[splice:].reindex(out.index).fillna(0).loc[splice:]
    return out


def synth_wldu(returns: pd.DataFrame) -> pd.Series:
    """
    Synthetic WLDU = daily-reset 2× spliced-URTH using Testfolio's L=N formula:
        daily ≈ 2 × URTH_daily
              − SW × 1 × (short_rate_daily + SP/252)   ← swap-financed borrow
              − 1 × E / 252                            ← LETF expense (E=0.5%×(L-1))
    Same construction as our other synthetic LETFs for consistency.
    """
    urth = spliced_urth(returns)
    if "BIL" in returns.columns:
        bil_daily = returns["BIL"].fillna(0)
        borrow_daily = SYNTH_LETF_SW * 1.0 * (bil_daily + SYNTH_LETF_SP / 252)
    else:
        borrow_daily = SYNTH_LETF_SW * 1.0 * (SYNTH_FINANCING_RATE + SYNTH_LETF_SP) / 252
    extra_drag_daily = 1.0 * SYNTH_LETF_E_PER_LEV / 252
    return 2.0 * urth - borrow_daily - extra_drag_daily


def spliced_wldu(returns: pd.DataFrame) -> pd.Series:
    """Synthetic WLDU before live launch (~2026-03-12), live thereafter."""
    synth = synth_wldu(returns)
    if "WLDU" not in returns.columns:
        return synth
    live = returns["WLDU"].fillna(0)
    splice = pd.Timestamp(WLDU_LIVE_FROM)
    out = synth.copy()
    out.loc[splice:] = live.loc[splice:].reindex(out.index).fillna(0).loc[splice:]
    return out


def spliced_kmlm(returns: pd.DataFrame) -> pd.Series:
    """
    KMLM returns. returns["KMLM"] is already KMLMSIM-spliced via the data layer
    (KMLMSIM 1988+ → real KMLM at 2020-12-02). Just use it.

    Falls back to DBMF (2019-05+) only if no KMLM column is present at all —
    this branch is essentially dead given the extended_data integration but
    kept as a defensive fallback.
    """
    if "KMLM" in returns.columns:
        return returns["KMLM"].fillna(0)
    return returns.get("DBMF", pd.Series(0.0, index=returns.index)).fillna(0)


def synth_rssb(returns: pd.DataFrame) -> pd.Series:
    """Synthetic RSSB: 100% VT + 100% IEF (1.00x leverage) with 0.36% ER.

    VT inception is 2008-06-24; pre-2008 we substitute spliced_urth (URTHSIM-
    extended back to 1970). URTH is developed-markets-only vs VT's global
    all-cap, but they're functionally similar global-equity exposure for
    backtest purposes.
    """
    out = pd.Series(0.0, index=returns.index)
    notional = 0.0
    # Equity leg: VT post-inception, URTH pre-inception
    vt_w = RSSB_SYNTH.get("VT", 0.0)
    if vt_w > 0:
        urth_series = spliced_urth(returns)  # URTHSIM 1970+ / real URTH 2012+
        if "VT" in returns.columns:
            vt = returns["VT"]
            vt_inception = vt.first_valid_index()
            equity_ret = urth_series.copy()
            if vt_inception is not None:
                equity_ret.loc[vt_inception:] = vt.loc[vt_inception:].fillna(0)
        else:
            equity_ret = urth_series
        out += equity_ret.fillna(0) * vt_w
        notional += vt_w
    # Bond + other legs: as configured (IEF etc.)
    for sym, w in RSSB_SYNTH.items():
        if sym == "VT":
            continue
        if sym in returns.columns:
            out += returns[sym].fillna(0) * w
            notional += w
    out -= max(0, notional - 1.0) * 0.025 / 252  # 2.5% financing
    out -= 0.0036 / 252  # ER
    return out


BTC_INCEPTION = pd.Timestamp("2014-01-01")  # Crypto data floor on Alpaca/EODHD


def synth_wtip(returns: pd.DataFrame) -> pd.Series:
    """Synthetic WTIP: TIP/BIL/BTC/DBC/GLD/SLV (1.88x notional) with 0.65% ER.

    Time-varying basket: pre-BTC-inception (2014), the 7.5% BTC slot is
    redistributed proportionally across the other 5 components so the total
    1.88× notional is preserved. Post-2014 uses the original published basket.
    This unfloors the pre-2014 WTIP synthetic so RSSB/WTIP can extend back to
    TIP's effective floor (2001 via VIPSX warmup) instead of 2014.
    """
    out = pd.Series(0.0, index=returns.index)
    has_btc = "BTC/USD" in returns.columns

    # Pre-2014 redistributed weights (BTC's 7.5% spread proportionally across
    # the other 5 slots; original ratios preserved, total 1.88× notional).
    btc_w = WTIP_SYNTH["BTC/USD"]
    other_total = sum(w for s, w in WTIP_SYNTH.items() if s != "BTC/USD")
    weights_pre = {s: w * (other_total + btc_w) / other_total
                   for s, w in WTIP_SYNTH.items() if s != "BTC/USD"}
    weights_post = {s: w for s, w in WTIP_SYNTH.items()}

    pre_mask = returns.index < BTC_INCEPTION
    post_mask = ~pre_mask

    for sym, w_post in weights_post.items():
        if sym not in returns.columns:
            continue
        r = returns[sym].fillna(0)
        if sym in weights_pre:
            w_pre = weights_pre[sym]
        else:
            w_pre = 0.0  # BTC has no pre-2014 weight
        out.loc[pre_mask] += r.loc[pre_mask] * w_pre
        out.loc[post_mask] += r.loc[post_mask] * w_post

    # 2.5% financing on the levered portion (notional 1.88 → 0.88 levered)
    out -= 0.88 * 0.025 / 252
    out -= 0.0065 / 252  # ER
    return out


def spliced_rssb_wtip(returns: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Use synthetic before live launch, live thereafter."""
    rssb_synth = synth_rssb(returns)
    wtip_synth = synth_wtip(returns)
    rssb_live = returns["RSSB"] if "RSSB" in returns.columns else None
    wtip_live = returns["WTIP"] if "WTIP" in returns.columns else None
    rssb_split = pd.Timestamp(RSSB_LIVE_FROM)
    wtip_split = pd.Timestamp(WTIP_LIVE_FROM)
    rssb = rssb_synth.copy()
    if rssb_live is not None:
        rssb.loc[rssb_split:] = rssb_live.loc[rssb_split:].reindex(rssb.index).fillna(0).loc[rssb_split:]
    wtip = wtip_synth.copy()
    if wtip_live is not None:
        wtip.loc[wtip_split:] = wtip_live.loc[wtip_split:].reindex(wtip.index).fillna(0).loc[wtip_split:]
    return rssb, wtip


# ═══════════════════════════════════════════════════════════════════════
# STRATEGY BACKTESTS — each returns a daily-returns Series
# Convention: start with $1, return the running daily PnL series.
# ═══════════════════════════════════════════════════════════════════════


def _quarterly_rebal_dates(idx: pd.DatetimeIndex) -> set:
    return set(idx.to_series().resample("Q").first().index)


def _monthly_rebal_dates(idx: pd.DatetimeIndex) -> set:
    return set(idx.to_series().resample("M").first().index)


def _drift_and_rebalance(returns: pd.DataFrame, target: dict, rebal_dates: set) -> pd.Series:
    """Generic weight-drift portfolio with rebalance to target on rebal_dates."""
    assets = list(target.keys())
    target_arr = np.array([target[a] for a in assets])
    rets = returns[assets].fillna(0)
    weights = target_arr.copy()
    out = []
    for date, daily in rets.iterrows():
        out.append(float(np.dot(weights, daily.values)))
        weights = weights * (1 + daily.values)
        s = weights.sum()
        if s > 0:
            weights = weights / s
        if date in rebal_dates:
            weights = target_arr.copy()
    return pd.Series(out, index=rets.index)


def _target_weights_from_segments(
    segments: list, idx: pd.DatetimeIndex, rebal_freq: str = "Q"
) -> pd.DataFrame:
    """Build a target-weights DataFrame from a list of (start_date, target_dict)
    segments. Emits one row per rebalance date (quarterly or monthly).

    Args:
        segments: list of (start_date_str_or_ts, target_weights_dict). Each
            segment applies from its start date until the next segment's start.
        idx: full DatetimeIndex of the strategy.
        rebal_freq: "Q" for quarter-start, "M" for month-start.
    """
    if rebal_freq == "Q":
        rebal_dates = sorted(_quarterly_rebal_dates(idx))
    else:
        rebal_dates = sorted(_monthly_rebal_dates(idx))
    if not rebal_dates:
        return pd.DataFrame()
    segs = [(pd.Timestamp(s[0]), s[1]) for s in segments]
    segs.sort(key=lambda s: s[0])
    all_tickers = sorted({t for _, w in segs for t in w.keys()})
    rows = []
    dates = []
    for d in rebal_dates:
        active = segs[0][1]
        for s_date, w in segs:
            if d >= s_date:
                active = w
            else:
                break
        rows.append({t: active.get(t, 0.0) for t in all_tickers})
        dates.append(d)
    # Anchor day 0 of strategy too, so initial buy is recorded
    if dates and dates[0] != idx[0]:
        first_active = segs[0][1]
        rows.insert(0, {t: first_active.get(t, 0.0) for t in all_tickers})
        dates.insert(0, idx[0])
    return pd.DataFrame(rows, index=pd.DatetimeIndex(dates))


def bt_hfea(returns: pd.DataFrame, return_weights: bool = False):
    """
    HFEA with full historical reconstruction:
      • UPRO, TMF: synthetic 3× SPY/TLT before fund inception (2009)
      • Pre-KMLM-launch (2020-12-02): classic 55/45 UPRO/TMF
      • Post-KMLM-launch: 45/25/30 UPRO/TMF/KMLM
    KMLM uses DBMF as proxy for 2019-05 → 2020-12 gap (DBMF inception); pre-2019
    no managed-futures component is reasonable to synthesize from EODHD's free
    feeds, so the strategy effectively runs as a 2-asset HFEA in those years.
    """
    upro = spliced_leveraged_etf(returns, "UPRO")
    tmf = spliced_leveraged_etf(returns, "TMF")
    kmlm = spliced_kmlm(returns)

    kmlm_split = pd.Timestamp(KMLM_LIVE_FROM_DATE)

    # Pre-KMLM segment: 55/45 UPRO/TMF (the original HFEA recipe)
    pre_idx = returns.index[returns.index < kmlm_split]
    pre_rets = pd.DataFrame({"UPRO": upro, "TMF": tmf}).loc[pre_idx]
    pre = _drift_and_rebalance(
        pre_rets, HFEA_PRE_KMLM_WEIGHTS, _quarterly_rebal_dates(pre_idx)
    ) if len(pre_idx) > 0 else pd.Series(dtype=float)

    # Post-KMLM segment: 45/25/30 with real KMLM
    post_idx = returns.index[returns.index >= kmlm_split]
    post_rets = pd.DataFrame({"UPRO": upro, "TMF": tmf, "KMLM": kmlm}).loc[post_idx]
    post = _drift_and_rebalance(
        post_rets, HFEA_POST_KMLM_WEIGHTS, _quarterly_rebal_dates(post_idx)
    ) if len(post_idx) > 0 else pd.Series(dtype=float)

    ret = pd.concat([pre, post])
    if not return_weights:
        return ret

    full_idx = returns.index[returns.index >= (pre_idx[0] if len(pre_idx) > 0 else post_idx[0])]
    asset_returns = pd.DataFrame({"UPRO": upro, "TMF": tmf, "KMLM": kmlm}).reindex(full_idx).fillna(0.0)
    weights = _target_weights_from_segments(
        [(full_idx[0], HFEA_PRE_KMLM_WEIGHTS), (kmlm_split, HFEA_POST_KMLM_WEIGHTS)],
        full_idx, "Q",
    )
    return ret, {"weights": weights, "asset_returns": asset_returns}


def bt_spxl_sma(returns: pd.DataFrame, prices: pd.DataFrame, return_weights: bool = False):
    """SPXL when SPY > 200-SMA × 1.01, SGOV otherwise. Switches checked daily.

    Uses spliced_leveraged_etf for SPXL so pre-2008-11-05 dates get a synthetic
    3× SPY return (with borrow + expense) instead of zeros."""
    spy = prices["SPY"]
    sma = spy.rolling(200).mean()
    bullish = spy > sma * 1.01
    bearish = spy < sma * 0.99
    state = pd.Series(False, index=spy.index)
    cur = False
    for d in spy.index:
        if bullish.loc[d]:
            cur = True
        elif bearish.loc[d]:
            cur = False
        state.loc[d] = cur
    spxl = spliced_leveraged_etf(returns, "SPXL")
    sgov = returns["SGOV"].fillna(0) if "SGOV" in returns.columns else pd.Series(0.0, index=returns.index)
    in_spxl = state.shift(1).fillna(False).astype(float)
    ret = in_spxl * spxl + (1 - in_spxl) * sgov
    if not return_weights:
        return ret

    asset_returns = pd.DataFrame({"SPXL": spxl, "SGOV": sgov}).fillna(0.0)
    # Weight changes only on state transitions — sparse weights timeline
    state_change = in_spxl.diff().fillna(in_spxl)
    change_idx = state_change[state_change != 0].index
    if len(change_idx) == 0 or change_idx[0] != ret.index[0]:
        change_idx = pd.DatetimeIndex([ret.index[0]]).append(change_idx)
    weights = pd.DataFrame(
        {"SPXL": in_spxl.reindex(change_idx).values,
         "SGOV": (1 - in_spxl).reindex(change_idx).values},
        index=change_idx,
    )
    return ret, {"weights": weights, "asset_returns": asset_returns}


def bt_rssb_wtip(returns: pd.DataFrame) -> pd.Series:
    rssb, wtip = spliced_rssb_wtip(returns)
    fund_returns = pd.DataFrame({"RSSB": rssb, "WTIP": wtip})
    return _drift_and_rebalance(fund_returns, RSSB_WTIP_WEIGHTS, _quarterly_rebal_dates(fund_returns.index))


def bt_nine_sig(returns: pd.DataFrame, prices: pd.DataFrame, return_weights: bool = False):
    """
    9-Sig backtest — Jason Kelly canonical (60/40 TQQQ/AGG).

    Each quarter:
      • signal line = previous post-trade TQQQ value × (1 + 9%)
        (first quarter: 60% of NAV)
      • SPIKE RESET: TQQQ +100% over the quarter AND currently >60% of NAV AND not
        in a 30-down episode → snap TQQQ to 60% of NAV (caps a runaway signal line).
      • SELL candidate (TQQQ > signal + tol): if TQQQ is ≥30% below its rolling
        8-quarter high AND we've ignored < 2 sells → SELL_IGNORED (hold). When the
        2-ignore streak is exhausted during an ongoing 30-down → BASE_RESET to 60/40.
        Otherwise sell TQQQ down to the signal line.
      • BUY candidate (TQQQ < signal − tol): buy toward the signal line, clamped by
        the 90% bond throttle AND the 10% bond floor.
      • Within tolerance: HOLD.

    The 30-down trigger uses the *synthetic TQQQ* series with a rolling 8-quarter
    high — NOT an expanding SPY all-time-high. The old expanding-ATH-on-SPY logic
    meant the trigger essentially never fired in a multi-year grind.

    Note: monthly AGG contributions are not simulated — we track unit-NAV growth.
    """
    idx = returns.index
    tqqq = spliced_leveraged_etf(returns, "TQQQ")  # synthetic 3× QQQ pre-2010-02
    agg = returns["AGG"].fillna(0) if "AGG" in returns.columns else pd.Series(0.0, index=idx)
    quarterly = _quarterly_rebal_dates(idx)

    # Synthetic TQQQ price level + rolling 8-quarter high for the 30-down trigger.
    tqqq_price = (1.0 + tqqq).cumprod()
    lookback_days = NINE_SIG_LOOKBACK_QUARTERS * 63          # 8 quarters ≈ 504 trading days
    tqqq_roll_high = tqqq_price.rolling(lookback_days, min_periods=1).max()

    w0_tqqq = NINE_SIG_TARGET["TQQQ"]   # 0.60
    w0_agg = NINE_SIG_TARGET["AGG"]     # 0.40
    w_tqqq, w_agg = w0_tqqq, w0_agg
    last_q_tqqq_value = None
    nav = 1.0
    sell_ignored_count = 0  # consecutive ignored sell signals
    out = []
    weight_log: list[tuple[pd.Timestamp, float, float]] = [(idx[0], w0_tqqq, w0_agg)]

    for date, _ in returns.iterrows():
        # Today's portfolio return
        port_ret = w_tqqq * tqqq.loc[date] + w_agg * agg.loc[date]
        out.append(port_ret)
        nav *= (1 + port_ret)

        # Drift weights to end of day
        new_tqqq = w_tqqq * (1 + tqqq.loc[date])
        new_agg = w_agg * (1 + agg.loc[date])
        ssum = new_tqqq + new_agg
        if ssum > 0:
            w_tqqq, w_agg = new_tqqq / ssum, new_agg / ssum

        if date in quarterly:
            tqqq_value = nav * w_tqqq
            agg_value = nav * w_agg
            if last_q_tqqq_value is None:
                signal = nav * w0_tqqq          # first quarter: 60% of NAV
            else:
                signal = last_q_tqqq_value * (1 + NINE_SIG_QUARTERLY_GROWTH)

            # 30-down state on the synthetic TQQQ series (rolling 8-quarter high)
            roll_high = tqqq_roll_high.loc[date]
            price = tqqq_price.loc[date]
            thirty_down = roll_high > 0 and (roll_high - price) / roll_high >= NINE_SIG_DRAWDOWN_THRESHOLD

            # TQQQ quarterly price gain for the spike-reset check (~one quarter back)
            prior_price = tqqq_price.asof(date - pd.Timedelta(days=95))
            spike_gain = (price / prior_price - 1.0) if (prior_price and prior_price > 0) else 0.0

            diff = tqqq_value - signal
            tolerance = nav * NINE_SIG_TOLERANCE_PCT
            cur_tqqq_pct = w_tqqq
            target_tqqq_value = tqqq_value  # default: no change
            trade = False

            if (last_q_tqqq_value is not None and spike_gain >= NINE_SIG_SPIKE_GAIN
                    and cur_tqqq_pct > w0_tqqq and not thirty_down):
                # SPIKE RESET → 60% of NAV
                target_tqqq_value = nav * w0_tqqq
                trade = True
                sell_ignored_count = 0
            elif diff < -tolerance:
                # BUY toward signal, clamped by 90% throttle + 10% bond floor
                deficit = -diff
                max_buy = max(0.0, min(deficit,
                                       NINE_SIG_THROTTLE * agg_value,
                                       agg_value - NINE_SIG_BOND_FLOOR * nav))
                if max_buy > 0:
                    target_tqqq_value = tqqq_value + max_buy
                    trade = True
            elif diff > tolerance:
                # SELL candidate. Check the 30-down rule on TQQQ.
                if thirty_down and sell_ignored_count < NINE_SIG_MAX_SELL_IGNORES:
                    sell_ignored_count += 1  # SELL_IGNORED — hold
                elif thirty_down:
                    # ignore streak exhausted during an ongoing 30-down → base reset
                    target_tqqq_value = nav * w0_tqqq
                    trade = True
                    sell_ignored_count = 0
                else:
                    target_tqqq_value = signal  # normal sell to the signal line
                    trade = True
                    sell_ignored_count = 0

            if trade:
                target_tqqq_value = max(0.0, min(nav, target_tqqq_value))
                w_tqqq = target_tqqq_value / nav if nav > 0 else w0_tqqq
                w_agg = 1.0 - w_tqqq
                weight_log.append((date, w_tqqq, w_agg))

            # Always update last_q_tqqq for next quarter's signal calc
            last_q_tqqq_value = nav * w_tqqq

    ret = pd.Series(out, index=idx)
    if not return_weights:
        return ret
    asset_returns = pd.DataFrame({"TQQQ": tqqq, "AGG": agg}).fillna(0.0)
    weights = pd.DataFrame(
        [{"TQQQ": w[1], "AGG": w[2]} for w in weight_log],
        index=pd.DatetimeIndex([w[0] for w in weight_log]),
    )
    return ret, {"weights": weights, "asset_returns": asset_returns}


def bt_dual_momentum(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """
    Dual Momentum: monthly check of SPY vs EFA 12-month returns.
      Signal A: SPY return > +1%
      Signal B: EFA return > +1%
      Signal C: SPY > EFA + 1%
    Allocations:
      A AND B AND C → SPUU (2x SPY)
      Else if B → EFO (2x EFA)
      Else → BND
    """
    spy = prices["SPY"]
    efa = prices["EFA"]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    spuu = spliced_leveraged_etf(returns, "SPUU")  # synthetic 2× SPY pre-2014-05
    efo = spliced_leveraged_etf(returns, "EFO")    # synthetic 2× EFA pre-2009-06
    bnd = returns["BND"].fillna(0) if "BND" in returns.columns else pd.Series(0.0, index=returns.index)
    asset = pd.Series("BND", index=returns.index)
    current = "BND"
    for d in returns.index:
        if d in rebal_dates and d - pd.Timedelta(days=365) >= returns.index[0]:
            try:
                spy_ret = spy.loc[d] / spy.asof(d - pd.Timedelta(days=365)) - 1
                efa_ret = efa.loc[d] / efa.asof(d - pd.Timedelta(days=365)) - 1
                a = spy_ret > 0.01
                b = efa_ret > 0.01
                c = spy_ret > efa_ret + 0.01
                if a and b and c:
                    current = "SPUU"
                elif b:
                    current = "EFO"
                else:
                    current = "BND"
            except Exception:
                pass
        asset.loc[d] = current
    out = []
    for d, sig in asset.items():
        if sig == "SPUU":
            out.append(spuu.loc[d])
        elif sig == "EFO":
            out.append(efo.loc[d])
        else:
            out.append(bnd.loc[d])
    return pd.Series(out, index=returns.index)


def bt_sector_momentum(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """
    Sector momentum: each month, score each sector by weighted multi-period
    momentum (1m/3m/6m/12m). Pick top N. SPY 200-SMA gate sends us to SCHZ
    when SPY < SMA × 1.01.
    """
    spy = prices["SPY"]
    spy_sma = spy.rolling(200).mean()
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    holding = []  # list of current ETFs to hold (equal-weighted top N)
    out = []
    for d in returns.index:
        if d in rebal_dates:
            spy_above = spy.loc[d] > spy_sma.loc[d] * 1.01 if not pd.isna(spy_sma.loc[d]) else False
            if not spy_above:
                holding = [SECTOR_BOND_ETF]
            else:
                scores = {}
                for sec in SECTOR_ETFS:
                    if sec not in prices.columns:
                        continue
                    score = 0
                    have_all = True
                    for label, lookback in SECTOR_LOOKBACKS_DAYS.items():
                        try:
                            past = prices[sec].asof(d - pd.Timedelta(days=int(lookback * 1.45)))
                            now = prices[sec].loc[d]
                            if pd.isna(past) or past <= 0:
                                have_all = False
                                break
                            ret = (now / past) - 1
                            score += SECTOR_WEIGHTS[label] * ret
                        except Exception:
                            have_all = False
                            break
                    if have_all:
                        scores[sec] = score
                if scores:
                    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:SECTOR_TOP_N]
                    holding = [s for s, _ in top]
                else:
                    holding = [SECTOR_HOLDING_FUND]
        if not holding:
            out.append(0)
            continue
        weight = 1.0 / len(holding)
        ret = sum(weight * returns[h].loc[d] for h in holding if h in returns.columns)
        out.append(ret)
    return pd.Series(out, index=returns.index)


def bt_regime_sso(returns: pd.DataFrame, prices: pd.DataFrame, vix_df: pd.DataFrame, fed_df: pd.DataFrame, return_weights: bool = False):
    """
    Regime SSO: 6-signal composite (news omitted). SSO when in market, USFR when defensive.
    Signals: price trend (3-day hysteresis), market breadth (skipped — too expensive
    to compute for 1400 days × 500 stocks), VIX level+trajectory, ADX, credit spread
    (HYG/LQD vs 50-SMA), canary (HYG/EEM/IWM vs 50-SMA). Composite range -5..+5.

    Decision logic mirrors production:
      EXIT_SLOW: composite ≤ 0 for 15 days
      EXIT_FAST: composite ≤ -3 for 3 days
      REENTER_STD: composite ≥ +3 for 15 days (Path C only — Path A/B require trajectory)
      Fed-hike filter blocks re-entry.
    """
    sso = spliced_leveraged_etf(returns, "SSO")  # Synthetic 2×SPY pre-2006-06-21, real SSO post
    usfr = spliced_usfr(returns)  # USFR live 2014+; SHY proxy for 2002-2014
    cfg = REGIME_CFG

    spy = prices["SPY"]
    spy_sma200 = spy.rolling(cfg["spy_sma_period"]).mean()

    # Pre-compute signals
    # Signal 1: price trend with 3-day hysteresis
    raw_trend = pd.Series(0, index=spy.index)
    raw_trend[spy > spy_sma200] = 1
    raw_trend[spy < spy_sma200] = -1
    s1 = pd.Series(0, index=spy.index)
    last_signal = 0
    for i, d in enumerate(spy.index):
        if i < 2:
            s1.iloc[i] = last_signal
            continue
        last3 = raw_trend.iloc[i - 2:i + 1].values
        if all(v != 0 and v == last3[-1] for v in last3):
            last_signal = int(last3[-1])
        s1.iloc[i] = last_signal

    # Signal 3: VIX
    vix = vix_df["value"] if vix_df is not None else None
    s3 = pd.Series(0, index=spy.index)
    if vix is not None:
        vix_aligned = vix.reindex(spy.index, method="ffill")
        vix_5d_change = vix_aligned.pct_change(5)
        s3[(vix_aligned > cfg["vix_high"]) | (vix_5d_change > cfg["vix_5d_change_high"])] = -1
        s3[(vix_aligned < cfg["vix_low"]) & (vix_5d_change < 0.10)] = 1

    # Signal 4: ADX
    high = prices["SPY"]  # using close as proxy for high/low here is rough but Alpaca returns OHLC
    # We need the bars dataframe to compute proper ADX; if prices is just close, use a simplified version
    s4 = pd.Series(0, index=spy.index)  # Skipping proper ADX in backtest for simplicity

    # Signal 5: HYG/LQD vs 50-SMA
    s5 = pd.Series(0, index=spy.index)
    if "HYG" in prices.columns and "LQD" in prices.columns:
        ratio = prices["HYG"] / prices["LQD"]
        ratio_sma = ratio.rolling(cfg["credit_sma_period"]).mean()
        s5[ratio > ratio_sma * 1.002] = 1
        s5[ratio < ratio_sma * 0.998] = -1

    # Signal 7: canary
    s7 = pd.Series(0, index=spy.index)
    canary_above = pd.Series(0, index=spy.index)
    canary_below = pd.Series(0, index=spy.index)
    for sym in ("HYG", "EEM", "IWM"):
        if sym not in prices.columns:
            continue
        sma = prices[sym].rolling(cfg["canary_sma_period"]).mean()
        canary_above += (prices[sym] > sma).astype(int)
        canary_below += (prices[sym] < sma).astype(int)
    s7[canary_below >= 3] = -1
    s7[canary_above >= 3] = 1

    # Signal 2 (breadth) and Signal 6 (news) skipped — backfill notes
    composite = s1 + s3 + s4 + s5 + s7  # 5-signal composite (range -5..+5)

    # Fed-hike filter from FRED
    fed = fed_df["value"] if fed_df is not None else None
    fed_hike = pd.Series(False, index=spy.index)
    if fed is not None:
        fed_aligned = fed.reindex(spy.index, method="ffill")
        fed_change = fed_aligned - fed_aligned.shift(cfg["fed_hike_lookback_days"])
        fed_hike = fed_change >= (cfg["fed_hike_threshold_bps"] / 100)

    # Decision loop
    position = "SSO"
    out = []
    state_log: list[tuple[pd.Timestamp, str]] = [(spy.index[0], "SSO")]
    composites_list = composite.values
    for i, d in enumerate(spy.index):
        # Today's return uses yesterday's position decision
        out.append(sso.loc[d] if position == "SSO" else usfr.loc[d])

        # End-of-day decision for tomorrow
        if i < cfg["slow_exit_days"]:
            continue
        c_recent_slow = composites_list[i - cfg["slow_exit_days"] + 1: i + 1]
        c_recent_fast = composites_list[i - cfg["fast_exit_days"] + 1: i + 1]
        c_recent_reentry = composites_list[i - cfg["standard_reentry_days"] + 1: i + 1]
        prev_position = position
        if position == "SSO":
            if all(c <= cfg["fast_exit_score"] for c in c_recent_fast):
                position = "USFR"
            elif all(c <= cfg["slow_exit_score"] for c in c_recent_slow):
                position = "USFR"
        else:
            if fed_hike.iloc[i]:
                continue
            if all(c >= cfg["reentry_score"] for c in c_recent_reentry):
                position = "SSO"
        if position != prev_position:
            state_log.append((d, position))
    ret = pd.Series(out, index=spy.index)
    if not return_weights:
        return ret
    asset_returns = pd.DataFrame({"SSO": sso, "USFR": usfr}).fillna(0.0)
    weights = pd.DataFrame(
        [{"SSO": 1.0 if s == "SSO" else 0.0, "USFR": 1.0 if s == "USFR" else 0.0}
         for _, s in state_log],
        index=pd.DatetimeIndex([d for d, _ in state_log]),
    )
    return ret, {"weights": weights, "asset_returns": asset_returns}


def bt_dual_momentum_optimized(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """
    Dual Momentum, optimized:
      • Blended momentum: avg of 6-month and 12-month returns (more robust than
        single 12-month lookback — Asness, multi-horizon momentum literature).
      • Skip-most-recent-month: signal uses prices as-of t−21 days, not t.
        This avoids the short-term reversal effect (Jegadeesh-Titman / Asness 1997).
      • Same SPUU / EFO / BND universe — keeps the strategy's leverage character.

    The original Antonacci spec uses a single 12-month return without skip-1.
    The literature on momentum strongly suggests both upgrades.
    """
    spy = prices["SPY"]
    efa = prices["EFA"]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    spuu = spliced_leveraged_etf(returns, "SPUU")
    efo = spliced_leveraged_etf(returns, "EFO")
    bnd = returns["BND"].fillna(0) if "BND" in returns.columns else pd.Series(0.0, index=returns.index)
    asset = pd.Series("BND", index=returns.index)
    current = "BND"
    for d in returns.index:
        if d in rebal_dates and d - pd.Timedelta(days=400) >= returns.index[0]:
            try:
                signal_date = d - pd.Timedelta(days=21)  # skip-1m
                spy_now = spy.asof(signal_date)
                efa_now = efa.asof(signal_date)
                spy_6m_ago = spy.asof(d - pd.Timedelta(days=189))
                spy_12m_ago = spy.asof(d - pd.Timedelta(days=378))
                efa_6m_ago = efa.asof(d - pd.Timedelta(days=189))
                efa_12m_ago = efa.asof(d - pd.Timedelta(days=378))
                if any(pd.isna(x) or x is None or x <= 0 for x in
                       (spy_now, efa_now, spy_6m_ago, spy_12m_ago, efa_6m_ago, efa_12m_ago)):
                    asset.loc[d] = current
                    continue
                spy_ret = 0.5 * (spy_now / spy_6m_ago - 1) + 0.5 * (spy_now / spy_12m_ago - 1)
                efa_ret = 0.5 * (efa_now / efa_6m_ago - 1) + 0.5 * (efa_now / efa_12m_ago - 1)
                a = spy_ret > 0.01
                b = efa_ret > 0.01
                c = spy_ret > efa_ret + 0.01
                if a and b and c:
                    current = "SPUU"
                elif b:
                    current = "EFO"
                else:
                    current = "BND"
            except Exception:
                pass
        asset.loc[d] = current
    out = []
    for d, sig in asset.items():
        if sig == "SPUU":
            out.append(spuu.loc[d])
        elif sig == "EFO":
            out.append(efo.loc[d])
        else:
            out.append(bnd.loc[d])
    return pd.Series(out, index=returns.index)


# Optimized sector momentum lookback weights — drops noisy 1-month, emphasizes long-term trend
SECTOR_LOOKBACKS_OPT = {"3m": 63, "6m": 126, "12m": 252}
SECTOR_WEIGHTS_OPT = {"3m": 0.30, "6m": 0.30, "12m": 0.40}

# 1× SPDR sector ETFs (longer history, lower drawdown). Map 2× → 1× equivalents.
SECTOR_ETFS_1X = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB"]


def bt_sector_momentum_optimized(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """
    Sector Momentum, optimized:
      • Drop the 1-month component (which had 40% weight in the original — too noisy
        and prone to short-term reversal). New weights: 30/30/40 across 3m/6m/12m.
      • Skip-most-recent-month: signal uses prices as-of t−21 days, not t.
      • Equal-weight top-3 (unchanged — score-weighting is overcomplication).
      • Same SPY 200-SMA gate, same 2× sector ETFs.

    Pulls all the improvements that have empirical support without changing
    the leverage profile or universe.
    """
    spy = prices["SPY"]
    spy_sma = spy.rolling(200).mean()
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    holding = []
    out = []
    for d in returns.index:
        if d in rebal_dates:
            spy_above = spy.loc[d] > spy_sma.loc[d] * 1.01 if not pd.isna(spy_sma.loc[d]) else False
            if not spy_above:
                holding = [SECTOR_BOND_ETF]
            else:
                signal_date = d - pd.Timedelta(days=21)  # skip-1m
                scores = {}
                for sec in SECTOR_ETFS:
                    if sec not in prices.columns:
                        continue
                    score = 0
                    have_all = True
                    for label, lookback in SECTOR_LOOKBACKS_OPT.items():
                        try:
                            past = prices[sec].asof(d - pd.Timedelta(days=int(lookback * 1.45)))
                            now = prices[sec].asof(signal_date)
                            if pd.isna(past) or past <= 0 or pd.isna(now):
                                have_all = False
                                break
                            ret = (now / past) - 1
                            score += SECTOR_WEIGHTS_OPT[label] * ret
                        except Exception:
                            have_all = False
                            break
                    if have_all:
                        scores[sec] = score
                if scores:
                    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:SECTOR_TOP_N]
                    holding = [s for s, _ in top]
                else:
                    holding = [SECTOR_HOLDING_FUND]
        if not holding:
            out.append(0)
            continue
        weight = 1.0 / len(holding)
        ret = sum(weight * returns[h].loc[d] for h in holding if h in returns.columns)
        out.append(ret)
    return pd.Series(out, index=returns.index)


def _dm_step(d, spy, efa, lookback_periods, lookback_weights, skip_days,
             lev2_assets, signal_only_mode=None):
    """
    Dual-momentum signal computation. Returns target asset name (string).
      lookback_periods: dict {label: days}
      lookback_weights: dict {label: weight} (must sum to 1.0)
      skip_days: trading days to skip from end (Jegadeesh-Titman skip-1m = 21)
      lev2_assets: dict {asset: ticker} mapping signal to fund (e.g. {"spy_win":"SPUU"})
      signal_only_mode: 'absolute_only' / None
    """
    sd = d - pd.Timedelta(days=skip_days) if skip_days else d
    spy_now = spy.asof(sd)
    efa_now = efa.asof(sd)
    if pd.isna(spy_now) or pd.isna(efa_now) or spy_now <= 0 or efa_now <= 0:
        return None
    spy_score = 0.0
    efa_score = 0.0
    for label, days in lookback_periods.items():
        spy_past = spy.asof(d - pd.Timedelta(days=int(days * 1.45)))
        efa_past = efa.asof(d - pd.Timedelta(days=int(days * 1.45)))
        if pd.isna(spy_past) or pd.isna(efa_past) or spy_past <= 0 or efa_past <= 0:
            return None
        spy_score += lookback_weights[label] * (spy_now / spy_past - 1)
        efa_score += lookback_weights[label] * (efa_now / efa_past - 1)
    a = spy_score > 0.01
    b = efa_score > 0.01
    c = spy_score > efa_score + 0.01
    if signal_only_mode == "absolute_only":
        # No relative comparison — just take whichever is positive
        if a:
            return "SPUU_or_1x_SPY"
        if b:
            return "EFO_or_1x_EFA"
        return "BND"
    if a and b and c:
        return "SPUU_or_1x_SPY"
    if b:
        return "EFO_or_1x_EFA"
    return "BND"


def _bt_dual_momentum_generic(returns, prices, lookback_periods, lookback_weights,
                                skip_days=0, leverage="2x", spy_filter_sma=None,
                                signal_mode=None):
    """
    Generic dual momentum runner.
      leverage: '2x' (SPUU/EFO/BND) or '1x' (SPY/EFA/BND)
      spy_filter_sma: if set, only allow equity positions when SPY > this SMA period
      signal_mode: 'absolute_only' to skip relative comparison
    """
    spy = prices["SPY"]
    efa = prices["EFA"]
    spy_sma = spy.rolling(spy_filter_sma).mean() if spy_filter_sma else None
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))

    if leverage == "2x":
        asset_returns = {
            "SPUU_or_1x_SPY": spliced_leveraged_etf(returns, "SPUU"),
            "EFO_or_1x_EFA": spliced_leveraged_etf(returns, "EFO"),
            "BND": returns["BND"].fillna(0) if "BND" in returns.columns else pd.Series(0.0, index=returns.index),
        }
    else:  # 1x
        asset_returns = {
            "SPUU_or_1x_SPY": returns["SPY"].fillna(0),
            "EFO_or_1x_EFA": returns["EFA"].fillna(0) if "EFA" in returns.columns else pd.Series(0.0, index=returns.index),
            "BND": returns["BND"].fillna(0) if "BND" in returns.columns else pd.Series(0.0, index=returns.index),
        }

    current = "BND"
    asset = pd.Series("BND", index=returns.index)
    for d in returns.index:
        if d in rebal_dates and d - pd.Timedelta(days=400) >= returns.index[0]:
            sig = _dm_step(d, spy, efa, lookback_periods, lookback_weights,
                           skip_days, asset_returns, signal_only_mode=signal_mode)
            if sig is not None:
                current = sig
                # Apply Faber-style SMA filter — don't go to equity if SPY < SMA
                if spy_filter_sma and current in ("SPUU_or_1x_SPY", "EFO_or_1x_EFA"):
                    sma_today = spy_sma.loc[d]
                    if pd.notna(sma_today) and spy.loc[d] < sma_today:
                        current = "BND"
        asset.loc[d] = current
    out = []
    for d, sig in asset.items():
        out.append(asset_returns[sig].loc[d])
    return pd.Series(out, index=returns.index)


def bt_dual_momentum_multi_lookback(returns, prices):
    """Avg of 3m/6m/9m/12m equal-weighted, skip-1m, 2x leverage. Asness multi-horizon style."""
    return _bt_dual_momentum_generic(
        returns, prices,
        lookback_periods={"3m": 63, "6m": 126, "9m": 189, "12m": 252},
        lookback_weights={"3m": 0.25, "6m": 0.25, "9m": 0.25, "12m": 0.25},
        skip_days=21, leverage="2x",
    )


def bt_dual_momentum_faber(returns, prices):
    """Optimized DM (6m+12m blend, skip-1m) PLUS Faber 200-SMA filter on SPY."""
    return _bt_dual_momentum_generic(
        returns, prices,
        lookback_periods={"6m": 126, "12m": 252},
        lookback_weights={"6m": 0.5, "12m": 0.5},
        skip_days=21, leverage="2x",
        spy_filter_sma=200,
    )


def bt_dual_momentum_1x(returns, prices):
    """1× version of the optimized strategy — SPY/EFA/BND instead of SPUU/EFO/BND."""
    return _bt_dual_momentum_generic(
        returns, prices,
        lookback_periods={"6m": 126, "12m": 252},
        lookback_weights={"6m": 0.5, "12m": 0.5},
        skip_days=21, leverage="1x",
    )


def bt_dual_momentum_abs_only(returns, prices):
    """Pure absolute momentum (no relative SPY-vs-EFA gate). Each leg trades alone."""
    return _bt_dual_momentum_generic(
        returns, prices,
        lookback_periods={"6m": 126, "12m": 252},
        lookback_weights={"6m": 0.5, "12m": 0.5},
        skip_days=21, leverage="2x",
        signal_mode="absolute_only",
    )


def _bt_abs_dm_dd_stop(returns, prices, dd_threshold=0.15):
    """
    Absolute-only DM with intra-month drawdown stop:
      • Track running NAV high-water mark
      • If current equity position drops > dd_threshold from peak, force BND immediately
      • Re-enter on next monthly rebal if signal is still bullish
    Keeps the high CAGR of absolute-only while cutting tail drawdowns.
    """
    spy = prices["SPY"]
    efa = prices["EFA"]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    spuu = spliced_leveraged_etf(returns, "SPUU")
    efo = spliced_leveraged_etf(returns, "EFO")
    bnd = returns["BND"].fillna(0) if "BND" in returns.columns else pd.Series(0.0, index=returns.index)

    current = "BND"
    nav = 1.0
    peak = 1.0
    out = []

    for d in returns.index:
        # Today's return uses yesterday's decision
        if current == "SPUU":
            r = spuu.loc[d]
        elif current == "EFO":
            r = efo.loc[d]
        else:
            r = bnd.loc[d]
        out.append(r)
        nav *= (1 + r)
        if current in ("SPUU", "EFO"):
            peak = max(peak, nav)
            dd = (nav - peak) / peak if peak > 0 else 0
            if dd < -dd_threshold:
                current = "BND"   # Trip stop — go to safety
                peak = nav

        # Monthly rebal — re-evaluate signal
        if d in rebal_dates and d - pd.Timedelta(days=400) >= returns.index[0]:
            sig = _dm_step(d, spy, efa, {"6m": 126, "12m": 252},
                           {"6m": 0.5, "12m": 0.5}, 21, None,
                           signal_only_mode="absolute_only")
            if sig is not None:
                new_pos = "SPUU" if sig == "SPUU_or_1x_SPY" else (
                          "EFO" if sig == "EFO_or_1x_EFA" else "BND")
                if new_pos != current:
                    current = new_pos
                    peak = nav  # Reset peak for new position
    return pd.Series(out, index=returns.index)


def bt_dual_momentum_abs_dd15(returns, prices):
    """Absolute-only DM with 15% drawdown stop."""
    return _bt_abs_dm_dd_stop(returns, prices, dd_threshold=0.15)


def bt_dual_momentum_abs_dd10(returns, prices):
    """Absolute-only DM with tighter 10% drawdown stop."""
    return _bt_abs_dm_dd_stop(returns, prices, dd_threshold=0.10)


def bt_dual_momentum_abs_dd20(returns, prices):
    """Absolute-only DM with looser 20% drawdown stop."""
    return _bt_abs_dm_dd_stop(returns, prices, dd_threshold=0.20)


def bt_dual_momentum_abs_dd25(returns, prices):
    """Absolute-only DM with 25% drawdown stop."""
    return _bt_abs_dm_dd_stop(returns, prices, dd_threshold=0.25)


def bt_dual_momentum_abs_dd30(returns, prices):
    """Absolute-only DM with 30% drawdown stop (very loose, catches only major crashes)."""
    return _bt_abs_dm_dd_stop(returns, prices, dd_threshold=0.30)


def bt_dual_momentum_abs_dd20_tmf(returns, prices):
    """Combined: 70% DD-stop-20 base + 30% TMF (combine the two best mitigations)."""
    base = bt_dual_momentum_abs_dd20(returns, prices)
    tmf = spliced_leveraged_etf(returns, "TMF")
    return 0.7 * base + 0.3 * tmf


def _bt_dm_dd_custom(returns, prices,
                     us_signal="SPY", us_position="SPUU",
                     intl_signal="EFA", intl_position="EFO",
                     defensive="BND", dd_threshold=0.20,
                     lookbacks=None, weights=None, skip_days=21,
                     us_score_threshold=0.01, intl_score_threshold=0.01):
    """
    Generic absolute-DM + DD-stop with fully configurable position assets.
    Signal source (us_signal/intl_signal) can differ from the position asset —
    e.g., signal on QQQ but hold TQQQ; signal on SPY but hold UPRO.
    """
    if lookbacks is None:
        lookbacks = {"6m": 126, "12m": 252}
    if weights is None:
        weights = {"6m": 0.5, "12m": 0.5}

    us_sig = prices[us_signal]
    intl_sig = prices[intl_signal]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))

    def _ret_series(ticker):
        if ticker in SYNTH_LEV_ETFS:
            return spliced_leveraged_etf(returns, ticker)
        return returns[ticker].fillna(0) if ticker in returns.columns else pd.Series(0.0, index=returns.index)

    us_pos_ret = _ret_series(us_position)
    intl_pos_ret = _ret_series(intl_position)
    def_ret = _ret_series(defensive)

    current = "DEF"
    nav = 1.0
    peak = 1.0
    out = []

    for d in returns.index:
        if current == "US":
            r = us_pos_ret.loc[d]
        elif current == "INTL":
            r = intl_pos_ret.loc[d]
        else:
            r = def_ret.loc[d]
        out.append(r)
        nav *= (1 + r)

        # DD stop
        if current in ("US", "INTL"):
            peak = max(peak, nav)
            dd = (nav - peak) / peak if peak > 0 else 0
            if dd < -dd_threshold:
                current = "DEF"
                peak = nav

        # Monthly rebal
        if d in rebal_dates and d - pd.Timedelta(days=400) >= returns.index[0]:
            try:
                sd = d - pd.Timedelta(days=skip_days)
                us_now = us_sig.asof(sd)
                intl_now = intl_sig.asof(sd)
                if pd.isna(us_now) or pd.isna(intl_now) or us_now <= 0 or intl_now <= 0:
                    continue
                us_score = 0
                intl_score = 0
                ok = True
                for label, days in lookbacks.items():
                    us_past = us_sig.asof(d - pd.Timedelta(days=int(days * 1.45)))
                    intl_past = intl_sig.asof(d - pd.Timedelta(days=int(days * 1.45)))
                    if pd.isna(us_past) or us_past <= 0 or pd.isna(intl_past) or intl_past <= 0:
                        ok = False
                        break
                    us_score += weights[label] * (us_now / us_past - 1)
                    intl_score += weights[label] * (intl_now / intl_past - 1)
                if not ok:
                    continue
                a = us_score > us_score_threshold
                b = intl_score > intl_score_threshold
                new_pos = "US" if a else ("INTL" if b else "DEF")
                if new_pos != current:
                    current = new_pos
                    peak = nav
            except Exception:
                pass
    return pd.Series(out, index=returns.index)


def bt_dm_dd20_upro(returns, prices):
    """DD20 + UPRO (3× S&P) instead of SPUU (2×) when US is winning."""
    return _bt_dm_dd_custom(returns, prices, us_position="UPRO", dd_threshold=0.20)


def bt_dm_dd30_upro(returns, prices):
    """DD30 + UPRO (3× S&P) — wider stop to accommodate higher leverage volatility."""
    return _bt_dm_dd_custom(returns, prices, us_position="UPRO", dd_threshold=0.30)


def bt_dm_dd20_tqqq(returns, prices):
    """DD20 + TQQQ (3× Nasdaq) — signal on QQQ, hold TQQQ when US wins."""
    return _bt_dm_dd_custom(returns, prices, us_signal="QQQ", us_position="TQQQ", dd_threshold=0.20)


def bt_dm_dd30_tqqq(returns, prices):
    """DD30 + TQQQ — wider stop for higher leverage."""
    return _bt_dm_dd_custom(returns, prices, us_signal="QQQ", us_position="TQQQ", dd_threshold=0.30)


def bt_dm_dd20_tmf_defensive(returns, prices):
    """SPUU/EFO + TMF (3× bonds) defensive instead of BND."""
    return _bt_dm_dd_custom(returns, prices, defensive="TMF", dd_threshold=0.20)


def bt_dm_dd30_upro_tmf(returns, prices):
    """Stacked: UPRO + TMF, wider DD-30 to handle higher overall vol."""
    return _bt_dm_dd_custom(returns, prices, us_position="UPRO", defensive="TMF", dd_threshold=0.30)


def bt_dm_dd30_tqqq_tmf(returns, prices):
    """TQQQ + TMF, DD-30. Most aggressive combo: tech-tilted 3× + leveraged bond hedge."""
    return _bt_dm_dd_custom(returns, prices, us_signal="QQQ", us_position="TQQQ",
                            defensive="TMF", dd_threshold=0.30)


def bt_dm_dd20_best_of_3(returns, prices, dd_threshold=0.20):
    """
    3-asset best-of-three: rank SPY, QQQ, EFA by blended momentum (skip-1m).
    Pick the highest-scoring that's >1%. Hold UPRO / TQQQ / EFO accordingly.
    Defensive: BND.
    """
    spy = prices["SPY"]
    qqq = prices["QQQ"]
    efa = prices["EFA"]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    upro = spliced_leveraged_etf(returns, "UPRO")
    tqqq = spliced_leveraged_etf(returns, "TQQQ")
    efo = spliced_leveraged_etf(returns, "EFO")
    bnd = returns["BND"].fillna(0) if "BND" in returns.columns else pd.Series(0.0, index=returns.index)

    lookbacks = {"6m": 126, "12m": 252}
    weights = {"6m": 0.5, "12m": 0.5}

    current = "DEF"
    nav = 1.0
    peak = 1.0
    out = []

    for d in returns.index:
        if current == "UPRO":
            r = upro.loc[d]
        elif current == "TQQQ":
            r = tqqq.loc[d]
        elif current == "EFO":
            r = efo.loc[d]
        else:
            r = bnd.loc[d]
        out.append(r)
        nav *= (1 + r)
        if current in ("UPRO", "TQQQ", "EFO"):
            peak = max(peak, nav)
            dd = (nav - peak) / peak if peak > 0 else 0
            if dd < -dd_threshold:
                current = "DEF"
                peak = nav

        if d in rebal_dates and d - pd.Timedelta(days=400) >= returns.index[0]:
            try:
                sd = d - pd.Timedelta(days=21)
                scores = {}
                for sig_name, sig_series, pos_name in [
                    ("SPY", spy, "UPRO"),
                    ("QQQ", qqq, "TQQQ"),
                    ("EFA", efa, "EFO"),
                ]:
                    s_now = sig_series.asof(sd)
                    if pd.isna(s_now) or s_now <= 0:
                        continue
                    score = 0
                    ok = True
                    for label, days in lookbacks.items():
                        s_past = sig_series.asof(d - pd.Timedelta(days=int(days * 1.45)))
                        if pd.isna(s_past) or s_past <= 0:
                            ok = False
                            break
                        score += weights[label] * (s_now / s_past - 1)
                    if ok:
                        scores[pos_name] = score
                if scores:
                    best_pos, best_score = max(scores.items(), key=lambda kv: kv[1])
                    new_pos = best_pos if best_score > 0.01 else "DEF"
                    if new_pos != current:
                        current = new_pos
                        peak = nav
            except Exception:
                pass
    return pd.Series(out, index=returns.index)


def bt_dm_dd30_best_of_3(returns, prices):
    """Best-of-3 with DD-30 (wider stop for the more volatile 3× ETFs)."""
    return bt_dm_dd20_best_of_3(returns, prices, dd_threshold=0.30)


def bt_dm_dd30_upro_3m6m(returns, prices):
    """Winner architecture + faster lookback (3m+6m blend, skip-1m)."""
    return _bt_dm_dd_custom(
        returns, prices,
        us_position="UPRO", dd_threshold=0.30,
        lookbacks={"3m": 63, "6m": 126},
        weights={"3m": 0.5, "6m": 0.5},
    )


def bt_dm_dd30_upro_1m3m(returns, prices):
    """Winner + very fast lookback (1m+3m+6m blend)."""
    return _bt_dm_dd_custom(
        returns, prices,
        us_position="UPRO", dd_threshold=0.30,
        lookbacks={"1m": 21, "3m": 63, "6m": 126},
        weights={"1m": 0.34, "3m": 0.33, "6m": 0.33},
    )


def bt_dm_conditional_leverage(returns, prices):
    """
    Conditional leverage by signal strength:
      • SPY blended momentum 1%-15% → SPUU (2× S&P)
      • SPY momentum > 15%          → UPRO (3× S&P) — high-conviction
      • EFA momentum > 1%           → EFO (2× MSCI EAFE)
      • Else                         → BND
    DD-stop = 30% on UPRO positions, 20% on SPUU/EFO positions.
    Idea: only crank leverage to 3× when momentum is genuinely strong.
    """
    spy = prices["SPY"]
    efa = prices["EFA"]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    upro = spliced_leveraged_etf(returns, "UPRO")
    spuu = spliced_leveraged_etf(returns, "SPUU")
    efo = spliced_leveraged_etf(returns, "EFO")
    bnd = returns["BND"].fillna(0) if "BND" in returns.columns else pd.Series(0.0, index=returns.index)

    current = "DEF"
    nav = 1.0
    peak = 1.0
    out = []
    for d in returns.index:
        if current == "UPRO":
            r = upro.loc[d]; dd_t = 0.30
        elif current == "SPUU":
            r = spuu.loc[d]; dd_t = 0.20
        elif current == "EFO":
            r = efo.loc[d]; dd_t = 0.20
        else:
            r = bnd.loc[d]; dd_t = None
        out.append(r)
        nav *= (1 + r)
        if dd_t is not None:
            peak = max(peak, nav)
            dd = (nav - peak) / peak if peak > 0 else 0
            if dd < -dd_t:
                current = "DEF"
                peak = nav

        if d in rebal_dates and d - pd.Timedelta(days=400) >= returns.index[0]:
            try:
                sd = d - pd.Timedelta(days=21)
                spy_now = spy.asof(sd)
                efa_now = efa.asof(sd)
                if pd.isna(spy_now) or pd.isna(efa_now):
                    continue
                spy_score = 0
                efa_score = 0
                ok = True
                for label, days in {"6m": 126, "12m": 252}.items():
                    spy_past = spy.asof(d - pd.Timedelta(days=int(days * 1.45)))
                    efa_past = efa.asof(d - pd.Timedelta(days=int(days * 1.45)))
                    if pd.isna(spy_past) or pd.isna(efa_past) or spy_past <= 0 or efa_past <= 0:
                        ok = False
                        break
                    spy_score += 0.5 * (spy_now / spy_past - 1)
                    efa_score += 0.5 * (efa_now / efa_past - 1)
                if not ok:
                    continue

                if spy_score > 0.15:
                    new_pos = "UPRO"
                elif spy_score > 0.01:
                    new_pos = "SPUU"
                elif efa_score > 0.01:
                    new_pos = "EFO"
                else:
                    new_pos = "DEF"
                if new_pos != current:
                    current = new_pos
                    peak = nav
            except Exception:
                pass
    return pd.Series(out, index=returns.index)


def bt_dm_dd30_upro_vix_kill(returns, prices, vix_df, vix_spike=35):
    """Winner + intra-month VIX kill switch: force BND if VIX > vix_spike."""
    spy = prices["SPY"]
    efa = prices["EFA"]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    upro = spliced_leveraged_etf(returns, "UPRO")
    efo = spliced_leveraged_etf(returns, "EFO")
    bnd = returns["BND"].fillna(0) if "BND" in returns.columns else pd.Series(0.0, index=returns.index)
    vix_aligned = (vix_df["value"].reindex(returns.index, method="ffill")
                   if vix_df is not None else pd.Series(20.0, index=returns.index))

    current = "DEF"
    nav = 1.0
    peak = 1.0
    out = []
    for d in returns.index:
        if current == "UPRO":
            r = upro.loc[d]
        elif current == "EFO":
            r = efo.loc[d]
        else:
            r = bnd.loc[d]
        out.append(r)
        nav *= (1 + r)

        # Intra-month kill switch
        if current in ("UPRO", "EFO"):
            vix_today = vix_aligned.loc[d] if not pd.isna(vix_aligned.loc[d]) else 20.0
            if vix_today > vix_spike:
                current = "DEF"
                peak = nav
            else:
                peak = max(peak, nav)
                dd = (nav - peak) / peak if peak > 0 else 0
                if dd < -0.30:
                    current = "DEF"
                    peak = nav

        if d in rebal_dates and d - pd.Timedelta(days=400) >= returns.index[0]:
            try:
                sd = d - pd.Timedelta(days=21)
                spy_now = spy.asof(sd)
                efa_now = efa.asof(sd)
                if pd.isna(spy_now) or pd.isna(efa_now):
                    continue
                spy_score = 0
                efa_score = 0
                for days in (126, 252):
                    spy_past = spy.asof(d - pd.Timedelta(days=int(days * 1.45)))
                    efa_past = efa.asof(d - pd.Timedelta(days=int(days * 1.45)))
                    if pd.isna(spy_past) or pd.isna(efa_past) or spy_past <= 0 or efa_past <= 0:
                        continue
                    spy_score += 0.5 * (spy_now / spy_past - 1)
                    efa_score += 0.5 * (efa_now / efa_past - 1)
                new_pos = "UPRO" if spy_score > 0.01 else ("EFO" if efa_score > 0.01 else "DEF")
                if new_pos != current:
                    current = new_pos
                    peak = nav
            except Exception:
                pass
    return pd.Series(out, index=returns.index)


def bt_dm_dd30_best_of_4(returns, prices):
    """Wider universe: UPRO/TQQQ/EFO/EDC. Skip SOXL (too volatile). DD30."""
    spy = prices["SPY"]
    qqq = prices["QQQ"]
    efa = prices["EFA"]
    eem = prices["EEM"]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    upro = spliced_leveraged_etf(returns, "UPRO")
    tqqq = spliced_leveraged_etf(returns, "TQQQ")
    efo = spliced_leveraged_etf(returns, "EFO")
    edc = spliced_leveraged_etf(returns, "EDC")
    bnd = returns["BND"].fillna(0) if "BND" in returns.columns else pd.Series(0.0, index=returns.index)

    current = "DEF"
    nav = 1.0
    peak = 1.0
    out = []
    universe = [("SPY", spy, "UPRO", upro), ("QQQ", qqq, "TQQQ", tqqq),
                ("EFA", efa, "EFO", efo), ("EEM", eem, "EDC", edc)]
    pos_to_ret = {p: r for _, _, p, r in universe}

    for d in returns.index:
        if current in pos_to_ret:
            r = pos_to_ret[current].loc[d]
        else:
            r = bnd.loc[d]
        out.append(r)
        nav *= (1 + r)
        if current in pos_to_ret:
            peak = max(peak, nav)
            dd = (nav - peak) / peak if peak > 0 else 0
            if dd < -0.30:
                current = "DEF"
                peak = nav

        if d in rebal_dates and d - pd.Timedelta(days=400) >= returns.index[0]:
            try:
                sd = d - pd.Timedelta(days=21)
                scores = {}
                for _, sig_series, pos_name, _ in universe:
                    s_now = sig_series.asof(sd)
                    if pd.isna(s_now) or s_now <= 0:
                        continue
                    score = 0
                    ok = True
                    for days in (126, 252):
                        s_past = sig_series.asof(d - pd.Timedelta(days=int(days * 1.45)))
                        if pd.isna(s_past) or s_past <= 0:
                            ok = False
                            break
                        score += 0.5 * (s_now / s_past - 1)
                    if ok:
                        scores[pos_name] = score
                if scores:
                    best_pos, best_score = max(scores.items(), key=lambda kv: kv[1])
                    new_pos = best_pos if best_score > 0.01 else "DEF"
                    if new_pos != current:
                        current = new_pos
                        peak = nav
            except Exception:
                pass
    return pd.Series(out, index=returns.index)


def _bt_dm_vol_target_custom(returns, prices,
                              us_position="UPRO", intl_position="EFO",
                              defensive="BND", dd_threshold=0.30, target_vol=0.25,
                              vol_window=60,
                              lookbacks=None, weights=None, skip_days=21):
    """
    Generic vol-targeted DM + DD-stop. Scales position size by
    min(1.0, target_vol / trailing_realized_vol). Excess parks in defensive.
    """
    if lookbacks is None:
        lookbacks = {"6m": 126, "12m": 252}
    if weights is None:
        weights = {"6m": 0.5, "12m": 0.5}

    spy = prices["SPY"]
    efa = prices["EFA"]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))

    def _ret_series(ticker):
        if ticker in SYNTH_LEV_ETFS:
            return spliced_leveraged_etf(returns, ticker)
        return returns[ticker].fillna(0) if ticker in returns.columns else pd.Series(0.0, index=returns.index)

    us_pos_ret = _ret_series(us_position)
    intl_pos_ret = _ret_series(intl_position)
    def_ret = _ret_series(defensive)
    us_vol = us_pos_ret.rolling(vol_window).std() * np.sqrt(252)
    intl_vol = intl_pos_ret.rolling(vol_window).std() * np.sqrt(252)

    current = "DEF"
    nav = 1.0
    peak = 1.0
    out = []
    for d in returns.index:
        if current == "US":
            v = us_vol.loc[d] if not pd.isna(us_vol.loc[d]) else target_vol
            scale = min(1.0, target_vol / v) if v > 0 else 1.0
            r = scale * us_pos_ret.loc[d] + (1 - scale) * def_ret.loc[d]
        elif current == "INTL":
            v = intl_vol.loc[d] if not pd.isna(intl_vol.loc[d]) else target_vol
            scale = min(1.0, target_vol / v) if v > 0 else 1.0
            r = scale * intl_pos_ret.loc[d] + (1 - scale) * def_ret.loc[d]
        else:
            r = def_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        if current in ("US", "INTL"):
            peak = max(peak, nav)
            dd = (nav - peak) / peak if peak > 0 else 0
            if dd < -dd_threshold:
                current = "DEF"
                peak = nav

        if d in rebal_dates and d - pd.Timedelta(days=400) >= returns.index[0]:
            try:
                sd = d - pd.Timedelta(days=skip_days)
                spy_now = spy.asof(sd)
                efa_now = efa.asof(sd)
                if pd.isna(spy_now) or pd.isna(efa_now):
                    continue
                spy_score = 0
                efa_score = 0
                ok = True
                for label, days in lookbacks.items():
                    spy_past = spy.asof(d - pd.Timedelta(days=int(days * 1.45)))
                    efa_past = efa.asof(d - pd.Timedelta(days=int(days * 1.45)))
                    if pd.isna(spy_past) or pd.isna(efa_past) or spy_past <= 0 or efa_past <= 0:
                        ok = False
                        break
                    spy_score += weights[label] * (spy_now / spy_past - 1)
                    efa_score += weights[label] * (efa_now / efa_past - 1)
                if not ok:
                    continue
                new_pos = "US" if spy_score > 0.01 else ("INTL" if efa_score > 0.01 else "DEF")
                if new_pos != current:
                    current = new_pos
                    peak = nav
            except Exception:
                pass
    return pd.Series(out, index=returns.index)


def bt_dm_dd20_spuu_vol25(returns, prices):
    """Baseline (DD20 + SPUU 2×) with 25% vol target."""
    return _bt_dm_vol_target_custom(returns, prices, us_position="SPUU", dd_threshold=0.20, target_vol=0.25)


def bt_dm_dd20_spuu_vol20(returns, prices):
    """Baseline (DD20 + SPUU 2×) with 20% vol target — tighter."""
    return _bt_dm_vol_target_custom(returns, prices, us_position="SPUU", dd_threshold=0.20, target_vol=0.20)


def bt_dm_dd20_spuu_vol18(returns, prices):
    """SPUU 2× + DD20 + vol-target 18% — even tighter exposure."""
    return _bt_dm_vol_target_custom(returns, prices, us_position="SPUU", dd_threshold=0.20, target_vol=0.18)


def bt_dm_dd20_spuu_vol15(returns, prices):
    """SPUU 2× + DD20 + vol-target 15% — very tight."""
    return _bt_dm_vol_target_custom(returns, prices, us_position="SPUU", dd_threshold=0.20, target_vol=0.15)


def bt_dm_dd15_spuu_vol20(returns, prices):
    """SPUU 2× + DD15 + vol-target 20% — tighter DD stop."""
    return _bt_dm_vol_target_custom(returns, prices, us_position="SPUU", dd_threshold=0.15, target_vol=0.20)


def bt_dm_dd25_spuu_vol20(returns, prices):
    """SPUU 2× + DD25 + vol-target 20% — looser DD stop."""
    return _bt_dm_vol_target_custom(returns, prices, us_position="SPUU", dd_threshold=0.25, target_vol=0.20)


# === High-CAGR sweep (≤2× leverage, target ≥14% CAGR) ===
# Lever loosen DD-stop + lift vol target to push CAGR. Sharpe will drop but
# we accept that trade if CAGR clears 14%.

def bt_dm_dd30_spuu_vol20(returns, prices):
    """SPUU 2× + DD30 + vol-target 20% — loosen DD-stop."""
    return _bt_dm_vol_target_custom(returns, prices, us_position="SPUU", dd_threshold=0.30, target_vol=0.20)


def bt_dm_dd30_spuu_vol25(returns, prices):
    """SPUU 2× + DD30 + vol-target 25% — loosen DD + higher vol cap."""
    return _bt_dm_vol_target_custom(returns, prices, us_position="SPUU", dd_threshold=0.30, target_vol=0.25)


def bt_dm_dd30_spuu_vol30(returns, prices):
    """SPUU 2× + DD30 + vol-target 30% — nearly pure SPUU when on."""
    return _bt_dm_vol_target_custom(returns, prices, us_position="SPUU", dd_threshold=0.30, target_vol=0.30)


def bt_dm_dd40_spuu_vol25(returns, prices):
    """SPUU 2× + DD40 + vol-target 25% — DD-stop rarely fires."""
    return _bt_dm_vol_target_custom(returns, prices, us_position="SPUU", dd_threshold=0.40, target_vol=0.25)


def bt_dm_nodd_spuu_vol22(returns, prices):
    """SPUU 2× + no DD-stop (99%) + vol-target 22% — pure vol-target play."""
    return _bt_dm_vol_target_custom(returns, prices, us_position="SPUU", dd_threshold=0.99, target_vol=0.22)


def bt_dm_dd30_spuu_vol25_tlt(returns, prices):
    """SPUU 2× + DD30 + vol-target 25% + TLT (1× long bonds) as defensive instead of BND.

    Rationale: BND is short-duration; TLT adds crisis-alpha duration that
    typically rallies when SPUU is drawn down, lifting vol-scaled returns.
    """
    return _bt_dm_vol_target_custom(returns, prices, us_position="SPUU", defensive="TLT",
                                     dd_threshold=0.30, target_vol=0.25)


# === Multi-asset 2× best-of-N (asset-class expansion within 2× cap) ===
# Strategy picks the strongest blended-momentum candidate each month from a
# configurable 2×-leveraged asset universe. Defensive: BND or TLT.

def _bt_dm_2x_multi_asset(returns, prices,
                          candidates,        # list of (signal_ticker, position_ticker) tuples
                          defensive="BND",
                          dd_threshold=0.30, target_vol=0.25,
                          vol_window=60,
                          lookbacks=None, weights=None, skip_days=21,
                          min_score=0.01,
                          return_weights=False):
    """
    Generic multi-asset DM with vol-target + DD-stop.

    candidates: list of (signal_price_ticker, position_return_ticker)
                e.g. [("SPY","SPUU"), ("QQQ","QLD"), ("EFA","EFO"), ("IWM","SAA")]

    Signal ticker = the underlying 1× price used to compute momentum.
    Position ticker = the actual (leveraged) ETF held when that candidate wins.

    Each month the strategy picks the candidate with the strongest blended
    momentum (skip-1m). If the winner's score < min_score, hold defensive.
    Position is vol-scaled to `target_vol`; excess parks in defensive.
    Trailing peak-NAV DD-stop forces defensive when DD exceeds threshold.
    """
    if lookbacks is None:
        lookbacks = {"6m": 126, "12m": 252}
    if weights is None:
        weights = {"6m": 0.5, "12m": 0.5}

    rebal_dates = sorted(_monthly_rebal_dates(returns.index))

    def _ret_series(ticker):
        if ticker in SYNTH_LEV_ETFS:
            return spliced_leveraged_etf(returns, ticker)
        return returns[ticker].fillna(0) if ticker in returns.columns else pd.Series(0.0, index=returns.index)

    # Pre-compute return series + trailing vol for each position candidate
    pos_returns = {pos: _ret_series(pos) for _, pos in candidates}
    pos_vols = {pos: pr.rolling(vol_window).std() * np.sqrt(252) for pos, pr in pos_returns.items()}
    signal_prices = {sig: prices[sig] for sig, _ in candidates if sig in prices.columns}
    def_ret = _ret_series(defensive)

    current = "DEF"
    nav = 1.0
    peak = 1.0
    out = []
    # Track (date, scale, current_pos) on changes so we can build a weights timeline
    weight_log: list[tuple[pd.Timestamp, str, float]] = [(returns.index[0], "DEF", 1.0)]
    prev_state = ("DEF", 1.0)
    for d in returns.index:
        if current in pos_returns:
            v = pos_vols[current].loc[d] if not pd.isna(pos_vols[current].loc[d]) else target_vol
            scale = min(1.0, target_vol / v) if v > 0 else 1.0
            r = scale * pos_returns[current].loc[d] + (1 - scale) * def_ret.loc[d]
        else:
            scale = 0.0
            r = def_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        if current in pos_returns:
            peak = max(peak, nav)
            dd = (nav - peak) / peak if peak > 0 else 0
            if dd < -dd_threshold:
                current = "DEF"
                peak = nav

        if d in rebal_dates and d - pd.Timedelta(days=400) >= returns.index[0]:
            try:
                sd = d - pd.Timedelta(days=skip_days)
                scores = {}
                for sig, pos in candidates:
                    if sig not in signal_prices:
                        continue
                    s_now = signal_prices[sig].asof(sd)
                    if pd.isna(s_now) or s_now <= 0:
                        continue
                    score = 0
                    ok = True
                    for label, days in lookbacks.items():
                        s_past = signal_prices[sig].asof(d - pd.Timedelta(days=int(days * 1.45)))
                        if pd.isna(s_past) or s_past <= 0:
                            ok = False
                            break
                        score += weights[label] * (s_now / s_past - 1)
                    if ok:
                        scores[pos] = score
                if scores:
                    best_pos, best_score = max(scores.items(), key=lambda kv: kv[1])
                    new_pos = best_pos if best_score > min_score else "DEF"
                    if new_pos != current:
                        current = new_pos
                        peak = nav
            except Exception:
                pass

        # End-of-day weight snapshot (post any state change). Vol-scale is
        # recomputed on the next bar but we log per-day current+scale anyway.
        new_state = (current, round(scale, 4) if current in pos_returns else 0.0)
        if new_state != prev_state:
            weight_log.append((d, current, scale if current in pos_returns else 0.0))
            prev_state = new_state

    ret = pd.Series(out, index=returns.index)
    if not return_weights:
        return ret

    # Build asset_returns DataFrame with every position-ticker + defensive
    all_pos_tickers = list({pos for _, pos in candidates})
    asset_returns_dict = {pos: pos_returns[pos] for pos in all_pos_tickers}
    if defensive not in asset_returns_dict:
        asset_returns_dict[defensive] = def_ret
    asset_returns_df = pd.DataFrame(asset_returns_dict).fillna(0.0)

    # Build weights timeline: weight per ticker on each state change
    rows = []
    for d, pos_name, sc in weight_log:
        row = {t: 0.0 for t in asset_returns_dict.keys()}
        if pos_name in asset_returns_dict:
            row[pos_name] = float(sc)
            row[defensive] = 1.0 - float(sc)
        else:
            row[defensive] = 1.0
        rows.append(row)
    weights_df = pd.DataFrame(rows, index=pd.DatetimeIndex([d for d, _, _ in weight_log]))
    return ret, {"weights": weights_df, "asset_returns": asset_returns_df}


def bt_dm_2x_best_of_3_dd30_vol25(returns, prices, return_weights: bool = False):
    """Best of (SPUU, QLD, EFO) with DD30 + vol-target 25%."""
    return _bt_dm_2x_multi_asset(
        returns, prices,
        candidates=[("SPY", "SPUU"), ("QQQ", "QLD"), ("EFA", "EFO")],
        dd_threshold=0.30, target_vol=0.25,
        return_weights=return_weights,
    )


def bt_dm_2x_best_of_4_dd30_vol25(returns, prices):
    """Best of (SPUU, QLD, EFO, SAA) with DD30 + vol-target 25%."""
    return _bt_dm_2x_multi_asset(
        returns, prices,
        candidates=[("SPY", "SPUU"), ("QQQ", "QLD"), ("EFA", "EFO"), ("IWM", "SAA")],
        dd_threshold=0.30, target_vol=0.25,
    )


def bt_dm_2x_best_of_5_dd30_vol25(returns, prices):
    """Best of (SPUU, QLD, EFO, SAA, EET) with DD30 + vol-target 25%."""
    return _bt_dm_2x_multi_asset(
        returns, prices,
        candidates=[("SPY", "SPUU"), ("QQQ", "QLD"), ("EFA", "EFO"),
                    ("IWM", "SAA"), ("EEM", "EET")],
        dd_threshold=0.30, target_vol=0.25,
    )


def bt_dm_2x_best_of_4_gold_dd30_vol25(returns, prices):
    """Best of (SPUU, QLD, EFO, GLD) — gold (1×) competes as inflation/crisis hedge.

    Note: GLD is 1× — when gold wins, effective leverage drops, which is a
    feature: gold typically leads during stagflationary regimes.
    """
    return _bt_dm_2x_multi_asset(
        returns, prices,
        candidates=[("SPY", "SPUU"), ("QQQ", "QLD"), ("EFA", "EFO"), ("GLD", "GLD")],
        dd_threshold=0.30, target_vol=0.25,
    )


def bt_dm_2x_best_of_5_gold_dd30_vol25(returns, prices):
    """Best of (SPUU, QLD, EFO, SAA, GLD) — 4 equity 2× + gold 1×."""
    return _bt_dm_2x_multi_asset(
        returns, prices,
        candidates=[("SPY", "SPUU"), ("QQQ", "QLD"), ("EFA", "EFO"),
                    ("IWM", "SAA"), ("GLD", "GLD")],
        dd_threshold=0.30, target_vol=0.25,
    )


def bt_dm_2x_best_of_3_dd30_vol30(returns, prices):
    """Best of (SPUU, QLD, EFO) + DD30 + vol-30% (more aggressive vol cap)."""
    return _bt_dm_2x_multi_asset(
        returns, prices,
        candidates=[("SPY", "SPUU"), ("QQQ", "QLD"), ("EFA", "EFO")],
        dd_threshold=0.30, target_vol=0.30,
    )


def bt_dm_2x_qld_only_dd30_vol25(returns, prices):
    """Single-asset QLD (2× Nasdaq) with DM gate + DD30 + vol-25.

    Pure tech-tilted DM. Use QQQ as signal; only enter if QQQ momentum > 1%.
    """
    return _bt_dm_2x_multi_asset(
        returns, prices,
        candidates=[("QQQ", "QLD")],
        dd_threshold=0.30, target_vol=0.25,
    )


def bt_dm_dd20_spuu_vol20_fast(returns, prices):
    """SPUU 2× + DD20 + vol-target 20% + faster 3m+6m signal blend."""
    return _bt_dm_vol_target_custom(
        returns, prices, us_position="SPUU", dd_threshold=0.20, target_vol=0.20,
        lookbacks={"3m": 63, "6m": 126},
        weights={"3m": 0.5, "6m": 0.5},
    )


# ════════════════════════════════════════════════════════════════════════
# NEW PORTFOLIO-ADDITION CANDIDATES — multi-asset strategies grounded in
# academic literature, designed to diversify the existing 7-sleeve mix.
# ════════════════════════════════════════════════════════════════════════


def _trailing_return(prices: pd.Series, end_date, days: int):
    """Trailing return over `days` trading days ending at end_date."""
    try:
        pos = prices.index.get_indexer([end_date], method="ffill")[0]
        if pos < days:
            return None
        p_now = prices.iloc[pos]
        p_past = prices.iloc[pos - days]
        if p_past <= 0 or pd.isna(p_now) or pd.isna(p_past):
            return None
        return float(p_now / p_past - 1)
    except Exception:
        return None


def _trailing_vol(returns: pd.Series, end_date, window: int = 60):
    """60-day annualized realized vol ending at end_date."""
    try:
        pos = returns.index.get_indexer([end_date], method="ffill")[0]
        if pos < window:
            return None
        sub = returns.iloc[pos - window + 1:pos + 1]
        v = float(sub.std()) * np.sqrt(252)
        return v if v > 0 else None
    except Exception:
        return None


def bt_faber_gtaa_7asset(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """
    Mebane Faber (2007) "A Quantitative Approach to Tactical Asset Allocation."

    Universe: SPY, EFA, EEM, IEF, TLT, GLD, DBC (broad asset classes).
    Rule: Each month, equal-weight to all assets above their 10-month SMA.
    Assets below SMA get cash (BIL). 0% in cash if all 7 are above.

    Original paper used 10-month SMA on monthly closes; we use 200-day SMA
    on daily closes (equivalent). Reference: Faber 2007 / 2013 update.
    Documented Sharpe ~0.7, MaxDD ~-10% over 1972-2012 — but multi-asset
    universe smooths returns significantly vs single-asset trend-following.
    """
    assets = ["SPY", "EFA", "EEM", "IEF", "TLT", "GLD", "DBC"]
    cash = "BIL"
    sma_period = 200
    available = [a for a in assets if a in prices.columns]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)

    # Pre-compute SMA for each asset
    smas = {a: prices[a].rolling(sma_period).mean() for a in available}

    weights = {a: 0.0 for a in available}
    weights["__cash__"] = 1.0
    out = []
    for d in returns.index:
        r = 0.0
        for a in available:
            r += weights[a] * returns[a].fillna(0).loc[d]
        r += weights["__cash__"] * cash_ret.loc[d]
        out.append(r)
        # Drift weights
        for a in available:
            weights[a] *= (1 + returns[a].fillna(0).loc[d])
        weights["__cash__"] *= (1 + cash_ret.loc[d])
        s = sum(weights.values())
        if s > 0:
            for k in weights:
                weights[k] /= s

        if d in rebal_dates:
            above = []
            for a in available:
                sma = smas[a].loc[d]
                p = prices[a].loc[d]
                if not pd.isna(sma) and not pd.isna(p) and p > sma:
                    above.append(a)
            new_w = {a: 0.0 for a in available}
            new_w["__cash__"] = 0.0
            if above:
                w_each = 1.0 / len(available)  # each asset gets 1/N if above SMA
                cash_w = 0.0
                for a in available:
                    if a in above:
                        new_w[a] = w_each
                    else:
                        cash_w += w_each
                new_w["__cash__"] = cash_w
            else:
                new_w["__cash__"] = 1.0
            weights = new_w
    return pd.Series(out, index=returns.index)


def bt_cross_asset_dual_momentum(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """
    Antonacci (2014) "Dual Momentum Investing" — cross-asset GEM extension.

    Universe: SPY (stocks), TLT (long bonds), GLD (gold), DBC (commodities).
    Rule: Monthly, pick the asset with strongest 12-month total return.
    Absolute momentum filter: only hold winner if its 12m return > 0%.
    Otherwise hold cash (BIL).

    Antonacci's original GEM was SPY vs EFA. Cross-asset extension to bonds/
    gold/commodities is well-supported (Asness/Moskowitz/Pedersen 2013, "Value
    and Momentum Everywhere"). Provides clean macro-regime rotation: stocks
    when risk-on, bonds in deflation, gold/commodities in inflation.
    """
    assets = ["SPY", "TLT", "GLD", "DBC"]
    cash = "BIL"
    lookback = 252
    available = [a for a in assets if a in prices.columns]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)

    current = "__cash__"
    out = []
    for d in returns.index:
        if current == "__cash__":
            r = cash_ret.loc[d]
        else:
            r = returns[current].fillna(0).loc[d]
        out.append(r)

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {}
            for a in available:
                tr = _trailing_return(prices[a], d, lookback)
                if tr is not None:
                    scores[a] = tr
            if scores:
                best, best_score = max(scores.items(), key=lambda kv: kv[1])
                current = best if best_score > 0 else "__cash__"
            else:
                current = "__cash__"
    return pd.Series(out, index=returns.index)


def bt_adaptive_asset_allocation(returns: pd.DataFrame, prices: pd.DataFrame,
                                  top_n: int = 3, lookback: int = 126) -> pd.Series:
    """
    Butler/Philbrick/Gordillo (2012) "Adaptive Asset Allocation."

    Universe: SPY, EFA, EEM, TLT, IEF, GLD, DBC (7 broad asset classes).
    Rule: Monthly:
      1. Rank all assets by 6-month total return.
      2. Select top-N (default 3).
      3. Weight by inverse 60-day vol (lower-vol assets get more weight).
      4. Cash (BIL) gets weight if any selected asset has negative momentum.

    Combines cross-sectional momentum (Jegadeesh-Titman 1993) with
    risk-parity weighting (Maillard et al 2010). Empirical Sharpe in the
    original paper: ~1.5 over 1995-2012. Robust to universe choice.
    """
    assets = ["SPY", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC"]
    cash = "BIL"
    available = [a for a in assets if a in prices.columns]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)

    weights = {a: 0.0 for a in available}
    weights["__cash__"] = 1.0
    out = []
    for d in returns.index:
        r = sum(weights[a] * returns[a].fillna(0).loc[d] for a in available)
        r += weights["__cash__"] * cash_ret.loc[d]
        out.append(r)
        # drift
        for a in available:
            weights[a] *= (1 + returns[a].fillna(0).loc[d])
        weights["__cash__"] *= (1 + cash_ret.loc[d])
        s = sum(weights.values())
        if s > 0:
            for k in weights:
                weights[k] /= s

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {}
            for a in available:
                tr = _trailing_return(prices[a], d, lookback)
                if tr is not None:
                    scores[a] = tr
            if not scores:
                continue
            # Pick top N
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            picks = [a for a, s in ranked[:top_n] if s > 0]
            if not picks:
                weights = {a: 0.0 for a in available}; weights["__cash__"] = 1.0
                continue
            # Inverse-vol weight
            invvols = {}
            for a in picks:
                v = _trailing_vol(returns[a], d, 60)
                if v is not None:
                    invvols[a] = 1.0 / v
            if not invvols:
                # Equal weight fallback
                w_each = 1.0 / len(picks)
                weights = {a: (w_each if a in picks else 0.0) for a in available}
                weights["__cash__"] = 0.0
                continue
            total_iv = sum(invvols.values())
            weights = {a: 0.0 for a in available}
            for a, iv in invvols.items():
                weights[a] = iv / total_iv
            weights["__cash__"] = 0.0
    return pd.Series(out, index=returns.index)


def bt_risk_parity_4asset(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """
    Bridgewater-style All Weather — 4-asset risk parity.

    Universe: SPY (stocks), TLT (long bonds), GLD (gold), DBC (commodities).
    Rule: Monthly rebalance to inverse-vol weights (60-day realized vol).
          Each asset gets weight ∝ 1/vol → equal expected risk contribution
          (approximately; full ERC requires solving for risk contribution).

    Theory: Maillard, Roncalli, Teiletche (2010) "Properties of Equally
    Weighted Risk Contribution Portfolios." Ray Dalio's All Weather (2005)
    uses similar logic across stocks/bonds/commodities/gold for regime
    diversification (growth-up/down × inflation-up/down).
    """
    assets = ["SPY", "TLT", "GLD", "DBC"]
    available = [a for a in assets if a in returns.columns]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))

    weights = {a: 1.0 / len(available) for a in available}
    out = []
    for d in returns.index:
        r = sum(weights[a] * returns[a].fillna(0).loc[d] for a in available)
        out.append(r)
        for a in available:
            weights[a] *= (1 + returns[a].fillna(0).loc[d])
        s = sum(weights.values())
        if s > 0:
            weights = {a: w / s for a, w in weights.items()}

        if d in rebal_dates:
            invvols = {}
            for a in available:
                v = _trailing_vol(returns[a], d, 60)
                if v is not None:
                    invvols[a] = 1.0 / v
            if not invvols:
                continue
            total = sum(invvols.values())
            weights = {a: invvols.get(a, 0.0) / total for a in available}
    return pd.Series(out, index=returns.index)


def bt_time_series_momentum(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """
    Moskowitz, Ooi, Pedersen (2012) "Time Series Momentum."

    Universe: SPY, EFA, EEM, TLT, IEF, GLD, DBC (7 asset classes).
    Rule: Each month, for each asset independently: take a LONG position
    if its trailing 12-month return is positive; otherwise hold cash for
    that slot. Weights are equal across the seven slots.

    Pure time-series momentum (different from our cross-sectional DM which
    only picks the BEST). Each asset is evaluated against ITSELF. MOP found
    significant alpha across 58 markets over 1985-2009.
    """
    assets = ["SPY", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC"]
    cash = "BIL"
    lookback = 252
    available = [a for a in assets if a in prices.columns]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)
    slot_weight = 1.0 / len(available)

    # Each slot is either "asset" or "cash"
    slot_pos = {a: "cash" for a in available}
    out = []
    for d in returns.index:
        r = 0.0
        for a in available:
            if slot_pos[a] == "asset":
                r += slot_weight * returns[a].fillna(0).loc[d]
            else:
                r += slot_weight * cash_ret.loc[d]
        out.append(r)

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            for a in available:
                tr = _trailing_return(prices[a], d, lookback)
                if tr is None:
                    continue
                slot_pos[a] = "asset" if tr > 0 else "cash"
    return pd.Series(out, index=returns.index)


def bt_spy_mean_reversion(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """
    Short-term mean reversion on SPY — De Bondt-Thaler (1985), Lo-MacKinlay (1990).

    Rule:
      • Trigger: SPY 5-day return < -5%
      • Action: Buy SPY (1× exposure); otherwise hold BIL (cash)
      • Exit: SPY recovers +3% from entry, OR 10 trading days elapse

    Theory: Behavioral overreaction to bad news → short-term reversal in
    equity prices, well-documented since the 1980s. Active strategy with
    irregular firing — most of the time the strategy is in cash.
    """
    if "SPY" not in returns.columns or "BIL" not in returns.columns:
        return pd.Series(0.0, index=returns.index)
    spy = prices["SPY"]
    spy_ret = returns["SPY"].fillna(0)
    bil_ret = returns["BIL"].fillna(0)
    in_position = False
    entry_price = None
    days_held = 0
    out = []
    for d in returns.index:
        if in_position:
            r = spy_ret.loc[d]
            days_held += 1
            current_p = spy.loc[d]
            if entry_price and current_p / entry_price - 1 >= 0.03:
                in_position = False; entry_price = None; days_held = 0
            elif days_held >= 10:
                in_position = False; entry_price = None; days_held = 0
        else:
            r = bil_ret.loc[d]
            # Check trigger
            pos = spy.index.get_indexer([d], method="ffill")[0]
            if pos >= 5:
                ret5 = spy.iloc[pos] / spy.iloc[pos - 5] - 1
                if ret5 < -0.05:
                    in_position = True
                    entry_price = float(spy.iloc[pos])
                    days_held = 0
        out.append(r)
    return pd.Series(out, index=returns.index)


# ════════════════════════════════════════════════════════════════════════
# LEVERAGED multi-asset variants — apply 2×/3× to AAA and Cross-Asset DM
# with the DD-stop + vol-target risk-control pattern from DM best-of-3.
# Goal: lift CAGR from 8-11% (unleveraged) toward 15-18% while keeping
# tail risk bounded.
# ════════════════════════════════════════════════════════════════════════


def synthetic_ntsd_returns(returns: pd.DataFrame, expense_ratio: float = 0.0035) -> pd.Series:
    """
    NTSD daily returns. NTSD (WisdomTree Efficient US Plus International Equity,
    launched 2026-03-19) is a capital-efficient stack:
      • 90% direct S&P 500 holdings
      • 10% cash collateralizing 60% notional MSCI EAFE futures exposure
      • Total: 150% notional, NOT daily-reset (no leverage decay)

    Preferred source: NTSDSIM (Testfolio's modeled returns 1970-2026) spliced
    with real NTSD post-2026-03-19. Loaded into `returns["NTSD"]` by the
    extended-data fetcher.

    Fallback (when NTSD column is absent): analytical stack
      Daily return ≈ 0.9 × SPY_total_return
                   + 0.6 × (EFA_total_return − BIL_financing)
                   − 0.0035/252
    """
    if "NTSD" in returns.columns:
        s = returns["NTSD"]
        if s.notna().sum() > 100:
            return s.fillna(0)
    spy = returns["SPY"].fillna(0) if "SPY" in returns.columns else pd.Series(0.0, index=returns.index)
    efa = returns["EFA"].fillna(0) if "EFA" in returns.columns else pd.Series(0.0, index=returns.index)
    bil = returns["BIL"].fillna(0) if "BIL" in returns.columns else pd.Series(0.0, index=returns.index)
    daily_er = expense_ratio / 252
    return 0.9 * spy + 0.6 * efa - 0.6 * bil - daily_er


def synthetic_ntsd_prices(returns: pd.DataFrame) -> pd.Series:
    """Cumulative price series for synthetic NTSD (starts at $100). Used for
    momentum signals via _trailing_return."""
    daily = synthetic_ntsd_returns(returns)
    return (1 + daily).cumprod() * 100.0


def _lev_etf_return(returns: pd.DataFrame, ticker: str) -> pd.Series:
    """Return series for a synthetic leveraged ETF if synthetic, else raw."""
    if ticker in SYNTH_LEV_ETFS:
        return spliced_leveraged_etf(returns, ticker)
    return returns[ticker].fillna(0) if ticker in returns.columns else pd.Series(0.0, index=returns.index)


# Map AAA's 1× universe → 2× leveraged equivalents (synthesized pre-inception).
# DBC has no clean 2× leveraged ETF → keep at 1×.
AAA_LEV2_MAP = {
    "SPY": "SPUU",   # 2× S&P 500
    "EFA": "EFO",    # 2× MSCI EAFE
    "EEM": "EET",    # 2× emerging markets
    "TLT": "UBT",    # 2× long Treasuries
    "IEF": "UST",    # 2× 7-10y Treasuries
    "GLD": "UGL",    # 2× gold
    "DBC": "DBC",    # commodities — no clean 2× ETF; kept at 1×
}


def bt_adaptive_asset_allocation_levered(
    returns: pd.DataFrame, prices: pd.DataFrame,
    top_n: int = 3, lookback: int = 126,
    dd_threshold: float = 0.30, target_vol: float = 0.25,
    vol_window: int = 60,
) -> pd.Series:
    """
    Adaptive Asset Allocation (Butler-Philbrick-Gordillo 2012) + 2× leverage
    on each held position + trailing-peak DD-stop + portfolio vol target.

    Selection logic identical to bt_adaptive_asset_allocation (top-N by 6m
    momentum, inverse-vol weighting), but each held position is the 2×
    leveraged ETF instead of the 1× underlying. Signal is computed on the
    1× underlying (cleaner momentum without leverage decay).

    Risk controls layered on top:
      • DD-stop 30%: if portfolio NAV drops 30% from peak → cash (BIL),
        reset peak when re-entering risk.
      • Vol-target: portfolio-level. Scale the entire weighted basket by
        min(1, target_vol / realized_portfolio_vol_60d). Excess → BIL.
    """
    signal_assets = ["SPY", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC"]
    cash = "BIL"
    available_signals = [a for a in signal_assets if a in prices.columns]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)

    # Pre-compute leveraged-position return series for each signal asset
    pos_returns = {a: _lev_etf_return(returns, AAA_LEV2_MAP.get(a, a)) for a in available_signals}

    # Current target weights (over leveraged positions); excess in cash
    weights = {a: 0.0 for a in available_signals}
    cash_weight = 1.0
    peak_nav = 1.0
    nav = 1.0
    out = []
    portfolio_returns_history = []

    for d in returns.index:
        # Compute today's daily portfolio return given current weights
        risk_r = sum(weights[a] * pos_returns[a].loc[d] for a in available_signals)
        r = risk_r + cash_weight * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        portfolio_returns_history.append(r)

        # DD-stop: drawdown beyond threshold → force defensive
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and (1 - cash_weight) > 0:
            weights = {a: 0.0 for a in available_signals}
            cash_weight = 1.0
            peak_nav = nav  # reset peak

        # Drift weights
        for a in available_signals:
            weights[a] *= (1 + pos_returns[a].loc[d])
        cash_weight *= (1 + cash_ret.loc[d])
        s = sum(weights.values()) + cash_weight
        if s > 0:
            for k in weights:
                weights[k] /= s
            cash_weight /= s

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            # Compute momentum scores on the 1× underlying (cleaner signal)
            scores = {}
            for a in available_signals:
                tr = _trailing_return(prices[a], d, lookback)
                if tr is not None:
                    scores[a] = tr
            if not scores:
                continue
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            picks = [a for a, s in ranked[:top_n] if s > 0]
            if not picks:
                weights = {a: 0.0 for a in available_signals}
                cash_weight = 1.0
                continue
            # Inverse-vol weights on the LEVERAGED position vols (not underlying)
            invvols = {}
            for a in picks:
                v = _trailing_vol(pos_returns[a], d, vol_window)
                if v is not None:
                    invvols[a] = 1.0 / v
            if not invvols:
                w_each = 1.0 / len(picks)
                raw_weights = {a: (w_each if a in picks else 0.0) for a in available_signals}
            else:
                total_iv = sum(invvols.values())
                raw_weights = {a: 0.0 for a in available_signals}
                for a, iv in invvols.items():
                    raw_weights[a] = iv / total_iv

            # Portfolio-level vol target — scale entire risk-on basket
            if len(portfolio_returns_history) >= vol_window:
                # Simulate the new basket's vol via weighted historical vol
                # Approximation: use weighted-sum-of-asset-vols (ignores cross-correlation)
                # Conservative — usually overestimates portfolio vol so under-leverages slightly.
                est_vol = 0.0
                for a, w in raw_weights.items():
                    if w > 0:
                        v = _trailing_vol(pos_returns[a], d, vol_window) or target_vol
                        est_vol += w * v  # not portfolio vol but a usable proxy
                if est_vol > 0:
                    scale = min(1.0, target_vol / est_vol)
                else:
                    scale = 1.0
            else:
                scale = 1.0

            weights = {a: w * scale for a, w in raw_weights.items()}
            cash_weight = 1.0 - sum(weights.values())
            peak_nav = nav
    return pd.Series(out, index=returns.index)


# Cross-Asset DM with 2× leverage + DD-stop + vol-target
CROSS_ASSET_LEV_MAP = {
    "SPY": "SPUU",
    "TLT": "UBT",
    "GLD": "UGL",
    "DBC": "DBC",  # no clean 2× — kept 1×
}


def bt_cross_asset_dual_momentum_levered(
    returns: pd.DataFrame, prices: pd.DataFrame,
    lookback: int = 252,
    dd_threshold: float = 0.30, target_vol: float = 0.25,
    vol_window: int = 60,
) -> pd.Series:
    """
    Cross-Asset Dual Momentum (Antonacci GEM extension) + 2× leverage +
    DD-stop + vol-target.

    Universe (signal → position):
      SPY → SPUU (2×)
      TLT → UBT (2×)
      GLD → UGL (2×)
      DBC → DBC (1× — no clean 2× ETF)

    Mechanics: same monthly selection as bt_cross_asset_dual_momentum,
    but holds the leveraged position. Vol-target scales total exposure
    by min(1, 25% / 60d-realized-vol-of-winner). DD-stop forces cash on
    30% trailing-peak drawdown.
    """
    signal_assets = ["SPY", "TLT", "GLD", "DBC"]
    cash = "BIL"
    available = [a for a in signal_assets if a in prices.columns]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)
    pos_returns = {a: _lev_etf_return(returns, CROSS_ASSET_LEV_MAP.get(a, a)) for a in available}

    current = "__cash__"
    scale = 0.0
    nav = 1.0
    peak_nav = 1.0
    out = []
    for d in returns.index:
        if current == "__cash__":
            r = cash_ret.loc[d]
        else:
            r = scale * pos_returns[current].loc[d] + (1 - scale) * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and current != "__cash__":
            current = "__cash__"
            scale = 0.0
            peak_nav = nav

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {}
            for a in available:
                tr = _trailing_return(prices[a], d, lookback)
                if tr is not None:
                    scores[a] = tr
            if scores:
                best, best_score = max(scores.items(), key=lambda kv: kv[1])
                if best_score > 0:
                    if current != best:
                        current = best
                        peak_nav = nav
                    # Set vol-target scale for the new winner
                    v = _trailing_vol(pos_returns[best], d, vol_window) or target_vol
                    scale = min(1.0, target_vol / v) if v > 0 else 1.0
                else:
                    current = "__cash__"
                    scale = 0.0
            else:
                current = "__cash__"
                scale = 0.0
    return pd.Series(out, index=returns.index)


def bt_aaa_3x_us_only_levered(
    returns: pd.DataFrame, prices: pd.DataFrame,
    top_n: int = 3, lookback: int = 126,
    dd_threshold: float = 0.30, target_vol: float = 0.25,
    vol_window: int = 60,
) -> pd.Series:
    """
    AAA variant: 3× UPRO for US equity (highest-conviction sleeve), 2× for
    others. Same DD-stop + vol-target framework. Tests whether asymmetric
    leverage produces better CAGR/Sharpe than uniform 2×.
    """
    # Reuse the levered AAA logic but swap SPY → UPRO (3×)
    asymmetric_map = {
        "SPY": "UPRO",   # 3×
        "EFA": "EFO",    # 2×
        "EEM": "EET",    # 2×
        "TLT": "UBT",    # 2×
        "IEF": "UST",    # 2×
        "GLD": "UGL",    # 2×
        "DBC": "DBC",    # 1×
    }
    signal_assets = ["SPY", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC"]
    cash = "BIL"
    available_signals = [a for a in signal_assets if a in prices.columns]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)
    pos_returns = {a: _lev_etf_return(returns, asymmetric_map.get(a, a)) for a in available_signals}

    weights = {a: 0.0 for a in available_signals}
    cash_weight = 1.0
    peak_nav = 1.0
    nav = 1.0
    out = []

    for d in returns.index:
        risk_r = sum(weights[a] * pos_returns[a].loc[d] for a in available_signals)
        r = risk_r + cash_weight * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and (1 - cash_weight) > 0:
            weights = {a: 0.0 for a in available_signals}
            cash_weight = 1.0
            peak_nav = nav

        for a in available_signals:
            weights[a] *= (1 + pos_returns[a].loc[d])
        cash_weight *= (1 + cash_ret.loc[d])
        s = sum(weights.values()) + cash_weight
        if s > 0:
            for k in weights:
                weights[k] /= s
            cash_weight /= s

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {}
            for a in available_signals:
                tr = _trailing_return(prices[a], d, lookback)
                if tr is not None:
                    scores[a] = tr
            if not scores:
                continue
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            picks = [a for a, s in ranked[:top_n] if s > 0]
            if not picks:
                weights = {a: 0.0 for a in available_signals}
                cash_weight = 1.0
                continue
            invvols = {}
            for a in picks:
                v = _trailing_vol(pos_returns[a], d, vol_window)
                if v is not None:
                    invvols[a] = 1.0 / v
            if not invvols:
                w_each = 1.0 / len(picks)
                raw_weights = {a: (w_each if a in picks else 0.0) for a in available_signals}
            else:
                total_iv = sum(invvols.values())
                raw_weights = {a: 0.0 for a in available_signals}
                for a, iv in invvols.items():
                    raw_weights[a] = iv / total_iv

            est_vol = 0.0
            for a, w in raw_weights.items():
                if w > 0:
                    v = _trailing_vol(pos_returns[a], d, vol_window) or target_vol
                    est_vol += w * v
            scale = min(1.0, target_vol / est_vol) if est_vol > 0 else 1.0

            weights = {a: w * scale for a, w in raw_weights.items()}
            cash_weight = 1.0 - sum(weights.values())
            peak_nav = nav
    return pd.Series(out, index=returns.index)


# ════════════════════════════════════════════════════════════════════════
# UNIQUE-TICKER LEVERAGED STRATEGIES — no overlap with production sleeves.
#
# Constraint: cannot use UPRO/TMF/KMLM/SPXL/SGOV/RSSB/WTIP/BIL/TQQQ/AGG/
# SPUU/QLD/EFO/BND/SSO/USFR/WLDU (all in production). Forces creative
# universes using SAA, EET, UBT, UST, UGL, EDC, TYD, TNA, DBC, SHV.
#
# Goal: lift portfolio CAGR while maintaining competitive Sharpe.
# Framework: same DD-stop + vol-target as DM best-of-3.
# ════════════════════════════════════════════════════════════════════════


# Mapping for the "free 2×" AAA — covers 6 asset classes with non-overlapping ETFs.
# Drops US large-cap and developed-international (no free 2× ETFs available; the
# portfolio already has 5 sleeves of US-large-cap leverage anyway, so this is
# a feature: AAA Free 2× is a pure diversifier into bonds/EM/gold/commodities).
AAA_FREE_2X_MAP = {
    "IWM": "SAA",    # 2× Russell 2000 small-cap
    "EEM": "EET",    # 2× emerging markets
    "TLT": "UBT",    # 2× 20+yr Treasuries
    "IEF": "UST",    # 2× 7-10yr Treasuries
    "GLD": "UGL",    # 2× gold
    "DBC": "DBC",    # 1× commodities (no clean 2× available)
}


def bt_aaa_free_2x(returns: pd.DataFrame, prices: pd.DataFrame,
                    top_n: int = 3, lookback: int = 126,
                    dd_threshold: float = 0.30, target_vol: float = 0.25,
                    vol_window: int = 60) -> pd.Series:
    """
    Adaptive Asset Allocation (Butler-Philbrick-Gordillo 2012) with 2× leverage
    using ONLY tickers free of production conflicts.

    Universe (signal → 2× held position):
      IWM → SAA  (US small-cap)
      EEM → EET  (emerging markets)
      TLT → UBT  (long Treasuries)
      IEF → UST  (intermediate Treasuries)
      GLD → UGL  (gold)
      DBC → DBC  (commodities; no clean 2× ETF exists)

    Defensive: SHV (short Treasuries — Sector Momentum's old defensive, now free).

    Selection: top-3 by 6-month momentum of underlyings, inverse-vol weights
    on the leveraged positions, portfolio-vol target 25%, DD30 stop.

    Theory: combines cross-sectional momentum (Jegadeesh-Titman 1993) with
    risk-parity weighting (Maillard et al. 2010) — same framework as the
    original AAA but with a deliberately non-overlapping universe that
    captures the asset classes the existing portfolio under-allocates to.
    """
    signal_assets = list(AAA_FREE_2X_MAP.keys())
    cash = "SHV"
    available = [a for a in signal_assets if a in prices.columns]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)
    pos_returns = {a: _lev_etf_return(returns, AAA_FREE_2X_MAP[a]) for a in available}

    weights = {a: 0.0 for a in available}
    cash_weight = 1.0
    peak_nav = 1.0
    nav = 1.0
    out = []
    for d in returns.index:
        risk_r = sum(weights[a] * pos_returns[a].loc[d] for a in available)
        r = risk_r + cash_weight * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and (1 - cash_weight) > 0:
            weights = {a: 0.0 for a in available}
            cash_weight = 1.0
            peak_nav = nav
        for a in available:
            weights[a] *= (1 + pos_returns[a].loc[d])
        cash_weight *= (1 + cash_ret.loc[d])
        s = sum(weights.values()) + cash_weight
        if s > 0:
            for k in weights:
                weights[k] /= s
            cash_weight /= s

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {a: _trailing_return(prices[a], d, lookback) for a in available}
            scores = {k: v for k, v in scores.items() if v is not None}
            if not scores:
                continue
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            picks = [a for a, s in ranked[:top_n] if s > 0]
            if not picks:
                weights = {a: 0.0 for a in available}
                cash_weight = 1.0
                continue
            invvols = {}
            for a in picks:
                v = _trailing_vol(pos_returns[a], d, vol_window)
                if v is not None:
                    invvols[a] = 1.0 / v
            if not invvols:
                w_each = 1.0 / len(picks)
                raw = {a: (w_each if a in picks else 0.0) for a in available}
            else:
                total_iv = sum(invvols.values())
                raw = {a: 0.0 for a in available}
                for a, iv in invvols.items():
                    raw[a] = iv / total_iv
            est_vol = sum(w * (_trailing_vol(pos_returns[a], d, vol_window) or target_vol)
                          for a, w in raw.items() if w > 0)
            scale = min(1.0, target_vol / est_vol) if est_vol > 0 else 1.0
            weights = {a: w * scale for a, w in raw.items()}
            cash_weight = 1.0 - sum(weights.values())
            peak_nav = nav
    return pd.Series(out, index=returns.index)


# Aggressive 3× variant — uses TYD (3× IEF), TNA (3× IWM), EDC (3× EEM)
# where available, falls back to 2× for assets without free 3× ETFs (gold,
# commodities). Higher target vol (30%) to let the 3× leverage show through.
AAA_FREE_3X_MAP = {
    "IWM": "TNA",    # 3× Russell 2000
    "EEM": "EDC",    # 3× emerging markets
    "TLT": "UBT",    # 2× 20+yr Treasuries (no free 3× TLT — TMF taken)
    "IEF": "TYD",    # 3× 7-10yr Treasuries
    "GLD": "UGL",    # 2× gold (no free 3× — NUGT is miners not gold)
    "DBC": "DBC",    # 1× commodities
}


def bt_aaa_free_3x_aggressive(returns: pd.DataFrame, prices: pd.DataFrame,
                                top_n: int = 3, lookback: int = 126,
                                dd_threshold: float = 0.30, target_vol: float = 0.30,
                                vol_window: int = 60) -> pd.Series:
    """
    Same selection mechanics as bt_aaa_free_2x but with 3× ETFs where free
    (TNA / EDC / TYD) and 2× fallback for assets lacking free 3× ETFs.

    Vol target raised to 30% so the higher leverage can compound — at 25%
    the vol-target would scale 3× exposure back to ~1.5× effective.

    Hypothesis: higher leverage on the strongest momentum slots (EM, bonds,
    small-cap) lifts CAGR enough to compensate for the wider DD profile.
    """
    signal_assets = list(AAA_FREE_3X_MAP.keys())
    cash = "SHV"
    available = [a for a in signal_assets if a in prices.columns]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)
    pos_returns = {a: _lev_etf_return(returns, AAA_FREE_3X_MAP[a]) for a in available}

    weights = {a: 0.0 for a in available}
    cash_weight = 1.0
    peak_nav = 1.0
    nav = 1.0
    out = []
    for d in returns.index:
        risk_r = sum(weights[a] * pos_returns[a].loc[d] for a in available)
        r = risk_r + cash_weight * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and (1 - cash_weight) > 0:
            weights = {a: 0.0 for a in available}
            cash_weight = 1.0
            peak_nav = nav
        for a in available:
            weights[a] *= (1 + pos_returns[a].loc[d])
        cash_weight *= (1 + cash_ret.loc[d])
        s = sum(weights.values()) + cash_weight
        if s > 0:
            for k in weights:
                weights[k] /= s
            cash_weight /= s

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {a: _trailing_return(prices[a], d, lookback) for a in available}
            scores = {k: v for k, v in scores.items() if v is not None}
            if not scores:
                continue
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            picks = [a for a, s in ranked[:top_n] if s > 0]
            if not picks:
                weights = {a: 0.0 for a in available}
                cash_weight = 1.0
                continue
            invvols = {}
            for a in picks:
                v = _trailing_vol(pos_returns[a], d, vol_window)
                if v is not None:
                    invvols[a] = 1.0 / v
            if not invvols:
                w_each = 1.0 / len(picks)
                raw = {a: (w_each if a in picks else 0.0) for a in available}
            else:
                total_iv = sum(invvols.values())
                raw = {a: 0.0 for a in available}
                for a, iv in invvols.items():
                    raw[a] = iv / total_iv
            est_vol = sum(w * (_trailing_vol(pos_returns[a], d, vol_window) or target_vol)
                          for a, w in raw.items() if w > 0)
            scale = min(1.0, target_vol / est_vol) if est_vol > 0 else 1.0
            weights = {a: w * scale for a, w in raw.items()}
            cash_weight = 1.0 - sum(weights.values())
            peak_nav = nav
    return pd.Series(out, index=returns.index)


# Cross-Asset DM with unique tickers — no SPY/QQQ/EFA overlap.
# Universe: small-cap, bonds, gold, commodities (4 asset classes).
CROSS_ASSET_FREE_2X_MAP = {
    "IWM": "SAA",
    "TLT": "UBT",
    "GLD": "UGL",
    "DBC": "DBC",
}


# ════════════════════════════════════════════════════════════════════════
# NTSD STRATEGY FAMILY — exhaustive exploration of NTSD-based portfolios.
# Each variant grounded in a specific academic anchor; comparison driven
# by CAGR + Sharpe.
# ════════════════════════════════════════════════════════════════════════


def bt_ntsd_buyhold(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """
    NTSD pure buy-and-hold. Baseline.

    Theory: WisdomTree's intended use case — NTSD as a core 90/60 holding
    that delivers 1.5× equity exposure without daily-reset decay.
    Use this as the comparison floor for all other NTSD strategies.
    """
    return synthetic_ntsd_returns(returns)


def bt_ntsd_sma200_trend(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """
    NTSD with 200-day SMA trend filter on the synthetic NTSD price (Faber 2007).

    When NTSD synthetic > 200-SMA: hold 100% NTSD.
    When below: hold SHV (cash).

    Faber's classic GTAA showed that a simple monthly 10-month SMA timing
    rule on any risk asset materially improves Sharpe by avoiding the worst
    bear-market drawdowns. Adapted here as 200-day SMA on daily data.
    """
    ntsd_ret = synthetic_ntsd_returns(returns)
    ntsd_px = synthetic_ntsd_prices(returns)
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    sma200 = ntsd_px.rolling(200).mean()
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))

    in_position = False
    out = []
    for d in returns.index:
        r = ntsd_ret.loc[d] if in_position else shv.loc[d]
        out.append(r)
        if d in rebal_dates:
            p = ntsd_px.loc[d]
            sma = sma200.loc[d]
            if not pd.isna(sma):
                in_position = p > sma
    return pd.Series(out, index=returns.index)


def bt_ntsd_voltarget(returns: pd.DataFrame, prices: pd.DataFrame,
                       target_vol: float = 0.18, vol_window: int = 60) -> pd.Series:
    """
    NTSD with pure vol-target — no trend filter, no DD-stop.

    Theory: Hocquard-Ng-Papageorgiou (2013) "Volatility-Targeted Strategy in
    Equity Markets." Vol-targeting alone — scale exposure inversely with
    realized vol — improves Sharpe significantly. Target 18% (slightly above
    SPY's long-run vol of ~16% to allow NTSD's 1.5× leverage to compound).

    No DD-stop because NTSD is capital-efficient (no daily reset) — leverage
    decay isn't an issue, so the DD-stop's main benefit is moot here.
    """
    ntsd_ret = synthetic_ntsd_returns(returns)
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    scale = 1.0
    out = []
    for d in returns.index:
        r = scale * ntsd_ret.loc[d] + (1 - scale) * shv.loc[d]
        out.append(r)
        if d in rebal_dates:
            v = _trailing_vol(ntsd_ret, d, vol_window)
            if v is not None and v > 0:
                scale = min(1.0, target_vol / v)
    return pd.Series(out, index=returns.index)


def bt_ntsd_dd25_vol18(returns: pd.DataFrame, prices: pd.DataFrame,
                        dd_threshold: float = 0.25, target_vol: float = 0.18,
                        vol_window: int = 60) -> pd.Series:
    """
    NTSD with combined DD-stop + vol-target. Tighter DD threshold (25% not 30%)
    because NTSD's natural vol (~22%) is lower than 3× ETFs.

    Theory: combines drawdown control (Carver 2015, Systematic Trading) with
    vol-targeting (Hocquard 2013). Belt and suspenders.
    """
    ntsd_ret = synthetic_ntsd_returns(returns)
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    scale = 1.0
    nav = 1.0
    peak_nav = 1.0
    out = []
    for d in returns.index:
        r = scale * ntsd_ret.loc[d] + (1 - scale) * shv.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and scale > 0:
            scale = 0.0
            peak_nav = nav
        if d in rebal_dates:
            v = _trailing_vol(ntsd_ret, d, vol_window)
            if v is not None and v > 0:
                scale = min(1.0, target_vol / v)
    return pd.Series(out, index=returns.index)


def bt_ntsd_ubt_barbell(returns: pd.DataFrame, prices: pd.DataFrame,
                         ntsd_weight: float = 0.60) -> pd.Series:
    """
    Capital-efficient leveraged 60/40 portfolio: NTSD (1.5× equity) + UBT (2× bonds).

    Theory: Modern Portfolio Theory leveraged extension. Asness's "Leveraged
    60/40" argument (2010) — borrow at risk-free rate, hold 60/40 at 1.5×.
    NTSD + UBT achieves this without explicit margin: each $1 holds
      0.60 × NTSD = 0.54 SPY + 0.36 EFA
      0.40 × UBT  = 0.80 TLT
    Total: 0.90 equity + 0.80 bonds = 170% notional exposure, no margin required.

    Monthly rebalance to target weights (60% NTSD / 40% UBT default).
    """
    ntsd_ret = synthetic_ntsd_returns(returns)
    ubt_ret = _lev_etf_return(returns, "UBT")
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))

    w_ntsd = ntsd_weight
    w_ubt = 1.0 - ntsd_weight
    out = []
    for d in returns.index:
        r = w_ntsd * ntsd_ret.loc[d] + w_ubt * ubt_ret.loc[d]
        out.append(r)
        # Drift then optionally rebalance
        w_ntsd *= (1 + ntsd_ret.loc[d])
        w_ubt *= (1 + ubt_ret.loc[d])
        s = w_ntsd + w_ubt
        if s > 0:
            w_ntsd /= s; w_ubt /= s
        if d in rebal_dates:
            w_ntsd = ntsd_weight
            w_ubt = 1.0 - ntsd_weight
    return pd.Series(out, index=returns.index)


def bt_ntsd_risk_parity(returns: pd.DataFrame, prices: pd.DataFrame,
                          assets: list = None, vol_window: int = 60) -> pd.Series:
    """
    All-Weather-style risk parity: NTSD + UBT + UGL + DBC, inverse-vol weighted.

    Theory: Maillard-Roncalli-Teiletche (2010) "Equally Weighted Risk Contribution."
    Bridgewater's All Weather framework (Dalio 2005) — diversify across the four
    macro regimes (growth+/-, inflation+/-). NTSD provides growth-up equity
    exposure; UBT provides growth-down (deflation hedge); UGL/DBC provide
    inflation hedges.

    Each asset weighted by 1/vol_60d. NTSD's lower vol means it gets larger
    weight, but UBT/UGL/DBC contribute the diversification.
    """
    if assets is None:
        assets = ["NTSD", "UBT", "UGL", "DBC"]
    ret_series = {
        "NTSD": synthetic_ntsd_returns(returns),
        "UBT": _lev_etf_return(returns, "UBT"),
        "UGL": _lev_etf_return(returns, "UGL"),
        "DBC": _lev_etf_return(returns, "DBC"),
    }
    available = [a for a in assets if a in ret_series]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))

    weights = {a: 1.0 / len(available) for a in available}
    out = []
    for d in returns.index:
        r = sum(weights[a] * ret_series[a].loc[d] for a in available)
        out.append(r)
        for a in available:
            weights[a] *= (1 + ret_series[a].loc[d])
        s = sum(weights.values())
        if s > 0:
            weights = {a: w / s for a, w in weights.items()}
        if d in rebal_dates:
            invvols = {}
            for a in available:
                v = _trailing_vol(ret_series[a], d, vol_window)
                if v is not None:
                    invvols[a] = 1.0 / v
            if not invvols:
                continue
            total = sum(invvols.values())
            weights = {a: invvols.get(a, 0.0) / total for a in available}
    return pd.Series(out, index=returns.index)


def bt_ntsd_cross_asset_dm(returns: pd.DataFrame, prices: pd.DataFrame,
                            lookback: int = 252, dd_threshold: float = 0.25,
                            target_vol: float = 0.20, vol_window: int = 60) -> pd.Series:
    """
    Cross-Asset Dual Momentum with NTSD as equity slot.

    Universe: NTSD (equity), UBT (long bonds), UGL (gold). Pick single best by
    12m total return. Absolute momentum gate: winner must be positive or hold
    SHV cash. DD-stop 25%, vol-target 20%.

    Theory: Antonacci (2014) GEM applied to capital-efficient ETFs. The NTSD
    slot captures both US and EAFE leadership in a single position.
    """
    cash = "SHV"
    ret_series = {
        "NTSD": synthetic_ntsd_returns(returns),
        "UBT": _lev_etf_return(returns, "UBT"),
        "UGL": _lev_etf_return(returns, "UGL"),
    }
    px_series = {
        "NTSD": synthetic_ntsd_prices(returns),
        "UBT": (1 + ret_series["UBT"]).cumprod() * 100.0,
        "UGL": (1 + ret_series["UGL"]).cumprod() * 100.0,
    }
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)

    current = "__cash__"
    scale = 0.0
    nav = 1.0
    peak_nav = 1.0
    out = []
    for d in returns.index:
        if current == "__cash__":
            r = cash_ret.loc[d]
        else:
            r = scale * ret_series[current].loc[d] + (1 - scale) * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and current != "__cash__":
            current = "__cash__"; scale = 0.0; peak_nav = nav

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {a: _trailing_return(px_series[a], d, lookback) for a in ret_series.keys()}
            scores = {k: v for k, v in scores.items() if v is not None}
            if scores:
                best, best_score = max(scores.items(), key=lambda kv: kv[1])
                if best_score > 0:
                    if current != best:
                        current = best; peak_nav = nav
                    v = _trailing_vol(ret_series[best], d, vol_window) or target_vol
                    scale = min(1.0, target_vol / v) if v > 0 else 1.0
                else:
                    current = "__cash__"; scale = 0.0
            else:
                current = "__cash__"; scale = 0.0
    return pd.Series(out, index=returns.index)


def bt_ntsd_ubt_ugl_dbc_3x_overlay(returns: pd.DataFrame, prices: pd.DataFrame,
                                    top_n: int = 2, lookback: int = 126,
                                    dd_threshold: float = 0.25, target_vol: float = 0.20,
                                    vol_window: int = 60) -> pd.Series:
    """
    Multi-asset rotation with NTSD core: top-2 by 6m momentum from
    {NTSD, UBT, UGL, DBC}, inverse-vol weighted, DD25 + vol20.

    Theory: Adaptive Asset Allocation (Butler-Philbrick-Gordillo 2012)
    applied to a smaller 4-asset universe centered on NTSD. Top-2 not
    top-3 because the universe is small.
    """
    ret_series = {
        "NTSD": synthetic_ntsd_returns(returns),
        "UBT":  _lev_etf_return(returns, "UBT"),
        "UGL":  _lev_etf_return(returns, "UGL"),
        "DBC":  _lev_etf_return(returns, "DBC"),
    }
    px_series = {
        "NTSD": synthetic_ntsd_prices(returns),
        "UBT":  (1 + ret_series["UBT"]).cumprod() * 100.0,
        "UGL":  (1 + ret_series["UGL"]).cumprod() * 100.0,
        "DBC":  (1 + ret_series["DBC"]).cumprod() * 100.0,
    }
    cash = "SHV"
    available = list(ret_series.keys())
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)

    weights = {a: 0.0 for a in available}
    cash_weight = 1.0
    peak_nav = 1.0
    nav = 1.0
    out = []
    for d in returns.index:
        risk_r = sum(weights[a] * ret_series[a].loc[d] for a in available)
        r = risk_r + cash_weight * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and (1 - cash_weight) > 0:
            weights = {a: 0.0 for a in available}; cash_weight = 1.0; peak_nav = nav
        for a in available:
            weights[a] *= (1 + ret_series[a].loc[d])
        cash_weight *= (1 + cash_ret.loc[d])
        s = sum(weights.values()) + cash_weight
        if s > 0:
            for k in weights:
                weights[k] /= s
            cash_weight /= s

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {a: _trailing_return(px_series[a], d, lookback) for a in available}
            scores = {k: v for k, v in scores.items() if v is not None}
            if not scores:
                continue
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            picks = [a for a, s in ranked[:top_n] if s > 0]
            if not picks:
                weights = {a: 0.0 for a in available}; cash_weight = 1.0
                continue
            invvols = {}
            for a in picks:
                v = _trailing_vol(ret_series[a], d, vol_window)
                if v is not None:
                    invvols[a] = 1.0 / v
            if not invvols:
                w_each = 1.0 / len(picks)
                raw = {a: (w_each if a in picks else 0.0) for a in available}
            else:
                total_iv = sum(invvols.values())
                raw = {a: 0.0 for a in available}
                for a, iv in invvols.items():
                    raw[a] = iv / total_iv
            est_vol = sum(w * (_trailing_vol(ret_series[a], d, vol_window) or target_vol)
                          for a, w in raw.items() if w > 0)
            scale = min(1.0, target_vol / est_vol) if est_vol > 0 else 1.0
            weights = {a: w * scale for a, w in raw.items()}
            cash_weight = 1.0 - sum(weights.values())
            peak_nav = nav
    return pd.Series(out, index=returns.index)


# ════════════════════════════════════════════════════════════════════════
# NTSD Generation 2 — push CAGR + Sharpe beyond current gold.
# Six architecture variants, all ≤2× leverage, no production conflicts.
# ════════════════════════════════════════════════════════════════════════


def _blended_trailing_return(prices_series, end_date,
                             lookbacks=(21, 63, 126, 252),
                             weights=(0.10, 0.30, 0.30, 0.30)) -> Optional[float]:
    """Multi-horizon momentum (Asness-Moskowitz-Pedersen 2013).
    Default blend: 1m=10%, 3m=30%, 6m=30%, 12m=30%. None if any lookback fails."""
    score = 0.0
    for lb, w in zip(lookbacks, weights):
        r = _trailing_return(prices_series, end_date, lb)
        if r is None:
            return None
        score += w * r
    return score


def bt_ntsd_top1_concentrated(returns, prices,
                                lookback=126, dd_threshold=0.25, target_vol=0.22,
                                vol_window=60):
    """
    Top-1 concentrated rotation among NTSD/UBT/UGL/DBC.

    Theory: Antonacci 2014 — momentum's premium is concentrated in the top
    candidate, top-2/3 dilutes alpha. Trade-off: higher CAGR potential but
    more single-asset risk. DD25 + vol22 to control downside.
    """
    cash = "SHV"
    ret_series = {
        "NTSD": synthetic_ntsd_returns(returns),
        "UBT": _lev_etf_return(returns, "UBT"),
        "UGL": _lev_etf_return(returns, "UGL"),
        "DBC": _lev_etf_return(returns, "DBC"),
    }
    px_series = {
        "NTSD": synthetic_ntsd_prices(returns),
        "UBT": (1 + ret_series["UBT"]).cumprod() * 100.0,
        "UGL": (1 + ret_series["UGL"]).cumprod() * 100.0,
        "DBC": (1 + ret_series["DBC"]).cumprod() * 100.0,
    }
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)

    current = "__cash__"
    scale = 0.0
    nav = 1.0
    peak_nav = 1.0
    out = []
    for d in returns.index:
        if current == "__cash__":
            r = cash_ret.loc[d]
        else:
            r = scale * ret_series[current].loc[d] + (1 - scale) * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and current != "__cash__":
            current = "__cash__"; scale = 0.0; peak_nav = nav

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {a: _trailing_return(px_series[a], d, lookback) for a in ret_series.keys()}
            scores = {k: v for k, v in scores.items() if v is not None}
            if scores:
                best, best_score = max(scores.items(), key=lambda kv: kv[1])
                if best_score > 0:
                    if current != best:
                        current = best; peak_nav = nav
                    v = _trailing_vol(ret_series[best], d, vol_window) or target_vol
                    scale = min(1.0, target_vol / v) if v > 0 else 1.0
                else:
                    current = "__cash__"; scale = 0.0
            else:
                current = "__cash__"; scale = 0.0
    return pd.Series(out, index=returns.index)


def bt_ntsd_aaa_multi_horizon(returns, prices,
                               top_n=2, dd_threshold=0.25, target_vol=0.20,
                               vol_window=60):
    """
    NTSD/UBT/UGL/DBC top-2 with multi-horizon momentum signal (1m/3m/6m/12m blend).

    Theory: Asness-Moskowitz-Pedersen 2013 "Value and Momentum Everywhere" —
    multi-horizon momentum is more robust than single-horizon. Each lookback
    has different signal-noise properties; blending captures multiple regime
    transitions while reducing whipsaw.
    """
    cash = "SHV"
    ret_series = {
        "NTSD": synthetic_ntsd_returns(returns),
        "UBT": _lev_etf_return(returns, "UBT"),
        "UGL": _lev_etf_return(returns, "UGL"),
        "DBC": _lev_etf_return(returns, "DBC"),
    }
    px_series = {
        "NTSD": synthetic_ntsd_prices(returns),
        "UBT": (1 + ret_series["UBT"]).cumprod() * 100.0,
        "UGL": (1 + ret_series["UGL"]).cumprod() * 100.0,
        "DBC": (1 + ret_series["DBC"]).cumprod() * 100.0,
    }
    available = list(ret_series.keys())
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)

    weights = {a: 0.0 for a in available}
    cash_weight = 1.0
    peak_nav = 1.0
    nav = 1.0
    out = []
    for d in returns.index:
        risk_r = sum(weights[a] * ret_series[a].loc[d] for a in available)
        r = risk_r + cash_weight * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and (1 - cash_weight) > 0:
            weights = {a: 0.0 for a in available}; cash_weight = 1.0; peak_nav = nav
        for a in available:
            weights[a] *= (1 + ret_series[a].loc[d])
        cash_weight *= (1 + cash_ret.loc[d])
        s = sum(weights.values()) + cash_weight
        if s > 0:
            for k in weights:
                weights[k] /= s
            cash_weight /= s

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= 252:
            scores = {a: _blended_trailing_return(px_series[a], d) for a in available}
            scores = {k: v for k, v in scores.items() if v is not None}
            if not scores:
                continue
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            picks = [a for a, s in ranked[:top_n] if s > 0]
            if not picks:
                weights = {a: 0.0 for a in available}; cash_weight = 1.0
                continue
            invvols = {}
            for a in picks:
                v = _trailing_vol(ret_series[a], d, vol_window)
                if v is not None:
                    invvols[a] = 1.0 / v
            if not invvols:
                w_each = 1.0 / len(picks)
                raw = {a: (w_each if a in picks else 0.0) for a in available}
            else:
                total_iv = sum(invvols.values())
                raw = {a: 0.0 for a in available}
                for a, iv in invvols.items():
                    raw[a] = iv / total_iv
            est_vol = sum(w * (_trailing_vol(ret_series[a], d, vol_window) or target_vol)
                          for a, w in raw.items() if w > 0)
            scale = min(1.0, target_vol / est_vol) if est_vol > 0 else 1.0
            weights = {a: w * scale for a, w in raw.items()}
            cash_weight = 1.0 - sum(weights.values())
            peak_nav = nav
    return pd.Series(out, index=returns.index)


def bt_ntsd_core_satellite(returns, prices,
                            core_weight=0.60, lookback=126,
                            vol_window=60):
    """
    Core-satellite: 60% NTSD always + 40% best-of (UBT/UGL/DBC) by 6m momentum.
    No DD-stop on the core (NTSD is meant to be held), DD-stop on satellite.

    Theory: Sharpe-Lintner CAPM core-satellite — beta from core, alpha from
    satellite. NTSD captures equity risk premium efficiently; the rotation
    satellite captures regime alpha. Lower turnover than pure rotation.
    """
    cash = "SHV"
    ntsd_ret = synthetic_ntsd_returns(returns)
    satellites = ["UBT", "UGL", "DBC"]
    ret_series = {a: _lev_etf_return(returns, a) for a in satellites}
    px_series = {a: (1 + ret_series[a]).cumprod() * 100.0 for a in satellites}
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)
    sat_weight = 1.0 - core_weight

    current_sat = "__cash__"
    out = []
    for d in returns.index:
        core_r = core_weight * ntsd_ret.loc[d]
        if current_sat == "__cash__":
            sat_r = sat_weight * cash_ret.loc[d]
        else:
            sat_r = sat_weight * ret_series[current_sat].loc[d]
        out.append(core_r + sat_r)

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {a: _trailing_return(px_series[a], d, lookback) for a in satellites}
            scores = {k: v for k, v in scores.items() if v is not None}
            if scores:
                best, best_score = max(scores.items(), key=lambda kv: kv[1])
                current_sat = best if best_score > 0 else "__cash__"
            else:
                current_sat = "__cash__"
    return pd.Series(out, index=returns.index)


def bt_ntsd_aaa_wide_universe(returns, prices,
                               top_n=3, lookback=126, dd_threshold=0.30,
                               target_vol=0.25, vol_window=60):
    """
    Wider 8-slot rotation: NTSD + SAA + EET + UBT + UGL + DBC + SLV.

    Theory: Carhart 1997 four-factor — more asset classes = more uncorrelated
    alpha sources. Adds silver (SLV) as an inflation/regime alternative to
    gold; adds SAA + EET for small-cap and EM. All ≤2×.

    Trade-off: more rotation candidates may dilute concentrated picks if
    several assets trend together, but should help in narrow regimes.
    """
    cash = "SHV"
    slot_specs = [
        ("NTSD", synthetic_ntsd_prices(returns),                  synthetic_ntsd_returns(returns)),
        ("IWM",  prices["IWM"] if "IWM" in prices.columns else None, _lev_etf_return(returns, "SAA")),
        ("EEM",  prices["EEM"] if "EEM" in prices.columns else None, _lev_etf_return(returns, "EET")),
        ("TLT",  prices["TLT"] if "TLT" in prices.columns else None, _lev_etf_return(returns, "UBT")),
        ("GLD",  prices["GLD"] if "GLD" in prices.columns else None, _lev_etf_return(returns, "UGL")),
        ("DBC",  prices["DBC"] if "DBC" in prices.columns else None, _lev_etf_return(returns, "DBC")),
        ("SLV",  prices["SLV"] if "SLV" in prices.columns else None, returns["SLV"].fillna(0) if "SLV" in returns.columns else None),
    ]
    slot_specs = [(n, p, r) for n, p, r in slot_specs if p is not None and r is not None]
    signal_prices = {n: p for n, p, _ in slot_specs}
    pos_returns = {n: r for n, _, r in slot_specs}
    available = [n for n, _, _ in slot_specs]

    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)

    weights = {a: 0.0 for a in available}
    cash_weight = 1.0
    peak_nav = 1.0
    nav = 1.0
    out = []
    for d in returns.index:
        risk_r = sum(weights[a] * pos_returns[a].loc[d] for a in available)
        r = risk_r + cash_weight * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and (1 - cash_weight) > 0:
            weights = {a: 0.0 for a in available}; cash_weight = 1.0; peak_nav = nav
        for a in available:
            weights[a] *= (1 + pos_returns[a].loc[d])
        cash_weight *= (1 + cash_ret.loc[d])
        s = sum(weights.values()) + cash_weight
        if s > 0:
            for k in weights:
                weights[k] /= s
            cash_weight /= s

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {a: _trailing_return(signal_prices[a], d, lookback) for a in available}
            scores = {k: v for k, v in scores.items() if v is not None}
            if not scores:
                continue
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            picks = [a for a, s in ranked[:top_n] if s > 0]
            if not picks:
                weights = {a: 0.0 for a in available}; cash_weight = 1.0
                continue
            invvols = {}
            for a in picks:
                v = _trailing_vol(pos_returns[a], d, vol_window)
                if v is not None:
                    invvols[a] = 1.0 / v
            if not invvols:
                w_each = 1.0 / len(picks)
                raw = {a: (w_each if a in picks else 0.0) for a in available}
            else:
                total_iv = sum(invvols.values())
                raw = {a: 0.0 for a in available}
                for a, iv in invvols.items():
                    raw[a] = iv / total_iv
            est_vol = sum(w * (_trailing_vol(pos_returns[a], d, vol_window) or target_vol)
                          for a, w in raw.items() if w > 0)
            scale = min(1.0, target_vol / est_vol) if est_vol > 0 else 1.0
            weights = {a: w * scale for a, w in raw.items()}
            cash_weight = 1.0 - sum(weights.values())
            peak_nav = nav
    return pd.Series(out, index=returns.index)


def bt_ntsd_aaa_higher_vol(returns, prices):
    """Same as current gold (NTSD/UBT/UGL/DBC top-2 AAA) but vol target 30%
    (vs 20%). Tests whether higher target allows more leverage when conditions
    permit, lifting CAGR. Hocquard 2013."""
    return bt_ntsd_ubt_ugl_dbc_3x_overlay(returns, prices,
                                            top_n=2, lookback=126,
                                            dd_threshold=0.30,
                                            target_vol=0.30,
                                            vol_window=60)


def bt_aaa_free_2x_plus_ntsd_top2(returns, prices):
    """Same as AAA Free 2× + NTSD but top-2 selection (more concentrated).
    Hypothesis: a smaller pick set increases CAGR via winner concentration."""
    return bt_aaa_free_2x_plus_ntsd(returns, prices, top_n=2, lookback=126,
                                      dd_threshold=0.30, target_vol=0.25)


def bt_aaa_free_2x_plus_ntsd(returns: pd.DataFrame, prices: pd.DataFrame,
                              top_n: int = 3, lookback: int = 126,
                              dd_threshold: float = 0.30, target_vol: float = 0.25,
                              vol_window: int = 60, return_weights: bool = False):
    """
    AAA Free 2× with NTSD slot — strictly ≤2× leverage everywhere.

    Universe (signal → held position, all ≤2×):
      NTSD-synth → NTSD (1.5× US large + EAFE futures stack)
      IWM        → SAA  (2× S&P SmallCap 600)
      EEM        → EET  (2× emerging markets)
      TLT        → UBT  (2× long Treasuries)
      IEF        → UST  (2× 7-10y Treasuries)
      GLD        → UGL  (2× gold)
      DBC        → DBC  (1× commodities)

    Defensive: SHV. Same selection (top-3 by 6m momentum, inverse-vol weighted)
    and risk controls (DD30 + vol25) as the 3× variant, but vol target lowered
    to 25% to match the lower aggregate vol of a 2×-capped basket.
    """
    ntsd_returns = synthetic_ntsd_returns(returns)
    ntsd_prices = synthetic_ntsd_prices(returns)
    cash = "SHV"

    # Slot tuples: (signal_label, signal_prices, position_returns, position_ticker)
    # Note: signal_label is used internally as the dict key; position_ticker is
    # what's actually held (for tax purposes — TFS lookup is by position ticker).
    slot_specs = [
        ("NTSD", ntsd_prices, ntsd_returns, "NTSD"),
        ("IWM",  prices["IWM"] if "IWM" in prices.columns else None,  _lev_etf_return(returns, "SAA"), "SAA"),
        ("EEM",  prices["EEM"] if "EEM" in prices.columns else None,  _lev_etf_return(returns, "EET"), "EET"),
        ("TLT",  prices["TLT"] if "TLT" in prices.columns else None,  _lev_etf_return(returns, "UBT"), "UBT"),
        ("IEF",  prices["IEF"] if "IEF" in prices.columns else None,  _lev_etf_return(returns, "UST"), "UST"),
        ("GLD",  prices["GLD"] if "GLD" in prices.columns else None,  _lev_etf_return(returns, "UGL"), "UGL"),
        ("DBC",  prices["DBC"] if "DBC" in prices.columns else None,  _lev_etf_return(returns, "DBC"), "DBC"),
    ]
    slot_specs = [(n, p, r, pos) for n, p, r, pos in slot_specs if p is not None]
    signal_to_pos = {n: pos for n, _, _, pos in slot_specs}
    signal_prices = {n: p for n, p, _, _ in slot_specs}
    pos_returns = {n: r for n, _, r, _ in slot_specs}
    available = [n for n, _, _, _ in slot_specs]

    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)

    weights = {a: 0.0 for a in available}
    cash_weight = 1.0
    peak_nav = 1.0
    nav = 1.0
    out = []
    # Rebalance log: emits a row whenever target weights actually change.
    rebal_log: list[tuple[pd.Timestamp, dict]] = [
        (returns.index[0],
         {pos: 0.0 for pos in signal_to_pos.values()} | {cash: 1.0})
    ]
    prev_target = rebal_log[0][1].copy()
    for d in returns.index:
        risk_r = sum(weights[a] * pos_returns[a].loc[d] for a in available)
        r = risk_r + cash_weight * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and (1 - cash_weight) > 0:
            weights = {a: 0.0 for a in available}; cash_weight = 1.0; peak_nav = nav
            new_target = {pos: 0.0 for pos in signal_to_pos.values()} | {cash: 1.0}
            if new_target != prev_target:
                rebal_log.append((d, new_target))
                prev_target = new_target.copy()
        for a in available:
            weights[a] *= (1 + pos_returns[a].loc[d])
        cash_weight *= (1 + cash_ret.loc[d])
        s = sum(weights.values()) + cash_weight
        if s > 0:
            for k in weights:
                weights[k] /= s
            cash_weight /= s

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {a: _trailing_return(signal_prices[a], d, lookback) for a in available}
            scores = {k: v for k, v in scores.items() if v is not None}
            if not scores:
                continue
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            picks = [a for a, s in ranked[:top_n] if s > 0]
            if not picks:
                weights = {a: 0.0 for a in available}; cash_weight = 1.0
                new_target = {pos: 0.0 for pos in signal_to_pos.values()} | {cash: 1.0}
                if new_target != prev_target:
                    rebal_log.append((d, new_target))
                    prev_target = new_target.copy()
                continue
            invvols = {}
            for a in picks:
                v = _trailing_vol(pos_returns[a], d, vol_window)
                if v is not None:
                    invvols[a] = 1.0 / v
            if not invvols:
                w_each = 1.0 / len(picks)
                raw = {a: (w_each if a in picks else 0.0) for a in available}
            else:
                total_iv = sum(invvols.values())
                raw = {a: 0.0 for a in available}
                for a, iv in invvols.items():
                    raw[a] = iv / total_iv
            est_vol = sum(w * (_trailing_vol(pos_returns[a], d, vol_window) or target_vol)
                          for a, w in raw.items() if w > 0)
            scale = min(1.0, target_vol / est_vol) if est_vol > 0 else 1.0
            weights = {a: w * scale for a, w in raw.items()}
            cash_weight = 1.0 - sum(weights.values())
            peak_nav = nav
            # Remap signal-keyed weights to position-ticker-keyed weights for tax
            new_target = {pos: 0.0 for pos in signal_to_pos.values()}
            new_target[cash] = cash_weight
            for sig, w_val in weights.items():
                pos = signal_to_pos.get(sig, sig)
                new_target[pos] = new_target.get(pos, 0.0) + w_val
            if new_target != prev_target:
                rebal_log.append((d, new_target))
                prev_target = new_target.copy()

    ret = pd.Series(out, index=returns.index)
    if not return_weights:
        return ret

    asset_returns_dict = {pos: pos_returns[sig] for sig, pos in signal_to_pos.items()}
    asset_returns_dict[cash] = cash_ret
    asset_returns_df = pd.DataFrame(asset_returns_dict).fillna(0.0)
    weights_df = pd.DataFrame(
        [r for _, r in rebal_log],
        index=pd.DatetimeIndex([d for d, _ in rebal_log]),
    ).fillna(0.0)
    return ret, {"weights": weights_df, "asset_returns": asset_returns_df}


def bt_aaa_free_3x_plus_ntsd(returns: pd.DataFrame, prices: pd.DataFrame,
                              top_n: int = 3, lookback: int = 126,
                              dd_threshold: float = 0.30, target_vol: float = 0.30,
                              vol_window: int = 60) -> pd.Series:
    """
    AAA Free 3× Aggressive WITH NTSD as a 7th slot covering US large-cap +
    developed-international equity (the only two asset classes the Free
    variant had to drop because of ticker conflicts).

    Universe (signal → held position):
      NTSD-synth → NTSD (1.5× US large + EAFE futures stack, non-daily-reset)
      IWM        → TNA  (3× Russell 2000)
      EEM        → EDC  (3× emerging markets)
      TLT        → UBT  (2× long Treasuries)
      IEF        → TYD  (3× 7-10y Treasuries)
      GLD        → UGL  (2× gold)
      DBC        → DBC  (1× commodities)

    Defensive: SHV. Selection: top-3 by 6m momentum, inverse-vol weighted on
    the leveraged-position vols, portfolio vol target 30%, DD30 stop.

    NTSD signal uses the synthetic NTSD price series so the momentum check
    captures the actual blended US+EAFE behavior. Inverse-vol weighting will
    naturally give NTSD a larger share (its 1.5× vol is much lower than the
    3× sleeves), implementing risk-parity logic correctly.
    """
    # Build synthetic NTSD time series
    ntsd_returns = synthetic_ntsd_returns(returns)
    ntsd_prices = synthetic_ntsd_prices(returns)

    # Map: signal name → (signal price series, position return series)
    cash = "SHV"

    slot_specs = [
        ("NTSD", ntsd_prices, ntsd_returns),
        ("IWM",  prices["IWM"] if "IWM" in prices.columns else None,  _lev_etf_return(returns, "TNA")),
        ("EEM",  prices["EEM"] if "EEM" in prices.columns else None,  _lev_etf_return(returns, "EDC")),
        ("TLT",  prices["TLT"] if "TLT" in prices.columns else None,  _lev_etf_return(returns, "UBT")),
        ("IEF",  prices["IEF"] if "IEF" in prices.columns else None,  _lev_etf_return(returns, "TYD")),
        ("GLD",  prices["GLD"] if "GLD" in prices.columns else None,  _lev_etf_return(returns, "UGL")),
        ("DBC",  prices["DBC"] if "DBC" in prices.columns else None,  _lev_etf_return(returns, "DBC")),
    ]
    # Filter out missing
    slot_specs = [(n, p, r) for n, p, r in slot_specs if p is not None]
    signal_prices = {n: p for n, p, _ in slot_specs}
    pos_returns = {n: r for n, _, r in slot_specs}
    available = [n for n, _, _ in slot_specs]

    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)

    weights = {a: 0.0 for a in available}
    cash_weight = 1.0
    peak_nav = 1.0
    nav = 1.0
    out = []
    for d in returns.index:
        risk_r = sum(weights[a] * pos_returns[a].loc[d] for a in available)
        r = risk_r + cash_weight * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and (1 - cash_weight) > 0:
            weights = {a: 0.0 for a in available}
            cash_weight = 1.0
            peak_nav = nav
        for a in available:
            weights[a] *= (1 + pos_returns[a].loc[d])
        cash_weight *= (1 + cash_ret.loc[d])
        s = sum(weights.values()) + cash_weight
        if s > 0:
            for k in weights:
                weights[k] /= s
            cash_weight /= s

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {}
            for a in available:
                tr = _trailing_return(signal_prices[a], d, lookback)
                if tr is not None:
                    scores[a] = tr
            if not scores:
                continue
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            picks = [a for a, s in ranked[:top_n] if s > 0]
            if not picks:
                weights = {a: 0.0 for a in available}
                cash_weight = 1.0
                continue
            invvols = {}
            for a in picks:
                v = _trailing_vol(pos_returns[a], d, vol_window)
                if v is not None:
                    invvols[a] = 1.0 / v
            if not invvols:
                w_each = 1.0 / len(picks)
                raw = {a: (w_each if a in picks else 0.0) for a in available}
            else:
                total_iv = sum(invvols.values())
                raw = {a: 0.0 for a in available}
                for a, iv in invvols.items():
                    raw[a] = iv / total_iv
            est_vol = sum(w * (_trailing_vol(pos_returns[a], d, vol_window) or target_vol)
                          for a, w in raw.items() if w > 0)
            scale = min(1.0, target_vol / est_vol) if est_vol > 0 else 1.0
            weights = {a: w * scale for a, w in raw.items()}
            cash_weight = 1.0 - sum(weights.values())
            peak_nav = nav
    return pd.Series(out, index=returns.index)


def bt_cross_asset_dm_free_2x(returns: pd.DataFrame, prices: pd.DataFrame,
                                lookback: int = 252,
                                dd_threshold: float = 0.30, target_vol: float = 0.25,
                                vol_window: int = 60) -> pd.Series:
    """
    Cross-Asset Dual Momentum with non-overlapping tickers.

    Universe (signal → 2× position):
      IWM → SAA  (US small-cap)
      TLT → UBT  (long Treasuries)
      GLD → UGL  (gold)
      DBC → DBC  (commodities; 1×)

    Picks the single highest-momentum asset by 12-month return. Absolute
    momentum gate: must be > 0 or hold cash (SHV). DD30 + vol25 risk controls.

    Drops the US-large-cap slot entirely: the portfolio already has five
    sleeves with leveraged S&P 500 exposure, so this sleeve provides pure
    non-US-large-cap regime rotation.
    """
    signal_assets = list(CROSS_ASSET_FREE_2X_MAP.keys())
    cash = "SHV"
    available = [a for a in signal_assets if a in prices.columns]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)
    pos_returns = {a: _lev_etf_return(returns, CROSS_ASSET_FREE_2X_MAP[a]) for a in available}

    current = "__cash__"
    scale = 0.0
    nav = 1.0
    peak_nav = 1.0
    out = []
    for d in returns.index:
        if current == "__cash__":
            r = cash_ret.loc[d]
        else:
            r = scale * pos_returns[current].loc[d] + (1 - scale) * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and current != "__cash__":
            current = "__cash__"
            scale = 0.0
            peak_nav = nav

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {a: _trailing_return(prices[a], d, lookback) for a in available}
            scores = {k: v for k, v in scores.items() if v is not None}
            if scores:
                best, best_score = max(scores.items(), key=lambda kv: kv[1])
                if best_score > 0:
                    if current != best:
                        current = best
                        peak_nav = nav
                    v = _trailing_vol(pos_returns[best], d, vol_window) or target_vol
                    scale = min(1.0, target_vol / v) if v > 0 else 1.0
                else:
                    current = "__cash__"
                    scale = 0.0
            else:
                current = "__cash__"
                scale = 0.0
    return pd.Series(out, index=returns.index)


def bt_dm_dd20_conditional_1x2x_vol20(returns, prices):
    """
    Conditional 1×/2× leverage at vol-target 20%:
      • Score 1%-10%  → SPY (1× S&P)
      • Score > 10%   → SPUU (2× S&P)
      • EFA > 1%      → EFO (2× MSCI EAFE)
      • Else          → BND
    Vol-target 20%, DD-stop scaled: DD20 for 2× positions, DD15 for 1×.
    """
    spy = prices["SPY"]
    efa = prices["EFA"]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    spuu = spliced_leveraged_etf(returns, "SPUU")
    efo = spliced_leveraged_etf(returns, "EFO")
    spy_ret = returns["SPY"].fillna(0)
    bnd = returns["BND"].fillna(0) if "BND" in returns.columns else pd.Series(0.0, index=returns.index)

    target_vol = 0.20
    spuu_vol = spuu.rolling(60).std() * np.sqrt(252)
    efo_vol = efo.rolling(60).std() * np.sqrt(252)
    spy_vol = spy_ret.rolling(60).std() * np.sqrt(252)

    current = "DEF"
    nav = 1.0
    peak = 1.0
    out = []
    for d in returns.index:
        if current == "SPUU":
            v = spuu_vol.loc[d] if not pd.isna(spuu_vol.loc[d]) else target_vol
            scale = min(1.0, target_vol / v) if v > 0 else 1.0
            r = scale * spuu.loc[d] + (1 - scale) * bnd.loc[d]
            dd_t = 0.20
        elif current == "SPY":
            v = spy_vol.loc[d] if not pd.isna(spy_vol.loc[d]) else target_vol
            scale = min(1.0, target_vol / v) if v > 0 else 1.0
            r = scale * spy_ret.loc[d] + (1 - scale) * bnd.loc[d]
            dd_t = 0.15
        elif current == "EFO":
            v = efo_vol.loc[d] if not pd.isna(efo_vol.loc[d]) else target_vol
            scale = min(1.0, target_vol / v) if v > 0 else 1.0
            r = scale * efo.loc[d] + (1 - scale) * bnd.loc[d]
            dd_t = 0.20
        else:
            r = bnd.loc[d]
            dd_t = None
        out.append(r)
        nav *= (1 + r)
        if dd_t is not None:
            peak = max(peak, nav)
            dd = (nav - peak) / peak if peak > 0 else 0
            if dd < -dd_t:
                current = "DEF"
                peak = nav

        if d in rebal_dates and d - pd.Timedelta(days=400) >= returns.index[0]:
            try:
                sd = d - pd.Timedelta(days=21)
                spy_now = spy.asof(sd)
                efa_now = efa.asof(sd)
                if pd.isna(spy_now) or pd.isna(efa_now):
                    continue
                spy_score = 0
                efa_score = 0
                ok = True
                for days in (126, 252):
                    spy_past = spy.asof(d - pd.Timedelta(days=int(days * 1.45)))
                    efa_past = efa.asof(d - pd.Timedelta(days=int(days * 1.45)))
                    if pd.isna(spy_past) or pd.isna(efa_past) or spy_past <= 0 or efa_past <= 0:
                        ok = False
                        break
                    spy_score += 0.5 * (spy_now / spy_past - 1)
                    efa_score += 0.5 * (efa_now / efa_past - 1)
                if not ok:
                    continue
                if spy_score > 0.10:
                    new_pos = "SPUU"
                elif spy_score > 0.01:
                    new_pos = "SPY"
                elif efa_score > 0.01:
                    new_pos = "EFO"
                else:
                    new_pos = "DEF"
                if new_pos != current:
                    current = new_pos
                    peak = nav
            except Exception:
                pass
    return pd.Series(out, index=returns.index)


def bt_dm_1x_spy_vol_target(returns, prices, target_vol=0.15):
    """Pure 1× SPY + EFA + BND with vol-target — control test for unleveraged."""
    return _bt_dm_vol_target_custom(
        returns, prices, us_position="SPY", intl_position="EFA",
        dd_threshold=0.15, target_vol=target_vol,
    )


def bt_dm_dd30_upro_vol20(returns, prices):
    """Winner architecture with 20% vol target (tighter than the 25% sweet spot)."""
    return _bt_dm_vol_target_custom(returns, prices, us_position="UPRO", dd_threshold=0.30, target_vol=0.20)


def bt_dm_dd30_upro_vol30(returns, prices):
    """Winner architecture with 30% vol target (looser — captures more upside)."""
    return _bt_dm_vol_target_custom(returns, prices, us_position="UPRO", dd_threshold=0.30, target_vol=0.30)


def bt_dm_dd30_upro_vol_target(returns, prices, target_vol=0.25):
    """
    Winner + volatility targeting: scale UPRO position size to target
    `target_vol` annualized vol based on trailing 60-day realized vol.
    Excess goes to BND. When UPRO vol is moderate, full position. When
    UPRO vol is high (crisis), scale back; when low (calm bull), maintain.
    """
    spy = prices["SPY"]
    efa = prices["EFA"]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    upro = spliced_leveraged_etf(returns, "UPRO")
    efo = spliced_leveraged_etf(returns, "EFO")
    bnd = returns["BND"].fillna(0) if "BND" in returns.columns else pd.Series(0.0, index=returns.index)

    # Pre-compute trailing 60-day realized vols
    upro_vol60 = upro.rolling(60).std() * np.sqrt(252)
    efo_vol60 = efo.rolling(60).std() * np.sqrt(252)

    current = "DEF"
    nav = 1.0
    peak = 1.0
    out = []
    pos_weight = 1.0  # how much of the position to hold
    for d in returns.index:
        if current == "UPRO":
            v = upro_vol60.loc[d] if not pd.isna(upro_vol60.loc[d]) else target_vol
            scale = min(1.0, target_vol / v) if v > 0 else 1.0
            r = scale * upro.loc[d] + (1 - scale) * bnd.loc[d]
        elif current == "EFO":
            v = efo_vol60.loc[d] if not pd.isna(efo_vol60.loc[d]) else target_vol
            scale = min(1.0, target_vol / v) if v > 0 else 1.0
            r = scale * efo.loc[d] + (1 - scale) * bnd.loc[d]
        else:
            r = bnd.loc[d]
        out.append(r)
        nav *= (1 + r)
        if current in ("UPRO", "EFO"):
            peak = max(peak, nav)
            dd = (nav - peak) / peak if peak > 0 else 0
            if dd < -0.30:
                current = "DEF"
                peak = nav

        if d in rebal_dates and d - pd.Timedelta(days=400) >= returns.index[0]:
            try:
                sd = d - pd.Timedelta(days=21)
                spy_now = spy.asof(sd)
                efa_now = efa.asof(sd)
                if pd.isna(spy_now) or pd.isna(efa_now):
                    continue
                spy_score = 0
                efa_score = 0
                for days in (126, 252):
                    spy_past = spy.asof(d - pd.Timedelta(days=int(days * 1.45)))
                    efa_past = efa.asof(d - pd.Timedelta(days=int(days * 1.45)))
                    if pd.isna(spy_past) or pd.isna(efa_past) or spy_past <= 0 or efa_past <= 0:
                        continue
                    spy_score += 0.5 * (spy_now / spy_past - 1)
                    efa_score += 0.5 * (efa_now / efa_past - 1)
                new_pos = "UPRO" if spy_score > 0.01 else ("EFO" if efa_score > 0.01 else "DEF")
                if new_pos != current:
                    current = new_pos
                    peak = nav
            except Exception:
                pass
    return pd.Series(out, index=returns.index)


def bt_dual_momentum_abs_dd20_vix(returns, prices, vix_df, vix_high=24):
    """DD-stop 20% PLUS VIX>24 forces BND (more aggressive than vix_high=28)."""
    spy = prices["SPY"]
    efa = prices["EFA"]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    spuu = spliced_leveraged_etf(returns, "SPUU")
    efo = spliced_leveraged_etf(returns, "EFO")
    bnd = returns["BND"].fillna(0) if "BND" in returns.columns else pd.Series(0.0, index=returns.index)
    vix_aligned = (vix_df["value"].reindex(returns.index, method="ffill")
                   if vix_df is not None else pd.Series(20.0, index=returns.index))
    current = "BND"
    nav = 1.0
    peak = 1.0
    out = []
    for d in returns.index:
        if current == "SPUU":
            r = spuu.loc[d]
        elif current == "EFO":
            r = efo.loc[d]
        else:
            r = bnd.loc[d]
        out.append(r)
        nav *= (1 + r)
        if current in ("SPUU", "EFO"):
            peak = max(peak, nav)
            dd = (nav - peak) / peak if peak > 0 else 0
            if dd < -0.20:
                current = "BND"
                peak = nav
        if d in rebal_dates and d - pd.Timedelta(days=400) >= returns.index[0]:
            vix_today = vix_aligned.loc[d] if not pd.isna(vix_aligned.loc[d]) else 20.0
            if vix_today > vix_high:
                current = "BND"
                continue
            sig = _dm_step(d, spy, efa, {"6m": 126, "12m": 252},
                           {"6m": 0.5, "12m": 0.5}, 21, None,
                           signal_only_mode="absolute_only")
            if sig is not None:
                new_pos = "SPUU" if sig == "SPUU_or_1x_SPY" else (
                          "EFO" if sig == "EFO_or_1x_EFA" else "BND")
                if new_pos != current:
                    current = new_pos
                    peak = nav
    return pd.Series(out, index=returns.index)


def bt_dual_momentum_abs_vix_gate(returns, prices, vix_df, vix_high=28):
    """Absolute-only DM, with VIX>vix_high forcing BND for that month."""
    spy = prices["SPY"]
    efa = prices["EFA"]
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    spuu = spliced_leveraged_etf(returns, "SPUU")
    efo = spliced_leveraged_etf(returns, "EFO")
    bnd = returns["BND"].fillna(0) if "BND" in returns.columns else pd.Series(0.0, index=returns.index)
    vix_aligned = (vix_df["value"].reindex(returns.index, method="ffill")
                   if vix_df is not None else pd.Series(20.0, index=returns.index))

    current = "BND"
    out = []
    for d in returns.index:
        if current == "SPUU":
            r = spuu.loc[d]
        elif current == "EFO":
            r = efo.loc[d]
        else:
            r = bnd.loc[d]
        out.append(r)
        if d in rebal_dates and d - pd.Timedelta(days=400) >= returns.index[0]:
            vix_today = vix_aligned.loc[d] if not pd.isna(vix_aligned.loc[d]) else 20.0
            if vix_today > vix_high:
                current = "BND"
                continue
            sig = _dm_step(d, spy, efa, {"6m": 126, "12m": 252},
                           {"6m": 0.5, "12m": 0.5}, 21, None,
                           signal_only_mode="absolute_only")
            if sig is not None:
                current = "SPUU" if sig == "SPUU_or_1x_SPY" else (
                          "EFO" if sig == "EFO_or_1x_EFA" else "BND")
    return pd.Series(out, index=returns.index)


def bt_dual_momentum_abs_tmf_hedge(returns, prices, hedge_pct=0.30):
    """70% absolute-only DM + 30% TMF (3× long bonds). HFEA-style crash hedge."""
    base = bt_dual_momentum_abs_only(returns, prices)
    tmf = spliced_leveraged_etf(returns, "TMF")
    return (1 - hedge_pct) * base + hedge_pct * tmf


def bt_dual_momentum_abs_sma_overlay(returns, prices):
    """
    Absolute-only DM + SPY 200-SMA overlay. Force BND when SPY is below
    its 200-SMA, regardless of the momentum signal.
    Belt-and-suspenders — the momentum filter catches slow regime changes,
    the SMA filter catches fast crashes.
    """
    spy = prices["SPY"]
    spy_sma = spy.rolling(200).mean()
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    spuu = spliced_leveraged_etf(returns, "SPUU")
    efo = spliced_leveraged_etf(returns, "EFO")
    efa = prices["EFA"]
    bnd = returns["BND"].fillna(0) if "BND" in returns.columns else pd.Series(0.0, index=returns.index)

    current = "BND"
    out = []
    for d in returns.index:
        if current == "SPUU":
            r = spuu.loc[d]
        elif current == "EFO":
            r = efo.loc[d]
        else:
            r = bnd.loc[d]
        out.append(r)
        if d in rebal_dates and d - pd.Timedelta(days=400) >= returns.index[0]:
            # Hard gate: if SPY below 200-SMA on any rebal, force BND
            sma = spy_sma.loc[d]
            if not pd.isna(sma) and spy.loc[d] < sma:
                current = "BND"
                continue
            sig = _dm_step(d, spy, efa, {"6m": 126, "12m": 252},
                           {"6m": 0.5, "12m": 0.5}, 21, None,
                           signal_only_mode="absolute_only")
            if sig is not None:
                current = "SPUU" if sig == "SPUU_or_1x_SPY" else (
                          "EFO" if sig == "EFO_or_1x_EFA" else "BND")
    return pd.Series(out, index=returns.index)


def _bt_sector_momentum_generic(returns, prices, sector_universe, lookbacks,
                                  weights, top_n=3, skip_days=0,
                                  individual_sma=None, vol_window=None):
    """
    Generic sector momentum runner.
      sector_universe: list of sector tickers (e.g. SECTOR_ETFS or SECTOR_ETFS_1X)
      individual_sma: if set, each sector must be above its own SMA over this period
      vol_window: if set, weight selected sectors by 1/vol
    """
    spy = prices["SPY"]
    spy_sma = spy.rolling(200).mean()
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    available = [s for s in sector_universe if s in prices.columns]
    holding = []  # list of (sector, weight) tuples
    out = []
    for d in returns.index:
        if d in rebal_dates:
            spy_above = spy.loc[d] > spy_sma.loc[d] * 1.01 if not pd.isna(spy_sma.loc[d]) else False
            if not spy_above:
                holding = [(SECTOR_BOND_ETF, 1.0)]
            else:
                signal_date = d - pd.Timedelta(days=skip_days) if skip_days else d
                scores = {}
                for sec in available:
                    score = 0
                    have_all = True
                    for label, lookback in lookbacks.items():
                        try:
                            past = prices[sec].asof(d - pd.Timedelta(days=int(lookback * 1.45)))
                            now = prices[sec].asof(signal_date)
                            if pd.isna(past) or past <= 0 or pd.isna(now):
                                have_all = False
                                break
                            ret = (now / past) - 1
                            score += weights[label] * ret
                        except Exception:
                            have_all = False
                            break
                    if have_all:
                        # Optional: each sector must be above its own SMA
                        if individual_sma:
                            sec_sma = prices[sec].rolling(individual_sma).mean().loc[d]
                            if pd.isna(sec_sma) or prices[sec].loc[d] < sec_sma:
                                continue
                        scores[sec] = score
                if scores:
                    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
                    if vol_window:
                        # Inverse-vol weighting among the top
                        inv_vols = []
                        for s, _ in top:
                            try:
                                vol = returns[s].iloc[max(0, returns.index.get_loc(d) - vol_window):returns.index.get_loc(d)].std()
                                inv_vols.append(1 / vol if vol > 0 else 1.0)
                            except Exception:
                                inv_vols.append(1.0)
                        total = sum(inv_vols)
                        holding = [(s, iv / total) for (s, _), iv in zip(top, inv_vols)]
                    else:
                        w = 1.0 / len(top)
                        holding = [(s, w) for s, _ in top]
                else:
                    holding = [(SECTOR_HOLDING_FUND, 1.0)]
        if not holding:
            out.append(0)
            continue
        ret = sum(weight * returns[h].loc[d] for h, weight in holding if h in returns.columns)
        out.append(ret)
    return pd.Series(out, index=returns.index)


def bt_sector_momentum_top1(returns, prices):
    """Top 1 sector only — max concentration on the best 2× ETF."""
    return _bt_sector_momentum_generic(
        returns, prices, SECTOR_ETFS,
        SECTOR_LOOKBACKS_DAYS, SECTOR_WEIGHTS, top_n=1,
    )


def bt_sector_momentum_1x_top1(returns, prices):
    """Top 1 sector, 1× SPDR — concentrated but unleveraged."""
    return _bt_sector_momentum_generic(
        returns, prices, SECTOR_ETFS_1X,
        SECTOR_LOOKBACKS_DAYS, SECTOR_WEIGHTS, top_n=1,
    )


def bt_sector_momentum_1x_top5(returns, prices):
    """Top 5 sectors, 1× SPDR — more diversified basket."""
    return _bt_sector_momentum_generic(
        returns, prices, SECTOR_ETFS_1X,
        SECTOR_LOOKBACKS_DAYS, SECTOR_WEIGHTS, top_n=5,
    )


def bt_sector_momentum_1x_indiv_trend(returns, prices):
    """1× SPDR top-3 with individual sector 200-SMA filter — each sector must be above its own SMA."""
    return _bt_sector_momentum_generic(
        returns, prices, SECTOR_ETFS_1X,
        SECTOR_LOOKBACKS_DAYS, SECTOR_WEIGHTS, top_n=3,
        individual_sma=200,
    )


def bt_sector_momentum_1x_volscaled(returns, prices):
    """1× SPDR top-3 weighted by inverse 60-day vol (risk parity within selection)."""
    return _bt_sector_momentum_generic(
        returns, prices, SECTOR_ETFS_1X,
        SECTOR_LOOKBACKS_DAYS, SECTOR_WEIGHTS, top_n=3,
        vol_window=60,
    )


def bt_sector_momentum_sharpe_ranked(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """
    Rank sectors by Sharpe-style score (return / 6-month vol) instead of raw return.
    Theory: rewards sectors that trended *consistently*, not ones with one lucky month.
    Uses 1× SPDR ETFs.
    """
    spy = prices["SPY"]
    spy_sma = spy.rolling(200).mean()
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    available = [s for s in SECTOR_ETFS_1X if s in prices.columns]
    holding = []
    out = []
    for d in returns.index:
        if d in rebal_dates:
            spy_above = spy.loc[d] > spy_sma.loc[d] * 1.01 if not pd.isna(spy_sma.loc[d]) else False
            if not spy_above:
                holding = [SECTOR_BOND_ETF]
            else:
                scores = {}
                for sec in available:
                    score = 0
                    have_all = True
                    for label, lookback in SECTOR_LOOKBACKS_DAYS.items():
                        try:
                            past = prices[sec].asof(d - pd.Timedelta(days=int(lookback * 1.45)))
                            now = prices[sec].loc[d]
                            if pd.isna(past) or past <= 0:
                                have_all = False
                                break
                            ret = (now / past) - 1
                            score += SECTOR_WEIGHTS[label] * ret
                        except Exception:
                            have_all = False
                            break
                    if not have_all:
                        continue
                    # Divide by recent realized vol — penalize choppy moves
                    try:
                        idx_loc = returns.index.get_loc(d)
                        vol_window = returns[sec].iloc[max(0, idx_loc - 126):idx_loc].std() * np.sqrt(252)
                        if vol_window > 0:
                            scores[sec] = score / vol_window
                    except Exception:
                        pass
                if scores:
                    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:SECTOR_TOP_N]
                    holding = [s for s, _ in top]
                else:
                    holding = [SECTOR_HOLDING_FUND]
        if not holding:
            out.append(0)
            continue
        weight = 1.0 / len(holding)
        ret = sum(weight * returns[h].loc[d] for h in holding if h in returns.columns)
        out.append(ret)
    return pd.Series(out, index=returns.index)


def bt_sector_momentum_threshold(returns: pd.DataFrame, prices: pd.DataFrame, min_score=0.05) -> pd.Series:
    """
    Only invest when the average score of top-3 sectors is decisively positive
    (> min_score = 5%). Otherwise sit in SHV. Avoids weak/whipsaw signals.
    1× SPDR universe.
    """
    spy = prices["SPY"]
    spy_sma = spy.rolling(200).mean()
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    available = [s for s in SECTOR_ETFS_1X if s in prices.columns]
    holding = []
    out = []
    for d in returns.index:
        if d in rebal_dates:
            spy_above = spy.loc[d] > spy_sma.loc[d] * 1.01 if not pd.isna(spy_sma.loc[d]) else False
            if not spy_above:
                holding = [SECTOR_BOND_ETF]
            else:
                scores = {}
                for sec in available:
                    score = 0
                    have_all = True
                    for label, lookback in SECTOR_LOOKBACKS_DAYS.items():
                        try:
                            past = prices[sec].asof(d - pd.Timedelta(days=int(lookback * 1.45)))
                            now = prices[sec].loc[d]
                            if pd.isna(past) or past <= 0:
                                have_all = False
                                break
                            ret = (now / past) - 1
                            score += SECTOR_WEIGHTS[label] * ret
                        except Exception:
                            have_all = False
                            break
                    if have_all:
                        scores[sec] = score
                if scores:
                    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:SECTOR_TOP_N]
                    avg_top_score = sum(sc for _, sc in top) / len(top)
                    if avg_top_score >= min_score:
                        holding = [s for s, _ in top]
                    else:
                        # Weak signal — go to safety
                        holding = [SECTOR_HOLDING_FUND]
                else:
                    holding = [SECTOR_HOLDING_FUND]
        if not holding:
            out.append(0)
            continue
        weight = 1.0 / len(holding)
        ret = sum(weight * returns[h].loc[d] for h in holding if h in returns.columns)
        out.append(ret)
    return pd.Series(out, index=returns.index)


def bt_sector_momentum_vix_gated_lev(returns, prices, vix_df):
    """
    Conditional leverage by volatility regime:
      VIX < 18 → 2× ProShares ETFs (high-conviction equity bull)
      VIX 18-25 → 1× SPDR ETFs (normal regime)
      VIX > 25 → SHV cash (risk-off)
    Plus the SPY 200-SMA umbrella gate.
    """
    spy = prices["SPY"]
    spy_sma = spy.rolling(200).mean()
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    out = []
    holding = []
    if vix_df is not None:
        vix_aligned = vix_df["value"].reindex(returns.index, method="ffill")
    else:
        vix_aligned = pd.Series(20.0, index=returns.index)
    sec_2x_avail = [s for s in SECTOR_ETFS if s in prices.columns]
    sec_1x_avail = [s for s in SECTOR_ETFS_1X if s in prices.columns]
    # Mapping 2x → 1x for when we want to fall back from 2x to 1x of same sector
    pair_2x_to_1x = dict(zip(SECTOR_ETFS, SECTOR_ETFS_1X))

    for d in returns.index:
        if d in rebal_dates:
            spy_above = spy.loc[d] > spy_sma.loc[d] * 1.01 if not pd.isna(spy_sma.loc[d]) else False
            vix = vix_aligned.loc[d] if not pd.isna(vix_aligned.loc[d]) else 20.0

            if not spy_above or vix > 25.0:
                holding = [SECTOR_HOLDING_FUND]
            else:
                # Pick universe based on VIX
                if vix < 18.0:
                    universe = sec_2x_avail
                else:
                    universe = sec_1x_avail
                scores = {}
                for sec in universe:
                    score = 0
                    have_all = True
                    for label, lookback in SECTOR_LOOKBACKS_DAYS.items():
                        try:
                            past = prices[sec].asof(d - pd.Timedelta(days=int(lookback * 1.45)))
                            now = prices[sec].loc[d]
                            if pd.isna(past) or past <= 0:
                                have_all = False
                                break
                            ret = (now / past) - 1
                            score += SECTOR_WEIGHTS[label] * ret
                        except Exception:
                            have_all = False
                            break
                    if have_all:
                        scores[sec] = score
                if scores:
                    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:SECTOR_TOP_N]
                    holding = [s for s, _ in top]
                else:
                    holding = [SECTOR_HOLDING_FUND]
        if not holding:
            out.append(0)
            continue
        weight = 1.0 / len(holding)
        ret = sum(weight * returns[h].loc[d] for h in holding if h in returns.columns)
        out.append(ret)
    return pd.Series(out, index=returns.index)


def bt_sector_momentum_strong_signal_2x(returns, prices, strong_threshold=0.08):
    """
    Use 2× ProShares ETFs when momentum score is strong (avg top-3 > 8%),
    else 1× SPDR ETFs. The leverage activates only when conviction is high.
    """
    spy = prices["SPY"]
    spy_sma = spy.rolling(200).mean()
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    sec_2x_avail = [s for s in SECTOR_ETFS if s in prices.columns]
    sec_1x_avail = [s for s in SECTOR_ETFS_1X if s in prices.columns]
    pair_2x_to_1x = dict(zip(SECTOR_ETFS, SECTOR_ETFS_1X))
    out = []
    holding = []
    use_2x = False

    for d in returns.index:
        if d in rebal_dates:
            spy_above = spy.loc[d] > spy_sma.loc[d] * 1.01 if not pd.isna(spy_sma.loc[d]) else False
            if not spy_above:
                holding = [SECTOR_BOND_ETF]
            else:
                # Score on 1× ETFs (broader universe + longer history)
                scores = {}
                for sec in sec_1x_avail:
                    score = 0
                    have_all = True
                    for label, lookback in SECTOR_LOOKBACKS_DAYS.items():
                        try:
                            past = prices[sec].asof(d - pd.Timedelta(days=int(lookback * 1.45)))
                            now = prices[sec].loc[d]
                            if pd.isna(past) or past <= 0:
                                have_all = False
                                break
                            ret = (now / past) - 1
                            score += SECTOR_WEIGHTS[label] * ret
                        except Exception:
                            have_all = False
                            break
                    if have_all:
                        scores[sec] = score
                if scores:
                    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:SECTOR_TOP_N]
                    avg_top_score = sum(sc for _, sc in top) / len(top)
                    use_2x = avg_top_score >= strong_threshold and all(s in pair_2x_to_1x.values() for s, _ in top)
                    if use_2x:
                        # Map 1× selection to 2× equivalents
                        rev_map = {v: k for k, v in pair_2x_to_1x.items()}
                        holding = [rev_map.get(s, s) for s, _ in top if rev_map.get(s, s) in sec_2x_avail]
                        if not holding:
                            holding = [s for s, _ in top]  # fallback to 1×
                    else:
                        holding = [s for s, _ in top]
                else:
                    holding = [SECTOR_HOLDING_FUND]
        if not holding:
            out.append(0)
            continue
        weight = 1.0 / len(holding)
        ret = sum(weight * returns[h].loc[d] for h in holding if h in returns.columns)
        out.append(ret)
    return pd.Series(out, index=returns.index)


def bt_sector_momentum_with_tmf_hedge(returns, prices, hedge_pct=0.30):
    """
    HFEA-style sector momentum: 70% in 1× SPDR top-3, 30% in TMF (3× long bonds).
    The TMF sleeve hedges the equity portion in deflationary crashes (2008, 2020)
    while still letting the sector momentum component capture rotation alpha.
    """
    base = bt_sector_momentum_1x(returns, prices)
    tmf = spliced_leveraged_etf(returns, "TMF")
    return (1 - hedge_pct) * base + hedge_pct * tmf


def bt_sector_momentum_vs_spy(returns, prices, margin=0.05):
    """
    Each month: compare the top sector's score to SPY's own momentum score.
    If top sector beats SPY by `margin` → rotate to top sector (1× SPDR).
    Otherwise → just hold SPY.
    This is "only deviate from the index when there's a CLEAR winner".
    """
    spy = prices["SPY"]
    spy_sma = spy.rolling(200).mean()
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    available = [s for s in SECTOR_ETFS_1X if s in prices.columns]
    holding = "SPY"
    out = []
    for d in returns.index:
        if d in rebal_dates:
            spy_above = spy.loc[d] > spy_sma.loc[d] * 1.01 if not pd.isna(spy_sma.loc[d]) else False
            if not spy_above:
                holding = SECTOR_BOND_ETF
            else:
                # Score SPY too
                spy_score = 0
                for label, lookback in SECTOR_LOOKBACKS_DAYS.items():
                    try:
                        past = spy.asof(d - pd.Timedelta(days=int(lookback * 1.45)))
                        if pd.notna(past) and past > 0:
                            spy_score += SECTOR_WEIGHTS[label] * (spy.loc[d] / past - 1)
                    except Exception:
                        pass

                # Score each sector
                scores = {}
                for sec in available:
                    score = 0
                    have_all = True
                    for label, lookback in SECTOR_LOOKBACKS_DAYS.items():
                        try:
                            past = prices[sec].asof(d - pd.Timedelta(days=int(lookback * 1.45)))
                            now = prices[sec].loc[d]
                            if pd.isna(past) or past <= 0:
                                have_all = False
                                break
                            score += SECTOR_WEIGHTS[label] * (now / past - 1)
                        except Exception:
                            have_all = False
                            break
                    if have_all:
                        scores[sec] = score
                if scores:
                    best_sec, best_score = max(scores.items(), key=lambda kv: kv[1])
                    holding = best_sec if (best_score - spy_score) > margin else "SPY"
                else:
                    holding = "SPY"
        out.append(returns[holding].loc[d] if holding in returns.columns else 0)
    return pd.Series(out, index=returns.index)


def bt_sector_momentum_tiered_leverage(returns, prices, vix_df):
    """
    Tiered conditional leverage based on signal strength AND volatility regime:
      • Top sector score > 15% AND VIX < 18 → 2× ProShares sector (most aggressive)
      • Top sector score > 5%  AND VIX < 25 → 1× SPDR sector (normal)
      • Top sector score < 5% OR SPY < 200-SMA → SPY (default to market)
      • VIX > 25 OR SPY < 200-SMA × 0.99 → cash (SHV)
    Single sector at a time (top-1) when conviction is high.
    """
    spy = prices["SPY"]
    spy_sma = spy.rolling(200).mean()
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    sec_1x = [s for s in SECTOR_ETFS_1X if s in prices.columns]
    pair_2x = dict(zip(SECTOR_ETFS_1X, SECTOR_ETFS))  # XLK→ROM, XLF→UYG, etc.
    if vix_df is not None:
        vix_aligned = vix_df["value"].reindex(returns.index, method="ffill")
    else:
        vix_aligned = pd.Series(20.0, index=returns.index)
    holding = "SPY"
    out = []
    for d in returns.index:
        if d in rebal_dates:
            spy_today = spy.loc[d]
            sma_today = spy_sma.loc[d]
            vix = vix_aligned.loc[d] if not pd.isna(vix_aligned.loc[d]) else 20.0

            # Hardcrash protection
            if pd.notna(sma_today) and spy_today < sma_today * 0.99:
                holding = SECTOR_HOLDING_FUND
            elif vix > 25:
                holding = SECTOR_HOLDING_FUND
            else:
                scores = {}
                for sec in sec_1x:
                    score = 0
                    have_all = True
                    for label, lookback in SECTOR_LOOKBACKS_DAYS.items():
                        try:
                            past = prices[sec].asof(d - pd.Timedelta(days=int(lookback * 1.45)))
                            now = prices[sec].loc[d]
                            if pd.isna(past) or past <= 0:
                                have_all = False
                                break
                            score += SECTOR_WEIGHTS[label] * (now / past - 1)
                        except Exception:
                            have_all = False
                            break
                    if have_all:
                        scores[sec] = score
                if scores:
                    best_1x, best_score = max(scores.items(), key=lambda kv: kv[1])
                    best_2x = pair_2x.get(best_1x)
                    if best_score > 0.15 and vix < 18 and best_2x in returns.columns:
                        holding = best_2x   # 2× sector — high conviction
                    elif best_score > 0.05 and vix < 25:
                        holding = best_1x   # 1× sector — normal
                    else:
                        holding = "SPY"     # Weak signal — just be in market
                else:
                    holding = "SPY"
        out.append(returns[holding].loc[d] if holding in returns.columns else 0)
    return pd.Series(out, index=returns.index)


def _top_sector_as_of(prices, d, universe=SECTOR_ETFS, top_n=1):
    """Return list of top-N highest-momentum sector tickers as of date d."""
    available = [s for s in universe if s in prices.columns]
    scores = {}
    for sec in available:
        score = 0
        have_all = True
        for label, lookback in SECTOR_LOOKBACKS_DAYS.items():
            try:
                past = prices[sec].asof(d - pd.Timedelta(days=int(lookback * 1.45)))
                now = prices[sec].loc[d]
                if pd.isna(past) or past <= 0 or pd.isna(now):
                    have_all = False
                    break
                score += SECTOR_WEIGHTS[label] * (now / past - 1)
            except Exception:
                have_all = False
                break
        if have_all:
            scores[sec] = score
    if not scores:
        return None
    return [s for s, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_n]]


def bt_regime_sso_with_sector_swap(returns, prices, vix_df, fed_df):
    """
    Regime SSO, but when the regime detector says "in market", hold the
    top-momentum 2× sector ETF instead of SSO. Safe asset stays USFR.
    Same regime-detection logic — sector swap only changes WHICH 2× equity
    we own when bullish. Monthly sector selection allows rotation while
    still in the bullish regime.
    """
    cfg = REGIME_CFG
    spy = prices["SPY"]
    spy_sma200 = spy.rolling(cfg["spy_sma_period"]).mean()
    usfr = spliced_usfr(returns)

    # === Signal computation (mirrors bt_regime_sso) ===
    raw_trend = pd.Series(0, index=spy.index)
    raw_trend[spy > spy_sma200] = 1
    raw_trend[spy < spy_sma200] = -1
    s1 = pd.Series(0, index=spy.index)
    last_signal = 0
    for i, d in enumerate(spy.index):
        if i < 2:
            s1.iloc[i] = last_signal
            continue
        last3 = raw_trend.iloc[i - 2:i + 1].values
        if all(v != 0 and v == last3[-1] for v in last3):
            last_signal = int(last3[-1])
        s1.iloc[i] = last_signal

    s3 = pd.Series(0, index=spy.index)
    if vix_df is not None:
        vix_aligned = vix_df["value"].reindex(spy.index, method="ffill")
        vix_5d_change = vix_aligned.pct_change(5)
        s3[(vix_aligned > cfg["vix_high"]) | (vix_5d_change > cfg["vix_5d_change_high"])] = -1
        s3[(vix_aligned < cfg["vix_low"]) & (vix_5d_change < 0.10)] = 1

    s4 = pd.Series(0, index=spy.index)
    s5 = pd.Series(0, index=spy.index)
    if "HYG" in prices.columns and "LQD" in prices.columns:
        ratio = prices["HYG"] / prices["LQD"]
        ratio_sma = ratio.rolling(cfg["credit_sma_period"]).mean()
        s5[ratio > ratio_sma * 1.002] = 1
        s5[ratio < ratio_sma * 0.998] = -1
    s7 = pd.Series(0, index=spy.index)
    canary_above = pd.Series(0, index=spy.index)
    canary_below = pd.Series(0, index=spy.index)
    for sym in ("HYG", "EEM", "IWM"):
        if sym not in prices.columns:
            continue
        sma = prices[sym].rolling(cfg["canary_sma_period"]).mean()
        canary_above += (prices[sym] > sma).astype(int)
        canary_below += (prices[sym] < sma).astype(int)
    s7[canary_below >= 3] = -1
    s7[canary_above >= 3] = 1

    composite = s1 + s3 + s4 + s5 + s7
    composites_list = composite.values

    fed_hike = pd.Series(False, index=spy.index)
    if fed_df is not None:
        fed_aligned = fed_df["value"].reindex(spy.index, method="ffill")
        fed_change = fed_aligned - fed_aligned.shift(cfg["fed_hike_lookback_days"])
        fed_hike = fed_change >= (cfg["fed_hike_threshold_bps"] / 100)

    # === Decision loop: in-market = top 2× sector ===
    monthly_dates = set(_monthly_rebal_dates(spy.index))
    in_market = True
    current_sector = None
    out = []
    sso_series = spliced_leveraged_etf(returns, "SSO")  # Synthetic pre-2006-06-21, real after
    for i, d in enumerate(spy.index):
        # Determine today's return
        if in_market:
            if current_sector is None or current_sector not in returns.columns:
                # Fall back to SSO if no sector available yet (use spliced synth+real)
                today_ret = sso_series.loc[d]
            else:
                today_ret = returns[current_sector].loc[d]
        else:
            today_ret = usfr.loc[d]
        out.append(today_ret)

        # End-of-day decisions
        if i < cfg["slow_exit_days"]:
            continue
        c_slow = composites_list[i - cfg["slow_exit_days"] + 1: i + 1]
        c_fast = composites_list[i - cfg["fast_exit_days"] + 1: i + 1]
        c_reentry = composites_list[i - cfg["standard_reentry_days"] + 1: i + 1]
        if in_market:
            if all(c <= cfg["fast_exit_score"] for c in c_fast):
                in_market = False
                current_sector = None
            elif all(c <= cfg["slow_exit_score"] for c in c_slow):
                in_market = False
                current_sector = None
        else:
            if not fed_hike.iloc[i]:
                if all(c >= cfg["reentry_score"] for c in c_reentry):
                    in_market = True
                    # Pick top sector at re-entry
                    top = _top_sector_as_of(prices, d, SECTOR_ETFS, top_n=1)
                    current_sector = top[0] if top else None

        # Monthly rotation within in-market regime
        if in_market and d in monthly_dates:
            top = _top_sector_as_of(prices, d, SECTOR_ETFS, top_n=1)
            if top:
                current_sector = top[0]
    return pd.Series(out, index=spy.index)


def bt_hfea_with_sector_swap(returns, prices):
    """
    HFEA with sector swap: replace UPRO sleeve with top-1 momentum 2× sector.
    Keep TMF (25%) and KMLM (30%) bond/managed-futures sleeves intact.
    Sector picked monthly; HFEA quarterly rebalance to 45/25/30 target weights.
    The hypothesis: sector rotation alpha amplifies HFEA's equity sleeve.
    """
    tmf = spliced_leveraged_etf(returns, "TMF")
    kmlm = spliced_kmlm(returns)
    monthly_dates = set(_monthly_rebal_dates(returns.index))
    quarterly_dates = set(_quarterly_rebal_dates(returns.index))
    kmlm_split = pd.Timestamp(KMLM_LIVE_FROM_DATE)

    current_sector = None
    # Initial weights: target HFEA split, equity sleeve in placeholder UPRO
    w_eq, w_tmf, w_kmlm = 0.55, 0.45, 0.0  # pre-KMLM era weights
    out = []
    for d in returns.index:
        # Pick sector on monthly rebalance
        if current_sector is None or d in monthly_dates:
            top = _top_sector_as_of(prices, d, SECTOR_ETFS, top_n=1)
            if top:
                current_sector = top[0]
            elif current_sector is None:
                current_sector = "ROM" if "ROM" in returns.columns else "UPRO"

        # Use spliced UPRO if before sector ETFs available (pre-2007)
        if current_sector in returns.columns:
            eq_ret = returns[current_sector].loc[d]
        else:
            eq_ret = spliced_leveraged_etf(returns, "UPRO").loc[d]

        port_ret = w_eq * eq_ret + w_tmf * tmf.loc[d] + w_kmlm * kmlm.loc[d]
        out.append(port_ret)

        # Drift
        new_eq = w_eq * (1 + eq_ret)
        new_tmf = w_tmf * (1 + tmf.loc[d])
        new_kmlm = w_kmlm * (1 + kmlm.loc[d])
        s = new_eq + new_tmf + new_kmlm
        if s > 0:
            w_eq, w_tmf, w_kmlm = new_eq / s, new_tmf / s, new_kmlm / s

        # Quarterly rebalance to target weights, accounting for KMLM era
        if d in quarterly_dates:
            if d >= kmlm_split:
                w_eq, w_tmf, w_kmlm = 0.45, 0.25, 0.30
            else:
                w_eq, w_tmf, w_kmlm = 0.55, 0.45, 0.0
    return pd.Series(out, index=returns.index)


def bt_spxl_sma_with_sector_swap(returns, prices):
    """
    SPXL SMA, but when SPY > 200-SMA × 1.01, hold the top-momentum 2× sector
    instead of SPXL (3×). When SPY < SMA × 0.99, hold SGOV (cash).
    Trade-off: lose 3× leverage, gain sector selection alpha.
    """
    spy = prices["SPY"]
    spy_sma = spy.rolling(200).mean()
    monthly_dates = set(_monthly_rebal_dates(returns.index))
    sgov = returns["SGOV"].fillna(0) if "SGOV" in returns.columns else pd.Series(0.0, index=returns.index)
    state = "OUT"
    current_sector = None
    out = []
    for d in returns.index:
        # State transitions
        sma_today = spy_sma.loc[d]
        if pd.notna(sma_today):
            if spy.loc[d] > sma_today * 1.01:
                if state != "IN":
                    state = "IN"
                    top = _top_sector_as_of(prices, d, SECTOR_ETFS, top_n=1)
                    current_sector = top[0] if top else "SPXL"
            elif spy.loc[d] < sma_today * 0.99:
                state = "OUT"
                current_sector = None

        # Monthly sector rotation while IN
        if state == "IN" and d in monthly_dates:
            top = _top_sector_as_of(prices, d, SECTOR_ETFS, top_n=1)
            if top:
                current_sector = top[0]

        if state == "IN" and current_sector in returns.columns:
            out.append(returns[current_sector].loc[d])
        else:
            out.append(sgov.loc[d])
    return pd.Series(out, index=returns.index)


def bt_sector_momentum_overlay(returns, prices, sector_weight=0.30):
    """
    SPY base (70%) + sector momentum overlay (30%). Holds the broad market
    most of the time; uses sector momentum as a small overlay for alpha
    without paying the full rotation tax. Could be the "best of both worlds"
    if sector picks generate marginal alpha.
    """
    base = returns["SPY"].fillna(0)
    overlay = bt_sector_momentum_1x(returns, prices)
    return (1 - sector_weight) * base + sector_weight * overlay


def bt_sector_momentum_1x(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """
    Sector Momentum on 1× SPDR sector ETFs (XLK, XLF, XLE, XLV, XLI, XLP, XLY,
    XLU, XLB) instead of the 2× ProShares versions. Same selection logic
    (multi-period weighted momentum, top 3 equal-weight, SPY 200-SMA gate)
    but with structurally lower drawdown and longer history (since 1999).

    Sector rotation works at any leverage; the 2× wrapper just amplifies both
    sides. With 1× ETFs the strategy retains the rotation alpha but cuts the
    drawdown roughly in half.
    """
    spy = prices["SPY"]
    spy_sma = spy.rolling(200).mean()
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    available = [s for s in SECTOR_ETFS_1X if s in prices.columns]
    holding = []
    out = []
    for d in returns.index:
        if d in rebal_dates:
            spy_above = spy.loc[d] > spy_sma.loc[d] * 1.01 if not pd.isna(spy_sma.loc[d]) else False
            if not spy_above:
                holding = [SECTOR_BOND_ETF]
            else:
                scores = {}
                for sec in available:
                    score = 0
                    have_all = True
                    for label, lookback in SECTOR_LOOKBACKS_DAYS.items():
                        try:
                            past = prices[sec].asof(d - pd.Timedelta(days=int(lookback * 1.45)))
                            now = prices[sec].loc[d]
                            if pd.isna(past) or past <= 0:
                                have_all = False
                                break
                            ret = (now / past) - 1
                            score += SECTOR_WEIGHTS[label] * ret
                        except Exception:
                            have_all = False
                            break
                    if have_all:
                        scores[sec] = score
                if scores:
                    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:SECTOR_TOP_N]
                    holding = [s for s, _ in top]
                else:
                    holding = [SECTOR_HOLDING_FUND]
        if not holding:
            out.append(0)
            continue
        weight = 1.0 / len(holding)
        ret = sum(weight * returns[h].loc[d] for h in holding if h in returns.columns)
        out.append(ret)
    return pd.Series(out, index=returns.index)


def bt_regime_world(returns: pd.DataFrame, prices: pd.DataFrame, vix_df: pd.DataFrame, fed_df: pd.DataFrame) -> pd.Series:
    """
    Regime World: 5-signal composite applied to MSCI World, trading the
    Leverage Shares 2× World ETP (WLDU) when bullish, USFR when defensive.

    Risk asset: WLDU (2× MSCI World daily reset) — synthetic before its
    2026-03-12 launch (2 × URTH − financing − ER), live after.
    Safe asset: USFR.

    Signals (5 of 7 — news + breadth omitted as in regime_sso backtest):
      1. URTH (the underlying index) vs 255-SMA with 3-day temporal hysteresis
      3. VIX level + 5-day trajectory
      4. ADX on URTH — skipped in backtest for simplicity, same as regime_sso
      5. HYG/LQD credit spread
      7. Canary universe (HYG, EEM, IWM)
    Plus the Fed-hike filter on re-entries.

    Note: signals are computed on URTH (the unleveraged underlying) but trade
    sizing happens via WLDU. This is the correct approach — the regime
    detector is reading what the *world equity market* is doing, then
    translating that into a leveraged or defensive position.
    """
    cfg = REGIME_WORLD_CFG
    safe_sym = cfg["safe_asset"]
    risk_ret = spliced_wldu(returns)   # 2× World — synthetic (2× spliced URTH) + live WLDU
    safe_ret = spliced_usfr(returns)   # USFR live 2014+; SHY proxy 2002-2014

    # Use spliced URTH for the trend signal — extends back to 2008 via VT
    urth_proxy_ret = spliced_urth(returns)
    # Build a price series from cumulative returns of the spliced URTH proxy
    urth = (1.0 + urth_proxy_ret).cumprod() * 100  # arbitrary base
    if "URTH" in prices.columns:
        # Where actual URTH price exists, use it directly so we get correct levels
        urth = urth.combine_first(prices["URTH"])
    urth_sma = urth.rolling(cfg["world_sma_period"]).mean()

    # Signal 1: URTH 255-SMA with 3-day hysteresis
    raw_trend = pd.Series(0, index=urth.index)
    raw_trend[urth > urth_sma] = 1
    raw_trend[urth < urth_sma] = -1
    s1 = pd.Series(0, index=urth.index)
    last_signal = 0
    for i, d in enumerate(urth.index):
        if i < 2:
            s1.iloc[i] = last_signal
            continue
        last3 = raw_trend.iloc[i - 2:i + 1].values
        if all(v != 0 and v == last3[-1] for v in last3):
            last_signal = int(last3[-1])
        s1.iloc[i] = last_signal

    # Signal 3: VIX (level + trajectory) — same as regime_sso
    s3 = pd.Series(0, index=urth.index)
    if vix_df is not None:
        vix_aligned = vix_df["value"].reindex(urth.index, method="ffill")
        vix_5d_change = vix_aligned.pct_change(5)
        s3[(vix_aligned > cfg["vix_high"]) | (vix_5d_change > cfg["vix_5d_change_high"])] = -1
        s3[(vix_aligned < cfg["vix_low"]) & (vix_5d_change < 0.10)] = 1

    # Signal 4: ADX on URTH (not SPY). Use simple proxy: skip in backtest as before.
    s4 = pd.Series(0, index=urth.index)

    # Signal 5: HYG/LQD credit
    s5 = pd.Series(0, index=urth.index)
    if "HYG" in prices.columns and "LQD" in prices.columns:
        ratio = prices["HYG"] / prices["LQD"]
        ratio_sma = ratio.rolling(cfg["credit_sma_period"]).mean()
        s5[ratio > ratio_sma * 1.002] = 1
        s5[ratio < ratio_sma * 0.998] = -1

    # Signal 7: canary universe
    s7 = pd.Series(0, index=urth.index)
    canary_above = pd.Series(0, index=urth.index)
    canary_below = pd.Series(0, index=urth.index)
    for sym in ("HYG", "EEM", "IWM"):
        if sym not in prices.columns:
            continue
        sma = prices[sym].rolling(cfg["canary_sma_period"]).mean()
        canary_above += (prices[sym] > sma).astype(int)
        canary_below += (prices[sym] < sma).astype(int)
    s7[canary_below >= 3] = -1
    s7[canary_above >= 3] = 1

    composite = s1 + s3 + s4 + s5 + s7

    # Fed-hike filter
    fed_hike = pd.Series(False, index=urth.index)
    if fed_df is not None:
        fed_aligned = fed_df["value"].reindex(urth.index, method="ffill")
        fed_change = fed_aligned - fed_aligned.shift(cfg["fed_hike_lookback_days"])
        fed_hike = fed_change >= (cfg["fed_hike_threshold_bps"] / 100)

    # Decision loop (mirror regime_sso)
    position = "WLDU"
    out = []
    composites_list = composite.values
    for i, d in enumerate(urth.index):
        out.append(risk_ret.loc[d] if position == "WLDU" else safe_ret.loc[d])
        if i < cfg["slow_exit_days"]:
            continue
        c_recent_slow = composites_list[i - cfg["slow_exit_days"] + 1: i + 1]
        c_recent_fast = composites_list[i - cfg["fast_exit_days"] + 1: i + 1]
        c_recent_reentry = composites_list[i - cfg["standard_reentry_days"] + 1: i + 1]
        if position == "WLDU":
            if all(c <= cfg["fast_exit_score"] for c in c_recent_fast):
                position = safe_sym
            elif all(c <= cfg["slow_exit_score"] for c in c_recent_slow):
                position = safe_sym
        else:
            if fed_hike.iloc[i]:
                continue
            if all(c >= cfg["reentry_score"] for c in c_recent_reentry):
                position = "WLDU"
    return pd.Series(out, index=urth.index)


# ═══════════════════════════════════════════════════════════════════════
# RESEARCH WAVE — 29 ADDITIONAL STRATEGIES
# Drawn from Hedgefundie/Boglehead/LETF forums + academic papers (Antonacci,
# Keller, Faber, Butler-Philbrick-Gordillo, Asness, Maillard, etc.)
# All routed through extended-history data + proper LETF splicing.
# ═══════════════════════════════════════════════════════════════════════


# ─── Synthetic capital-efficient stack returns (NTSX-family, GDE) ─────

def synthetic_ntsx_returns(returns: pd.DataFrame, expense_ratio: float = 0.0020) -> pd.Series:
    """NTSX = 90% SPY + 60% 5-7y Treasury futures (collateralized). Pre-inception (2018)
    we synthesize from SPY + IEF − BIL financing − 20bp ER."""
    if "NTSX" in returns.columns and returns["NTSX"].notna().sum() > 100:
        return returns["NTSX"].fillna(0)
    spy = returns["SPY"].fillna(0) if "SPY" in returns.columns else pd.Series(0.0, index=returns.index)
    ief = returns["IEF"].fillna(0) if "IEF" in returns.columns else pd.Series(0.0, index=returns.index)
    bil = returns["BIL"].fillna(0)
    return 0.9 * spy + 0.6 * ief - 0.6 * bil - expense_ratio / 252


def synthetic_gde_returns(returns: pd.DataFrame, expense_ratio: float = 0.0020) -> pd.Series:
    """GDE = 90% SPY + 90% gold futures (collateralized). Pre-inception (2022) synth."""
    spy = returns["SPY"].fillna(0) if "SPY" in returns.columns else pd.Series(0.0, index=returns.index)
    gld = returns["GLD"].fillna(0) if "GLD" in returns.columns else pd.Series(0.0, index=returns.index)
    bil = returns["BIL"].fillna(0)
    return 0.9 * spy + 0.9 * gld - 0.9 * bil - expense_ratio / 252


# ─── Wave 8 capital-efficient stacks (WisdomTree NTSI, GraniteShares GDT,
# Return Stacked RSIT, Quantify GOLY) — synthetic constructions from
# underlyings. Real-fund splice happens automatically when the ETF column
# is present in returns. ───

def synthetic_ntsi_returns(returns: pd.DataFrame, expense_ratio: float = 0.0026) -> pd.Series:
    """NTSI = 90% intl-developed equity (VEA ≈ EFA) + 60% Treasury futures (IEF).
    Pre-inception (2022-08) synth."""
    if "NTSI" in returns.columns and returns["NTSI"].notna().sum() > 100:
        return returns["NTSI"].fillna(0)
    efa = returns["EFA"].fillna(0) if "EFA" in returns.columns else pd.Series(0.0, index=returns.index)
    ief = returns["IEF"].fillna(0) if "IEF" in returns.columns else pd.Series(0.0, index=returns.index)
    bil = returns["BIL"].fillna(0)
    return 0.9 * efa + 0.6 * ief - 0.6 * bil - expense_ratio / 252


def synthetic_gdt_returns(returns: pd.DataFrame, expense_ratio: float = 0.0030) -> pd.Series:
    """GDT = 90% short-term TIPS (STIP ≈ TIP at the short end) + 90% gold futures.
    Pre-inception (2024-09) synth uses TIP as a STIP proxy."""
    if "GDT" in returns.columns and returns["GDT"].notna().sum() > 100:
        return returns["GDT"].fillna(0)
    tip = returns["TIP"].fillna(0) if "TIP" in returns.columns else pd.Series(0.0, index=returns.index)
    gld = returns["GLD"].fillna(0) if "GLD" in returns.columns else pd.Series(0.0, index=returns.index)
    bil = returns["BIL"].fillna(0)
    return 0.9 * tip + 0.9 * gld - 0.9 * bil - expense_ratio / 252


def synthetic_rsit_returns(returns: pd.DataFrame, expense_ratio: float = 0.0097) -> pd.Series:
    """RSIT = 100% global stocks (VT) + 100% managed futures (proxied by DBMF).
    Pre-inception (2024) synth uses VT + DBMFSIM-extended DBMF for the MF leg."""
    if "RSIT" in returns.columns and returns["RSIT"].notna().sum() > 100:
        return returns["RSIT"].fillna(0)
    vt = returns["VT"].fillna(0) if "VT" in returns.columns else pd.Series(0.0, index=returns.index)
    dbmf = returns["DBMF"].fillna(0) if "DBMF" in returns.columns else pd.Series(0.0, index=returns.index)
    bil = returns["BIL"].fillna(0)
    return 1.0 * vt + 1.0 * dbmf - 1.0 * bil - expense_ratio / 252


def synthetic_goly_returns(returns: pd.DataFrame, expense_ratio: float = 0.0050) -> pd.Series:
    """GOLY = 50% gold (GLD) + 50% managed futures (CTA, proxied by DBMF) + 100% corp bonds (LQD).
    Pre-inception (2025-04) synth — note 200% total notional."""
    if "GOLY" in returns.columns and returns["GOLY"].notna().sum() > 100:
        return returns["GOLY"].fillna(0)
    gld = returns["GLD"].fillna(0) if "GLD" in returns.columns else pd.Series(0.0, index=returns.index)
    dbmf = returns["DBMF"].fillna(0) if "DBMF" in returns.columns else pd.Series(0.0, index=returns.index)
    lqd = returns["LQD"].fillna(0) if "LQD" in returns.columns else pd.Series(0.0, index=returns.index)
    bil = returns["BIL"].fillna(0)
    return 0.5 * gld + 0.5 * dbmf + 1.0 * lqd - 1.0 * bil - expense_ratio / 252


# ═════════════════════════════════════════════════════════════════════
# GROUP A — HFEA FAMILY VARIANTS (8)
# Sourced from Hedgefundie's original 2019 Boglehead thread + community
# evolution (r/LETFs, r/HFEA modern HFEA discussions 2022-2025).
# ═════════════════════════════════════════════════════════════════════

def bt_hfea_classic_55_45(returns: pd.DataFrame) -> pd.Series:
    """Hedgefundie's original: 55% UPRO / 45% TMF, quarterly rebal. The reference
    benchmark for all HFEA variants. Source: Boglehead's "Hedgefundie's Excellent
    Adventure" thread (2019)."""
    upro = spliced_leveraged_etf(returns, "UPRO")
    tmf = spliced_leveraged_etf(returns, "TMF")
    funds = pd.DataFrame({"UPRO": upro, "TMF": tmf})
    return _drift_and_rebalance(funds, {"UPRO": 0.55, "TMF": 0.45},
                                 _quarterly_rebal_dates(funds.index))


def bt_hfea_40_60(returns: pd.DataFrame) -> pd.Series:
    """Bond-heavy HFEA: 40 UPRO / 60 TMF. Tested by community for lower MaxDD."""
    upro = spliced_leveraged_etf(returns, "UPRO")
    tmf = spliced_leveraged_etf(returns, "TMF")
    funds = pd.DataFrame({"UPRO": upro, "TMF": tmf})
    return _drift_and_rebalance(funds, {"UPRO": 0.40, "TMF": 0.60},
                                 _quarterly_rebal_dates(funds.index))


def bt_hfea_50_30_20_kmlm(returns: pd.DataFrame) -> pd.Series:
    """Modern HFEA: 50/30/20 UPRO/TMF/KMLM. Bond-lighter + managed futures slice.
    Popular 2022+ variant after the bond bear market."""
    upro = spliced_leveraged_etf(returns, "UPRO")
    tmf = spliced_leveraged_etf(returns, "TMF")
    kmlm = spliced_kmlm(returns)
    funds = pd.DataFrame({"UPRO": upro, "TMF": tmf, "KMLM": kmlm})
    return _drift_and_rebalance(funds, {"UPRO": 0.50, "TMF": 0.30, "KMLM": 0.20},
                                 _quarterly_rebal_dates(funds.index))


def bt_hfea_30_40_30_kmlm(returns: pd.DataFrame) -> pd.Series:
    """Conservative modern HFEA: 30/40/30 UPRO/TMF/KMLM. More diversification weight."""
    upro = spliced_leveraged_etf(returns, "UPRO")
    tmf = spliced_leveraged_etf(returns, "TMF")
    kmlm = spliced_kmlm(returns)
    funds = pd.DataFrame({"UPRO": upro, "TMF": tmf, "KMLM": kmlm})
    return _drift_and_rebalance(funds, {"UPRO": 0.30, "TMF": 0.40, "KMLM": 0.30},
                                 _quarterly_rebal_dates(funds.index))


def bt_hfea_no_bonds_60_40(returns: pd.DataFrame) -> pd.Series:
    """Anti-bond era HFEA: 60% UPRO / 40% KMLM. No bonds. Reddit-favored 2022 take."""
    upro = spliced_leveraged_etf(returns, "UPRO")
    kmlm = spliced_kmlm(returns)
    funds = pd.DataFrame({"UPRO": upro, "KMLM": kmlm})
    return _drift_and_rebalance(funds, {"UPRO": 0.60, "KMLM": 0.40},
                                 _quarterly_rebal_dates(funds.index))


def bt_hfea_gold_overlay(returns: pd.DataFrame) -> pd.Series:
    """HFEA + gold overlay: 40/30/30 UPRO/TMF/UGL. Trade some bond duration for
    inflation-hedging 2× gold."""
    upro = spliced_leveraged_etf(returns, "UPRO")
    tmf = spliced_leveraged_etf(returns, "TMF")
    ugl = spliced_leveraged_etf(returns, "UGL")
    funds = pd.DataFrame({"UPRO": upro, "TMF": tmf, "UGL": ugl})
    return _drift_and_rebalance(funds, {"UPRO": 0.40, "TMF": 0.30, "UGL": 0.30},
                                 _quarterly_rebal_dates(funds.index))


def bt_hfea_diversified_4asset(returns: pd.DataFrame) -> pd.Series:
    """4-asset HFEA: 35/25/20/20 UPRO/TMF/KMLM/UGL. Full diversifier set."""
    upro = spliced_leveraged_etf(returns, "UPRO")
    tmf = spliced_leveraged_etf(returns, "TMF")
    kmlm = spliced_kmlm(returns)
    ugl = spliced_leveraged_etf(returns, "UGL")
    funds = pd.DataFrame({"UPRO": upro, "TMF": tmf, "KMLM": kmlm, "UGL": ugl})
    return _drift_and_rebalance(funds,
                                 {"UPRO": 0.35, "TMF": 0.25, "KMLM": 0.20, "UGL": 0.20},
                                 _quarterly_rebal_dates(funds.index))


def bt_leveraged_permanent_portfolio(returns: pd.DataFrame) -> pd.Series:
    """Leveraged Permanent Portfolio (Harry Browne 1980, leveraged variant):
    25/25/25/25 UPRO / UBT / UGL / SHV. 2× notional ≈ 200%."""
    upro = spliced_leveraged_etf(returns, "UPRO")
    ubt = spliced_leveraged_etf(returns, "UBT")
    ugl = spliced_leveraged_etf(returns, "UGL")
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    funds = pd.DataFrame({"UPRO": upro, "UBT": ubt, "UGL": ugl, "SHV": shv})
    return _drift_and_rebalance(funds,
                                 {"UPRO": 0.25, "UBT": 0.25, "UGL": 0.25, "SHV": 0.25},
                                 _quarterly_rebal_dates(funds.index))


# ═════════════════════════════════════════════════════════════════════
# GROUP B — CAPITAL-EFFICIENT STACKS (4)
# WisdomTree NTSX/NTSI family + Return Stacked GDE.
# Source: WisdomTree research papers + Return Stacked Portfolio Solutions.
# ═════════════════════════════════════════════════════════════════════

def bt_ntsx_buyhold(returns: pd.DataFrame) -> pd.Series:
    """NTSX 100% buy-and-hold. 90/60 US stocks/bonds via capital efficiency.
    Source: WisdomTree NTSX (Bessembinder methodology)."""
    return synthetic_ntsx_returns(returns)


def bt_ntsx_kmlm_80_20(returns: pd.DataFrame) -> pd.Series:
    """NTSX + KMLM: 80% capital-efficient 90/60 + 20% managed futures.
    Source: Return Stacked Portfolios research on diversifier overlays."""
    ntsx = synthetic_ntsx_returns(returns)
    kmlm = spliced_kmlm(returns)
    funds = pd.DataFrame({"NTSX": ntsx, "KMLM": kmlm})
    return _drift_and_rebalance(funds, {"NTSX": 0.80, "KMLM": 0.20},
                                 _quarterly_rebal_dates(funds.index))


def bt_ntsx_ntsd_blend(returns: pd.DataFrame) -> pd.Series:
    """50/50 NTSX (US 90/60) + NTSD (US+Intl 90/60). Capital-efficient global diversification."""
    ntsx = synthetic_ntsx_returns(returns)
    ntsd = synthetic_ntsd_returns(returns)
    funds = pd.DataFrame({"NTSX": ntsx, "NTSD": ntsd})
    return _drift_and_rebalance(funds, {"NTSX": 0.50, "NTSD": 0.50},
                                 _quarterly_rebal_dates(funds.index))


def bt_gde_kmlm_70_30(returns: pd.DataFrame) -> pd.Series:
    """GDE + KMLM: 70% 90/90 stocks/gold + 30% managed futures. Stocks/gold/MF triad."""
    gde = synthetic_gde_returns(returns)
    kmlm = spliced_kmlm(returns)
    funds = pd.DataFrame({"GDE": gde, "KMLM": kmlm})
    return _drift_and_rebalance(funds, {"GDE": 0.70, "KMLM": 0.30},
                                 _quarterly_rebal_dates(funds.index))


# ═════════════════════════════════════════════════════════════════════
# GROUP C — RISK PARITY / ALL-WEATHER (4)
# Bridgewater All-Weather, Permanent Portfolio, Golden Butterfly,
# Leveraged All-Weather. Sources: Dalio (Bridgewater), Browne (1980),
# Treadway (Portfolio Charts), Maillard-Roncalli-Teiletche (2010).
# ═════════════════════════════════════════════════════════════════════

def bt_bridgewater_all_weather(returns: pd.DataFrame) -> pd.Series:
    """Bridgewater All-Weather (Dalio): 30% SPY / 40% TLT / 15% IEF / 7.5% GLD / 7.5% DBC.
    Risk-parity-style across 4 economic regimes (growth/inflation/deflation/recession)."""
    w = {"SPY": 0.30, "TLT": 0.40, "IEF": 0.15, "GLD": 0.075, "DBC": 0.075}
    funds = pd.DataFrame({k: returns[k].fillna(0) if k in returns.columns
                          else pd.Series(0.0, index=returns.index) for k in w})
    return _drift_and_rebalance(funds, w, _quarterly_rebal_dates(funds.index))


def bt_permanent_portfolio(returns: pd.DataFrame) -> pd.Series:
    """Harry Browne Permanent Portfolio (1980): 25% stocks / 25% LT bonds / 25% gold /
    25% cash. Equal-weight, quarterly rebalance."""
    w = {"SPY": 0.25, "TLT": 0.25, "GLD": 0.25, "SHV": 0.25}
    funds = pd.DataFrame({k: returns[k].fillna(0) if k in returns.columns
                          else pd.Series(0.0, index=returns.index) for k in w})
    return _drift_and_rebalance(funds, w, _quarterly_rebal_dates(funds.index))


def bt_golden_butterfly(returns: pd.DataFrame) -> pd.Series:
    """Tyler Treadway's Golden Butterfly: 20% each of small-cap (IWM proxy), large-cap (SPY),
    long bonds (TLT), short bonds (SHY), gold (GLD). Source: portfoliocharts.com."""
    w = {"IWM": 0.20, "SPY": 0.20, "TLT": 0.20, "SHY": 0.20, "GLD": 0.20}
    funds = pd.DataFrame({k: returns[k].fillna(0) if k in returns.columns
                          else pd.Series(0.0, index=returns.index) for k in w})
    return _drift_and_rebalance(funds, w, _quarterly_rebal_dates(funds.index))


def bt_leveraged_all_weather(returns: pd.DataFrame) -> pd.Series:
    """Leveraged All-Weather: UPRO/UBT/UGL/DBC at 25% each.
    2× equity + 2× LT bonds + 2× gold + commodities. ~200% notional."""
    upro = spliced_leveraged_etf(returns, "UPRO")
    ubt = spliced_leveraged_etf(returns, "UBT")
    ugl = spliced_leveraged_etf(returns, "UGL")
    dbc = returns["DBC"].fillna(0) if "DBC" in returns.columns else pd.Series(0.0, index=returns.index)
    funds = pd.DataFrame({"UPRO": upro, "UBT": ubt, "UGL": ugl, "DBC": dbc})
    return _drift_and_rebalance(funds,
                                 {"UPRO": 0.25, "UBT": 0.25, "UGL": 0.25, "DBC": 0.25},
                                 _quarterly_rebal_dates(funds.index))


# ═════════════════════════════════════════════════════════════════════
# GROUP D — TACTICAL / TREND-FOLLOWING (6)
# Antonacci GEM strict + Keller VAA/DAA/PAA + Composite DM + Faber 5-asset.
# Sources: Antonacci (2014), Keller-Keuning papers (2016-2017), Faber (2007).
# ═════════════════════════════════════════════════════════════════════

def bt_antonacci_gem_strict(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """Strict Antonacci GEM (Global Equities Momentum, 2014):
    If SPY 12m return > 0:  hold the higher of SPY vs EFA (12m)
    Else:                    hold AGG.
    Monthly rebalance, simple 12m lookback, no leverage."""
    spy = prices["SPY"]
    efa = prices["EFA"]
    agg_ret = returns["AGG"].fillna(0) if "AGG" in returns.columns else pd.Series(0.0, index=returns.index)
    spy_ret = returns["SPY"].fillna(0)
    efa_ret = returns["EFA"].fillna(0) if "EFA" in returns.columns else pd.Series(0.0, index=returns.index)
    rebal_dates = sorted(_monthly_rebal_dates(spy.index))
    position = "AGG"
    out = []
    for i, d in enumerate(spy.index):
        if position == "SPY":
            out.append(float(spy_ret.loc[d]))
        elif position == "EFA":
            out.append(float(efa_ret.loc[d]))
        else:
            out.append(float(agg_ret.loc[d]))
        if d in rebal_dates and i >= 252:
            spy_mom = _trailing_return(spy, d, 252) or -1.0
            efa_mom = _trailing_return(efa, d, 252) or -1.0
            if spy_mom > 0:
                position = "SPY" if spy_mom > efa_mom else "EFA"
            else:
                position = "AGG"
    return pd.Series(out, index=spy.index)


def bt_keller_vaa(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """Keller-Keuning Vigilant Asset Allocation (VAA, 2017):
    Offense universe: SPY, EFA, EEM, AGG (top-1 by 13612W momentum).
    Canary universe: VWO, BND (proxied EEM, AGG). If ANY canary < 0 → all defensive.
    Defensive universe: SHY, IEF, LQD (top-1 by 13612W).
    13612W = (12*r1m + 4*r3m + 2*r6m + r12m)."""
    syms = {"SPY": prices["SPY"],
            "EFA": prices["EFA"] if "EFA" in prices.columns else None,
            "EEM": prices["EEM"] if "EEM" in prices.columns else None,
            "AGG": prices["AGG"] if "AGG" in prices.columns else None}
    def_syms = {"SHY": prices["SHY"] if "SHY" in prices.columns else None,
                "IEF": prices["IEF"] if "IEF" in prices.columns else None,
                "LQD": prices["LQD"] if "LQD" in prices.columns else None}

    def w13612(p, d):
        r1 = _trailing_return(p, d, 21)
        r3 = _trailing_return(p, d, 63)
        r6 = _trailing_return(p, d, 126)
        r12 = _trailing_return(p, d, 252)
        if any(v is None for v in (r1, r3, r6, r12)):
            return None
        return 12*r1 + 4*r3 + 2*r6 + r12

    rebal_dates = sorted(_monthly_rebal_dates(prices.index))
    current = "AGG"
    out = []
    idx = prices.index
    for i, d in enumerate(idx):
        if current in returns.columns:
            out.append(float(returns[current].fillna(0).loc[d]))
        else:
            out.append(0.0)
        if d in rebal_dates and i >= 252:
            # Canary = EEM and AGG (per VAA-G4 spec). All canary must be > 0.
            canary_ok = all(((w13612(syms[s], d) or -1) > 0) for s in ("EEM", "AGG") if syms[s] is not None)
            if canary_ok:
                scores = {s: w13612(p, d) for s, p in syms.items() if p is not None}
                scores = {k: v for k, v in scores.items() if v is not None and v > 0}
                if scores:
                    current = max(scores, key=scores.get)
                else:
                    scores2 = {s: w13612(p, d) for s, p in def_syms.items() if p is not None}
                    scores2 = {k: v for k, v in scores2.items() if v is not None}
                    current = max(scores2, key=scores2.get) if scores2 else "AGG"
            else:
                scores2 = {s: w13612(p, d) for s, p in def_syms.items() if p is not None}
                scores2 = {k: v for k, v in scores2.items() if v is not None}
                current = max(scores2, key=scores2.get) if scores2 else "SHY"
    return pd.Series(out, index=idx)


def bt_keller_daa(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """Keller-Keuning Defensive Asset Allocation (DAA, 2018):
    Top-6 universe: SPY, EFA, EEM, IWM, AGG, GLD, ... (we use 6 available).
    Defensive: count of canary <0 → fraction in defense.
    Simpler variant: hold top-3 if canary all-positive, else all-defensive."""
    risk_syms = {s: prices[s] for s in ("SPY", "EFA", "EEM", "IWM", "AGG", "GLD") if s in prices.columns}
    canary_syms = {s: prices[s] for s in ("EEM", "AGG") if s in prices.columns}
    def_sym = "SHY" if "SHY" in returns.columns else "BIL"

    def mom(p, d):
        r1 = _trailing_return(p, d, 21)
        r3 = _trailing_return(p, d, 63)
        r6 = _trailing_return(p, d, 126)
        r12 = _trailing_return(p, d, 252)
        if any(v is None for v in (r1, r3, r6, r12)):
            return None
        return 12*r1 + 4*r3 + 2*r6 + r12

    rebal_dates = sorted(_monthly_rebal_dates(prices.index))
    positions = {}  # ticker -> weight
    out = []
    for i, d in enumerate(prices.index):
        r = 0.0
        for sym, w in positions.items():
            if sym in returns.columns:
                r += w * float(returns[sym].fillna(0).loc[d])
        out.append(r)
        if d in rebal_dates and i >= 252:
            canary_ok = all(((mom(p, d) or -1) > 0) for p in canary_syms.values())
            if canary_ok:
                scores = {s: mom(p, d) for s, p in risk_syms.items()}
                scores = {k: v for k, v in scores.items() if v is not None and v > 0}
                top3 = sorted(scores.items(), key=lambda kv: -kv[1])[:3]
                if top3:
                    positions = {s: 1.0 / len(top3) for s, _ in top3}
                else:
                    positions = {def_sym: 1.0}
            else:
                positions = {def_sym: 1.0}
    return pd.Series(out, index=prices.index)


def bt_keller_paa(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """Keller Protective Asset Allocation (PAA, 2016):
    Universe: SPY, QQQ, IWM, EFA, EEM, IEF, TLT, GLD, DBC (9 assets).
    Bond fraction = max(0, count_below_sma / N_total) → fraction in IEF.
    Risk fraction = 1 − bond fraction → equal-weighted top-6 momentum."""
    risk_universe = [s for s in ("SPY", "QQQ", "IWM", "EFA", "EEM", "IEF", "TLT", "GLD", "DBC") if s in prices.columns]

    def sma_ratio(p, d, n=252):
        end_pos = p.index.get_indexer([d], method="ffill")[0]
        if end_pos < n:
            return None
        window = p.iloc[end_pos-n+1:end_pos+1]
        if window.isna().any():
            return None
        return float(window.iloc[-1] / window.mean())

    def mom(p, d):
        return _trailing_return(p, d, 252)

    rebal_dates = sorted(_monthly_rebal_dates(prices.index))
    positions = {}
    out = []
    for i, d in enumerate(prices.index):
        r = 0.0
        for sym, w in positions.items():
            if sym in returns.columns:
                r += w * float(returns[sym].fillna(0).loc[d])
        out.append(r)
        if d in rebal_dates and i >= 252:
            sma_signals = []
            for s in risk_universe:
                ratio = sma_ratio(prices[s], d)
                if ratio is not None:
                    sma_signals.append(ratio < 1.0)  # below SMA
            n = len(sma_signals)
            below_count = sum(sma_signals)
            bond_frac = below_count / n if n > 0 else 1.0
            risk_frac = 1.0 - bond_frac
            scores = {s: mom(prices[s], d) for s in risk_universe}
            scores = {k: v for k, v in scores.items() if v is not None and v > 0}
            top6 = sorted(scores.items(), key=lambda kv: -kv[1])[:6]
            positions = {}
            if top6 and risk_frac > 0:
                per = risk_frac / len(top6)
                for s, _ in top6:
                    positions[s] = per
            if bond_frac > 0 and "IEF" in returns.columns:
                positions["IEF"] = bond_frac
    return pd.Series(out, index=prices.index)


def bt_composite_dual_momentum(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """Composite Dual Momentum (multi-lookback Antonacci):
    Universe: SPY, EFA. Score = avg of 3m/6m/9m/12m returns.
    Hold winner if score > 0 (absolute), else AGG. Monthly."""
    rebal_dates = sorted(_monthly_rebal_dates(prices.index))
    def comp_score(p, d):
        scores = [_trailing_return(p, d, n) for n in (63, 126, 189, 252)]
        if any(s is None for s in scores):
            return None
        return sum(scores) / len(scores)
    position = "AGG"
    out = []
    for i, d in enumerate(prices.index):
        ret_col = "SPY" if position == "SPY" else "EFA" if position == "EFA" else "AGG"
        if ret_col in returns.columns:
            out.append(float(returns[ret_col].fillna(0).loc[d]))
        else:
            out.append(0.0)
        if d in rebal_dates and i >= 252:
            spy_s = comp_score(prices["SPY"], d)
            efa_s = comp_score(prices["EFA"], d) if "EFA" in prices.columns else None
            best_s = max((s for s in (spy_s, efa_s) if s is not None), default=-1)
            if best_s and best_s > 0:
                position = "SPY" if spy_s == best_s else "EFA"
            else:
                position = "AGG"
    return pd.Series(out, index=prices.index)


def bt_faber_gtaa_5asset(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """Faber 5-asset GTAA (Faber 2007): SPY, EFA, TLT, GLD, DBC. Each held if
    above its own 200-day SMA, else cash for that slot. Monthly reset."""
    universe = ["SPY", "EFA", "TLT", "GLD", "DBC"]
    universe = [s for s in universe if s in prices.columns]
    rebal_dates = sorted(_monthly_rebal_dates(prices.index))
    holdings = {s: True for s in universe}  # start all-in
    bil = returns["BIL"].fillna(0) if "BIL" in returns.columns else pd.Series(0.0, index=returns.index)
    n = len(universe) if universe else 1
    out = []
    for i, d in enumerate(prices.index):
        r = 0.0
        for s in universe:
            slot = returns[s].fillna(0).loc[d] if holdings[s] else bil.loc[d]
            r += slot / n
        out.append(r)
        if d in rebal_dates and i >= 200:
            for s in universe:
                end_pos = prices.index.get_indexer([d], method="ffill")[0]
                sma = prices[s].iloc[end_pos-199:end_pos+1].mean()
                holdings[s] = prices[s].iloc[end_pos] > sma
    return pd.Series(out, index=prices.index)


# ═════════════════════════════════════════════════════════════════════
# GROUP E — HFEA RISK-MANAGED VARIANTS (4)
# 200-SMA gating, DD-30 stop, vol-target, monthly-rebal comparison.
# ═════════════════════════════════════════════════════════════════════

def bt_hfea_sma_gated(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """HFEA + 200-SMA gate: hold 45/25/30 UPRO/TMF/KMLM when SPY > 200-SMA × 1.01,
    else 100% SHV. Daily signal eval."""
    upro = spliced_leveraged_etf(returns, "UPRO")
    tmf = spliced_leveraged_etf(returns, "TMF")
    kmlm = spliced_kmlm(returns)
    spy = prices["SPY"]
    sma = spy.rolling(200).mean()
    bullish = spy > sma * 1.01
    bearish = spy < sma * 0.99
    state = pd.Series(False, index=spy.index)
    cur = False
    for d in spy.index:
        if bullish.loc[d]:
            cur = True
        elif bearish.loc[d]:
            cur = False
        state.loc[d] = cur
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    hfea_ret = 0.45 * upro + 0.25 * tmf + 0.30 * kmlm
    return state.shift(1).fillna(False).astype(float) * hfea_ret + (1 - state.shift(1).fillna(False).astype(float)) * shv


def bt_hfea_dd30_stop(returns: pd.DataFrame) -> pd.Series:
    """HFEA with -30% trailing peak-NAV drawdown stop. Exit to SHV when DD breached;
    re-enter on the next quarterly rebal date."""
    upro = spliced_leveraged_etf(returns, "UPRO")
    tmf = spliced_leveraged_etf(returns, "TMF")
    kmlm = spliced_kmlm(returns)
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    funds = pd.DataFrame({"UPRO": upro, "TMF": tmf, "KMLM": kmlm})
    target = {"UPRO": 0.45, "TMF": 0.25, "KMLM": 0.30}
    rebal_dates = _quarterly_rebal_dates(funds.index)
    weights = np.array([target[a] for a in funds.columns])
    out = []
    nav = 1.0
    peak = 1.0
    in_cash = False
    for d, row in funds.iterrows():
        if in_cash:
            r = float(shv.loc[d])
            out.append(r)
            nav *= (1 + r)
            peak = max(peak, nav)
            if d in rebal_dates:
                in_cash = False
                weights = np.array([target[a] for a in funds.columns])
                peak = nav
        else:
            r = float(np.dot(weights, row.values))
            out.append(r)
            nav *= (1 + r)
            peak = max(peak, nav)
            dd = (nav - peak) / peak
            if dd < -0.30:
                in_cash = True
                continue
            weights = weights * (1 + row.values)
            s = weights.sum()
            if s > 0:
                weights = weights / s
            if d in rebal_dates:
                weights = np.array([target[a] for a in funds.columns])
    return pd.Series(out, index=funds.index)


def bt_hfea_vol_target_25(returns: pd.DataFrame) -> pd.Series:
    """HFEA + 25% annualized vol target. Scale position size by realized 60-day vol;
    excess parks in SHV."""
    upro = spliced_leveraged_etf(returns, "UPRO")
    tmf = spliced_leveraged_etf(returns, "TMF")
    kmlm = spliced_kmlm(returns)
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    hfea_ret = 0.45 * upro + 0.25 * tmf + 0.30 * kmlm
    rolling_vol = hfea_ret.rolling(60).std() * np.sqrt(252)
    scale = (0.25 / rolling_vol).clip(0, 1.0).shift(1).fillna(0.5)
    return scale * hfea_ret + (1 - scale) * shv


def bt_hfea_monthly_rebal(returns: pd.DataFrame) -> pd.Series:
    """HFEA with MONTHLY rebal instead of quarterly. Tests whether rebal frequency
    materially affects the strategy. Same 45/25/30 weights."""
    upro = spliced_leveraged_etf(returns, "UPRO")
    tmf = spliced_leveraged_etf(returns, "TMF")
    kmlm = spliced_kmlm(returns)
    funds = pd.DataFrame({"UPRO": upro, "TMF": tmf, "KMLM": kmlm})
    return _drift_and_rebalance(funds, {"UPRO": 0.45, "TMF": 0.25, "KMLM": 0.30},
                                 _monthly_rebal_dates(funds.index))


# ═════════════════════════════════════════════════════════════════════
# GROUP F — TQQQ VARIANTS (3)
# Nasdaq-heavy leveraged sleeves. Sources: 9-Sig + Kelly community,
# r/LETFs TQQQ-DCA discussions.
# ═════════════════════════════════════════════════════════════════════

def bt_tqqq_tmf_kmlm(returns: pd.DataFrame) -> pd.Series:
    """Nasdaq-HFEA: 45/25/30 TQQQ/TMF/KMLM. More aggressive than HFEA on equity leg."""
    tqqq = spliced_leveraged_etf(returns, "TQQQ")
    tmf = spliced_leveraged_etf(returns, "TMF")
    kmlm = spliced_kmlm(returns)
    funds = pd.DataFrame({"TQQQ": tqqq, "TMF": tmf, "KMLM": kmlm})
    return _drift_and_rebalance(funds, {"TQQQ": 0.45, "TMF": 0.25, "KMLM": 0.30},
                                 _quarterly_rebal_dates(funds.index))


def bt_tqqq_ubt_70_30(returns: pd.DataFrame) -> pd.Series:
    """TQQQ + UBT 70/30. Nasdaq + 2× LT bonds. Less bond decay than TMF."""
    tqqq = spliced_leveraged_etf(returns, "TQQQ")
    ubt = spliced_leveraged_etf(returns, "UBT")
    funds = pd.DataFrame({"TQQQ": tqqq, "UBT": ubt})
    return _drift_and_rebalance(funds, {"TQQQ": 0.70, "UBT": 0.30},
                                 _quarterly_rebal_dates(funds.index))


def bt_tqqq_sma_gated(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """TQQQ with 200-SMA gate on QQQ. Hold TQQQ when QQQ > 200SMA × 1.01, else SHV."""
    tqqq = spliced_leveraged_etf(returns, "TQQQ")
    qqq = prices["QQQ"]
    sma = qqq.rolling(200).mean()
    bullish = qqq > sma * 1.01
    bearish = qqq < sma * 0.99
    state = pd.Series(False, index=qqq.index)
    cur = False
    for d in qqq.index:
        if bullish.loc[d]:
            cur = True
        elif bearish.loc[d]:
            cur = False
        state.loc[d] = cur
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    return state.shift(1).fillna(False).astype(float) * tqqq + (1 - state.shift(1).fillna(False).astype(float)) * shv


# ═══════════════════════════════════════════════════════════════════════
# RESEARCH WAVE 3 — 21 WLDU-BASED STRATEGIES
# Goal: find better implementations of WLDU (2× MSCI World) than Regime World.
# Sources: Hedgefundie modern HFEA-on-global variants, Antonacci cross-asset DM,
# Butler-Philbrick-Gordillo AAA, Faber GTAA. All use spliced_wldu (URTHSIM-extended).
# ═══════════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════════
# GROUP G — Static HFEA-Global (5 strategies)
# Apply HFEA-style risk-parity weightings using WLDU as the equity sleeve.
# ═════════════════════════════════════════════════════════════════════

def bt_global_hfea_classic(returns: pd.DataFrame) -> pd.Series:
    """Global HFEA Classic: 55% WLDU / 45% TMF. Hedgefundie original applied globally."""
    wldu = spliced_wldu(returns)
    tmf = spliced_leveraged_etf(returns, "TMF")
    funds = pd.DataFrame({"WLDU": wldu, "TMF": tmf})
    return _drift_and_rebalance(funds, {"WLDU": 0.55, "TMF": 0.45},
                                 _quarterly_rebal_dates(funds.index))


def bt_global_hfea_modern(returns: pd.DataFrame) -> pd.Series:
    """Global HFEA Modern: 45/25/30 WLDU/TMF/KMLM. Direct mirror of production HFEA but with WLDU instead of UPRO."""
    wldu = spliced_wldu(returns)
    tmf = spliced_leveraged_etf(returns, "TMF")
    kmlm = spliced_kmlm(returns)
    funds = pd.DataFrame({"WLDU": wldu, "TMF": tmf, "KMLM": kmlm})
    return _drift_and_rebalance(funds, {"WLDU": 0.45, "TMF": 0.25, "KMLM": 0.30},
                                 _quarterly_rebal_dates(funds.index))


def bt_global_hfea_bond_light(returns: pd.DataFrame) -> pd.Series:
    """Bond-light Global HFEA: 50/30/20 WLDU/TMF/KMLM."""
    wldu = spliced_wldu(returns)
    tmf = spliced_leveraged_etf(returns, "TMF")
    kmlm = spliced_kmlm(returns)
    funds = pd.DataFrame({"WLDU": wldu, "TMF": tmf, "KMLM": kmlm})
    return _drift_and_rebalance(funds, {"WLDU": 0.50, "TMF": 0.30, "KMLM": 0.20},
                                 _quarterly_rebal_dates(funds.index))


def bt_global_hfea_ubt(returns: pd.DataFrame) -> pd.Series:
    """Global HFEA with 2× bonds (UBT) instead of 3× TMF: 50/25/25 WLDU/UBT/KMLM."""
    wldu = spliced_wldu(returns)
    ubt = spliced_leveraged_etf(returns, "UBT")
    kmlm = spliced_kmlm(returns)
    funds = pd.DataFrame({"WLDU": wldu, "UBT": ubt, "KMLM": kmlm})
    return _drift_and_rebalance(funds, {"WLDU": 0.50, "UBT": 0.25, "KMLM": 0.25},
                                 _quarterly_rebal_dates(funds.index))


def bt_global_hfea_gold(returns: pd.DataFrame) -> pd.Series:
    """Global HFEA with Gold instead of KMLM: 45/25/30 WLDU/TMF/UGL."""
    wldu = spliced_wldu(returns)
    tmf = spliced_leveraged_etf(returns, "TMF")
    ugl = spliced_leveraged_etf(returns, "UGL")
    funds = pd.DataFrame({"WLDU": wldu, "TMF": tmf, "UGL": ugl})
    return _drift_and_rebalance(funds, {"WLDU": 0.45, "TMF": 0.25, "UGL": 0.30},
                                 _quarterly_rebal_dates(funds.index))


# ═════════════════════════════════════════════════════════════════════
# GROUP H — Trend-managed WLDU (5 strategies)
# Apply trend filters (SMA / DD-stop / vol-target) to WLDU exposure.
# ═════════════════════════════════════════════════════════════════════

def bt_wldu_sma200_gate(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """WLDU when URTH > 200-SMA × 1.01, else SHV. Daily signal."""
    wldu = spliced_wldu(returns)
    urth = spliced_urth(returns)
    # Build URTH price level for SMA calculation
    urth_level = (1 + urth.fillna(0)).cumprod() * 100.0
    sma = urth_level.rolling(200).mean()
    bullish = urth_level > sma * 1.01
    bearish = urth_level < sma * 0.99
    state = pd.Series(False, index=urth_level.index)
    cur = False
    for d in urth_level.index:
        if bullish.loc[d]:
            cur = True
        elif bearish.loc[d]:
            cur = False
        state.loc[d] = cur
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    return state.shift(1).fillna(False).astype(float) * wldu + (1 - state.shift(1).fillna(False).astype(float)) * shv


def bt_wldu_sma255_gate(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """WLDU + URTH 255-day SMA gate. Longer SMA (Faber suggested for global indices)."""
    wldu = spliced_wldu(returns)
    urth = spliced_urth(returns)
    urth_level = (1 + urth.fillna(0)).cumprod() * 100.0
    sma = urth_level.rolling(255).mean()
    bullish = urth_level > sma * 1.01
    bearish = urth_level < sma * 0.99
    state = pd.Series(False, index=urth_level.index)
    cur = False
    for d in urth_level.index:
        if bullish.loc[d]:
            cur = True
        elif bearish.loc[d]:
            cur = False
        state.loc[d] = cur
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    return state.shift(1).fillna(False).astype(float) * wldu + (1 - state.shift(1).fillna(False).astype(float)) * shv


def bt_wldu_dd30_stop(returns: pd.DataFrame) -> pd.Series:
    """WLDU with trailing -30% peak-NAV drawdown stop. Re-enter on monthly rebalance."""
    wldu = spliced_wldu(returns)
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    rebal_dates = _monthly_rebal_dates(wldu.index)
    out = []
    nav = 1.0
    peak = 1.0
    in_cash = False
    for d, r_wldu, r_shv in zip(wldu.index, wldu, shv):
        if in_cash:
            r = float(r_shv)
            nav *= (1 + r)
            peak = max(peak, nav)
            out.append(r)
            if d in rebal_dates:
                in_cash = False
                peak = nav
        else:
            r = float(r_wldu)
            nav *= (1 + r)
            peak = max(peak, nav)
            out.append(r)
            dd = (nav - peak) / peak if peak > 0 else 0
            if dd < -0.30:
                in_cash = True
    return pd.Series(out, index=wldu.index)


def bt_wldu_vol_target_25(returns: pd.DataFrame) -> pd.Series:
    """WLDU with 25% annualized vol target. Scale exposure by realized 60-day vol; remainder in SHV."""
    wldu = spliced_wldu(returns)
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    rolling_vol = wldu.rolling(60).std() * np.sqrt(252)
    scale = (0.25 / rolling_vol).clip(0, 1.0).shift(1).fillna(0.5)
    return scale * wldu + (1 - scale) * shv


def bt_wldu_vol_target_20(returns: pd.DataFrame) -> pd.Series:
    """WLDU with 20% annualized vol target. Tighter risk control."""
    wldu = spliced_wldu(returns)
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    rolling_vol = wldu.rolling(60).std() * np.sqrt(252)
    scale = (0.20 / rolling_vol).clip(0, 1.0).shift(1).fillna(0.5)
    return scale * wldu + (1 - scale) * shv


# ═════════════════════════════════════════════════════════════════════
# GROUP I — Tactical AAA-Global (4 strategies)
# Adaptive Asset Allocation on global universe with WLDU as the equity leg.
# ═════════════════════════════════════════════════════════════════════

def bt_wldu_aaa_top2_dd25_vol20(returns: pd.DataFrame, prices: pd.DataFrame,
                                  top_n: int = 2, lookback: int = 126,
                                  dd_threshold: float = 0.25, target_vol: float = 0.20,
                                  vol_window: int = 60) -> pd.Series:
    """WLDU + UBT/UGL/DBC AAA top-2 (Bronze-style with WLDU instead of NTSD).
    Top-2 by 6m momentum, inverse-vol weighted, DD25 + vol20."""
    ret_series = {
        "WLDU": spliced_wldu(returns),
        "UBT":  _lev_etf_return(returns, "UBT"),
        "UGL":  _lev_etf_return(returns, "UGL"),
        "DBC":  _lev_etf_return(returns, "DBC"),
    }
    px_series = {k: (1 + v).cumprod() * 100.0 for k, v in ret_series.items()}
    cash = "SHV"
    available = list(ret_series.keys())
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)

    weights = {a: 0.0 for a in available}
    cash_weight = 1.0
    peak_nav = 1.0
    nav = 1.0
    out = []
    for d in returns.index:
        risk_r = sum(weights[a] * ret_series[a].loc[d] for a in available)
        r = risk_r + cash_weight * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and (1 - cash_weight) > 0:
            weights = {a: 0.0 for a in available}; cash_weight = 1.0; peak_nav = nav
        for a in available:
            weights[a] *= (1 + ret_series[a].loc[d])
        cash_weight *= (1 + cash_ret.loc[d])
        s = sum(weights.values()) + cash_weight
        if s > 0:
            for k in weights:
                weights[k] /= s
            cash_weight /= s

        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {a: _trailing_return(px_series[a], d, lookback) for a in available}
            scores = {k: v for k, v in scores.items() if v is not None}
            if not scores:
                continue
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            picks = [a for a, s in ranked[:top_n] if s > 0]
            if not picks:
                weights = {a: 0.0 for a in available}; cash_weight = 1.0
                continue
            invvols = {}
            for a in picks:
                v = _trailing_vol(ret_series[a], d, vol_window)
                if v is not None:
                    invvols[a] = 1.0 / v
            if not invvols:
                w_each = 1.0 / len(picks)
                raw = {a: (w_each if a in picks else 0.0) for a in available}
            else:
                total_iv = sum(invvols.values())
                raw = {a: 0.0 for a in available}
                for a, iv in invvols.items():
                    raw[a] = iv / total_iv
            est_vol = sum(w * (_trailing_vol(ret_series[a], d, vol_window) or target_vol)
                          for a, w in raw.items() if w > 0)
            scale = min(1.0, target_vol / est_vol) if est_vol > 0 else 1.0
            weights = {a: w * scale for a, w in raw.items()}
            cash_weight = 1.0 - sum(weights.values())
            peak_nav = nav
    return pd.Series(out, index=returns.index)


def bt_wldu_aaa_top3_dd30_vol25(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """WLDU AAA top-3 universe: WLDU/UBT/UST/UGL/DBC. Top-3 by 6m mom, DD30 + vol25."""
    ret_series = {
        "WLDU": spliced_wldu(returns),
        "UBT":  _lev_etf_return(returns, "UBT"),
        "UST":  _lev_etf_return(returns, "UST"),
        "UGL":  _lev_etf_return(returns, "UGL"),
        "DBC":  _lev_etf_return(returns, "DBC"),
    }
    px_series = {k: (1 + v).cumprod() * 100.0 for k, v in ret_series.items()}
    cash = "SHV"
    available = list(ret_series.keys())
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    cash_ret = returns[cash].fillna(0) if cash in returns.columns else pd.Series(0.0, index=returns.index)
    top_n = 3; lookback = 126; dd_threshold = 0.30; target_vol = 0.25; vol_window = 60

    weights = {a: 0.0 for a in available}
    cash_weight = 1.0
    peak_nav = 1.0
    nav = 1.0
    out = []
    for d in returns.index:
        risk_r = sum(weights[a] * ret_series[a].loc[d] for a in available)
        r = risk_r + cash_weight * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and (1 - cash_weight) > 0:
            weights = {a: 0.0 for a in available}; cash_weight = 1.0; peak_nav = nav
        for a in available:
            weights[a] *= (1 + ret_series[a].loc[d])
        cash_weight *= (1 + cash_ret.loc[d])
        s = sum(weights.values()) + cash_weight
        if s > 0:
            for k in weights: weights[k] /= s
            cash_weight /= s
        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {a: _trailing_return(px_series[a], d, lookback) for a in available}
            scores = {k: v for k, v in scores.items() if v is not None}
            if not scores: continue
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            picks = [a for a, s in ranked[:top_n] if s > 0]
            if not picks:
                weights = {a: 0.0 for a in available}; cash_weight = 1.0; continue
            invvols = {}
            for a in picks:
                v = _trailing_vol(ret_series[a], d, vol_window)
                if v is not None: invvols[a] = 1.0 / v
            if not invvols:
                w_each = 1.0 / len(picks)
                raw = {a: (w_each if a in picks else 0.0) for a in available}
            else:
                total_iv = sum(invvols.values())
                raw = {a: 0.0 for a in available}
                for a, iv in invvols.items(): raw[a] = iv / total_iv
            est_vol = sum(w * (_trailing_vol(ret_series[a], d, vol_window) or target_vol)
                          for a, w in raw.items() if w > 0)
            scale = min(1.0, target_vol / est_vol) if est_vol > 0 else 1.0
            weights = {a: w * scale for a, w in raw.items()}
            cash_weight = 1.0 - sum(weights.values())
            peak_nav = nav
    return pd.Series(out, index=returns.index)


def bt_wldu_cross_asset_dm(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """Single-best rotation across {WLDU, UBT, UGL, DBC} by 12m momentum (Antonacci cross-asset DM)."""
    ret_series = {
        "WLDU": spliced_wldu(returns),
        "UBT":  _lev_etf_return(returns, "UBT"),
        "UGL":  _lev_etf_return(returns, "UGL"),
        "DBC":  _lev_etf_return(returns, "DBC"),
    }
    px_series = {k: (1 + v).cumprod() * 100.0 for k, v in ret_series.items()}
    cash_ret = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    current = None  # holding cash initially
    out = []
    for d in returns.index:
        if current is None:
            r = float(cash_ret.loc[d])
        else:
            r = float(ret_series[current].loc[d])
        out.append(r)
        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= 252:
            scores = {a: _trailing_return(px_series[a], d, 252) for a in ret_series}
            scores = {k: v for k, v in scores.items() if v is not None and v > 0}
            current = max(scores, key=scores.get) if scores else None
    return pd.Series(out, index=returns.index)


def bt_wldu_core_satellite(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """60% WLDU core + 40% rotating top-1 of {UBT, UGL, DBC}. Core never sold."""
    wldu = spliced_wldu(returns)
    ret_series = {
        "UBT":  _lev_etf_return(returns, "UBT"),
        "UGL":  _lev_etf_return(returns, "UGL"),
        "DBC":  _lev_etf_return(returns, "DBC"),
    }
    px_series = {k: (1 + v).cumprod() * 100.0 for k, v in ret_series.items()}
    rebal_dates = sorted(_monthly_rebal_dates(returns.index))
    satellite = None
    cash_ret = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    out = []
    for d in returns.index:
        core_r = 0.6 * float(wldu.loc[d])
        sat_r = 0.4 * (float(ret_series[satellite].loc[d]) if satellite else float(cash_ret.loc[d]))
        out.append(core_r + sat_r)
        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= 126:
            scores = {a: _trailing_return(px_series[a], d, 126) for a in ret_series}
            scores = {k: v for k, v in scores.items() if v is not None and v > 0}
            satellite = max(scores, key=scores.get) if scores else None
    return pd.Series(out, index=returns.index)


# ═════════════════════════════════════════════════════════════════════
# GROUP J — WLDU DM rotation (3 strategies)
# ═════════════════════════════════════════════════════════════════════

def bt_wldu_qld_efo_dm(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """DM best-of-3 with WLDU/QLD/EFO (Global / Nasdaq / Intl). All 2× leverage."""
    return _bt_dm_2x_multi_asset(
        returns, prices,
        candidates=[("URTH", "WLDU"), ("QQQ", "QLD"), ("EFA", "EFO")],
        dd_threshold=0.30, target_vol=0.25,
    )


def bt_wldu_spuu_qld_dm(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """DM best-of-3 with WLDU/SPUU/QLD (Global / US / Nasdaq). Mostly equity exposure mix."""
    return _bt_dm_2x_multi_asset(
        returns, prices,
        candidates=[("URTH", "WLDU"), ("SPY", "SPUU"), ("QQQ", "QLD")],
        dd_threshold=0.30, target_vol=0.25,
    )


def bt_wldu_spuu_qld_efo_dm(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """DM best-of-4: WLDU/SPUU/QLD/EFO. Adds WLDU to production DM universe."""
    return _bt_dm_2x_multi_asset(
        returns, prices,
        candidates=[("URTH", "WLDU"), ("SPY", "SPUU"), ("QQQ", "QLD"), ("EFA", "EFO")],
        dd_threshold=0.30, target_vol=0.25,
    )


# ═════════════════════════════════════════════════════════════════════
# GROUP K — Diversified static + hybrid (4 strategies)
# ═════════════════════════════════════════════════════════════════════

def bt_wldu_diversified_4asset(returns: pd.DataFrame) -> pd.Series:
    """4-asset Global HFEA: 35/25/20/20 WLDU/TMF/KMLM/UGL. Full diversifier set."""
    wldu = spliced_wldu(returns)
    tmf = spliced_leveraged_etf(returns, "TMF")
    kmlm = spliced_kmlm(returns)
    ugl = spliced_leveraged_etf(returns, "UGL")
    funds = pd.DataFrame({"WLDU": wldu, "TMF": tmf, "KMLM": kmlm, "UGL": ugl})
    return _drift_and_rebalance(funds, {"WLDU": 0.35, "TMF": 0.25, "KMLM": 0.20, "UGL": 0.20},
                                 _quarterly_rebal_dates(funds.index))


def bt_wldu_permanent_portfolio(returns: pd.DataFrame) -> pd.Series:
    """Leveraged Global Permanent Portfolio: 25/25/25/25 WLDU/UBT/UGL/SHV."""
    wldu = spliced_wldu(returns)
    ubt = spliced_leveraged_etf(returns, "UBT")
    ugl = spliced_leveraged_etf(returns, "UGL")
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    funds = pd.DataFrame({"WLDU": wldu, "UBT": ubt, "UGL": ugl, "SHV": shv})
    return _drift_and_rebalance(funds, {"WLDU": 0.25, "UBT": 0.25, "UGL": 0.25, "SHV": 0.25},
                                 _quarterly_rebal_dates(funds.index))


def bt_wldu_upro_hybrid(returns: pd.DataFrame) -> pd.Series:
    """Hybrid Global+US HFEA: 30/15/25/30 WLDU/UPRO/TMF/KMLM."""
    wldu = spliced_wldu(returns)
    upro = spliced_leveraged_etf(returns, "UPRO")
    tmf = spliced_leveraged_etf(returns, "TMF")
    kmlm = spliced_kmlm(returns)
    funds = pd.DataFrame({"WLDU": wldu, "UPRO": upro, "TMF": tmf, "KMLM": kmlm})
    return _drift_and_rebalance(funds,
                                 {"WLDU": 0.30, "UPRO": 0.15, "TMF": 0.25, "KMLM": 0.30},
                                 _quarterly_rebal_dates(funds.index))


def bt_wldu_all_weather(returns: pd.DataFrame) -> pd.Series:
    """Leveraged Global All-Weather: 30 WLDU / 40 UBT / 15 UST / 7.5 UGL / 7.5 DBC."""
    wldu = spliced_wldu(returns)
    ubt = spliced_leveraged_etf(returns, "UBT")
    ust = spliced_leveraged_etf(returns, "UST")
    ugl = spliced_leveraged_etf(returns, "UGL")
    dbc_ret = returns["DBC"].fillna(0) if "DBC" in returns.columns else pd.Series(0.0, index=returns.index)
    funds = pd.DataFrame({"WLDU": wldu, "UBT": ubt, "UST": ust, "UGL": ugl, "DBC": dbc_ret})
    return _drift_and_rebalance(funds,
                                 {"WLDU": 0.30, "UBT": 0.40, "UST": 0.15, "UGL": 0.075, "DBC": 0.075},
                                 _quarterly_rebal_dates(funds.index))


# ═════════════════════════════════════════════════════════════════════
# RESEARCH WAVE 4 — 3 grounded WLDU strategies (May 2026)
#
# Backed by:
#   ReSolve/Return Stacked "Filling the Gap" (managed-futures stack research)
#   AQR Ilmanen-Maloney 2025 (US-vs-intl forward returns)
#   Antonacci GEM 2014 + optimalmomentum.com extended backtest
#   GMO 7-year forecasts (EM-value forward returns)
#   Cambria GVAL investment case
# ═════════════════════════════════════════════════════════════════════


def bt_wldu_kmlm_voltarget(returns: pd.DataFrame,
                            target_vol: float = 0.12,
                            vol_window: int = 60) -> pd.Series:
    """R1 — WLDU 60% + KMLM 40%, vol-targeted to 12% annualized.

    Pure intl-tilted equity (WLDU) + crisis-alpha managed futures (KMLM).
    KMLM correlation to MSCI World <0.2 → diversification works.
    No bond duplication (HFEA covers bonds elsewhere). Vol-target floors
    leverage when blend is too volatile; remainder parks in SHV.

    Source: ReSolve/Return Stacked "Filling the Gap" — optimal MF weight
    25-40% of portfolio; 40% chosen here as upper bound for diversification."""
    wldu = spliced_wldu(returns)
    kmlm = spliced_kmlm(returns)
    base = 0.60 * wldu + 0.40 * kmlm
    rolling_vol = base.rolling(vol_window).std() * np.sqrt(252)
    scale = (target_vol / rolling_vol).clip(0, 1.0).shift(1).fillna(0.5)
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    return scale * base + (1 - scale) * shv


def bt_wldu_gem_rotation(returns: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """R3 — Antonacci GEM-style US/Intl relative momentum.

    Each month:
      If max(SPY 12m TR, EFA 12m TR) <= 0:           hold cash (SHV)
      Else if EFA 12m TR > SPY 12m TR (intl wins):   hold WLDU
      Else (US wins):                                hold SPUU

    KEY: signal uses underlying SPY/EFA (1× prices), not the leveraged ETFs —
    avoids leverage-decay noise corrupting the momentum signal. The position
    is held in the 2× ETF (WLDU or SPUU) for amplified exposure.

    Source: Antonacci 2014 *Dual Momentum*; optimalmomentum.com extended
    backtest of GEM across 50+ years."""
    wldu = spliced_wldu(returns)
    spuu = spliced_leveraged_etf(returns, "SPUU")
    cash_ret = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    spy = prices["SPY"]
    efa = prices["EFA"]
    rebal_dates = sorted(_monthly_rebal_dates(prices.index))
    current = None  # cash
    out = []
    for i, d in enumerate(prices.index):
        if current == "WLDU":
            r = float(wldu.loc[d])
        elif current == "SPUU":
            r = float(spuu.loc[d])
        else:
            r = float(cash_ret.loc[d])
        out.append(r)
        if d in rebal_dates and i >= 252:
            spy_mom = _trailing_return(spy, d, 252)
            efa_mom = _trailing_return(efa, d, 252)
            if spy_mom is None or efa_mom is None:
                continue
            if max(spy_mom, efa_mom) <= 0:
                current = None  # cash
            elif efa_mom > spy_mom:
                current = "WLDU"
            else:
                current = "SPUU"
    return pd.Series(out, index=prices.index)


def bt_wldu_em_stack(returns: pd.DataFrame) -> pd.Series:
    """R6 — WLDU 70% + EET 30% (2× EM). Quarterly rebal.

    MSCI World (and thus WLDU) explicitly EXCLUDES emerging markets — adding
    EET genuinely extends geographic coverage rather than duplicating exposure.
    Both legs are 2× leveraged, matching WLDU's risk profile.

    Source: GMO 7-year forecasts (EM value = best forward real returns of any
    asset class); Lazard / VanEck / JPM AM 2025 outlooks (EM CAPE ~12-15 vs
    US ~30+ creates the strongest forward case in 20 years); MSCI: 'EM in a
    World Beyond US Exceptionalism'."""
    wldu = spliced_wldu(returns)
    eet = _lev_etf_return(returns, "EET")
    funds = pd.DataFrame({"WLDU": wldu, "EET": eet})
    return _drift_and_rebalance(funds, {"WLDU": 0.70, "EET": 0.30},
                                 _quarterly_rebal_dates(funds.index))


def bt_wldu_kmlm_static(returns: pd.DataFrame) -> pd.Series:
    """R1b — WLDU 60% + KMLM 40% static, NO vol-target. Quarterly rebal.
    Removes the 12% vol-target that strangled R1."""
    wldu = spliced_wldu(returns)
    kmlm = spliced_kmlm(returns)
    funds = pd.DataFrame({"WLDU": wldu, "KMLM": kmlm})
    return _drift_and_rebalance(funds, {"WLDU": 0.60, "KMLM": 0.40},
                                 _quarterly_rebal_dates(funds.index))


def bt_wldu_kmlm_voltarget18(returns: pd.DataFrame,
                              target_vol: float = 0.18,
                              vol_window: int = 60) -> pd.Series:
    """R1c — WLDU 60% + KMLM 40% with looser 18% vol target."""
    wldu = spliced_wldu(returns)
    kmlm = spliced_kmlm(returns)
    base = 0.60 * wldu + 0.40 * kmlm
    rolling_vol = base.rolling(vol_window).std() * np.sqrt(252)
    scale = (target_vol / rolling_vol).clip(0, 1.0).shift(1).fillna(0.9)
    shv = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    return scale * base + (1 - scale) * shv


# ═══════════════════════════════════════════════════════════════════════
# WAVE 5 — WLDU candidates: revival + alternative diversifiers
# Constraints: no SPUU (deployed in DM), no HFEA-Global structure (user
# already runs it elsewhere), no standalone 200/255-SMA gate (user runs
# WLDU+255-SMA elsewhere). C1 is the revival of WLDU AAA top-2 which
# scored 13.91% / 0.67 / -28.8% but was bundled-rejected.
# ═══════════════════════════════════════════════════════════════════════


def _wldu_series_for(returns: pd.DataFrame, ticker: str) -> pd.Series:
    if ticker == "WLDU":  return spliced_wldu(returns)
    if ticker == "KMLM":  return spliced_kmlm(returns)
    if ticker == "WTIP":  return spliced_rssb_wtip(returns)[1]
    return _lev_etf_return(returns, ticker)


def _wldu_static_blend(returns: pd.DataFrame, weights: dict) -> pd.Series:
    """Static blend, quarterly drift+rebalance."""
    series = {t: _wldu_series_for(returns, t) for t in weights}
    funds = pd.DataFrame(series)
    return _drift_and_rebalance(funds, weights, _quarterly_rebal_dates(funds.index))


def bt_w5_c2_wldu_ubt_kmlm(returns: pd.DataFrame) -> pd.Series:
    """C2 — WLDU 50% + UBT 30% + KMLM 20% (intl equity + duration + crisis-alpha)."""
    return _wldu_static_blend(returns, {"WLDU": 0.50, "UBT": 0.30, "KMLM": 0.20})


def bt_w5_c3_wldu_kmlm_ugl(returns: pd.DataFrame) -> pd.Series:
    """C3 — WLDU 50% + KMLM 30% + UGL 20% (no bonds: intl equity + MF + gold)."""
    return _wldu_static_blend(returns, {"WLDU": 0.50, "KMLM": 0.30, "UGL": 0.20})


def bt_w5_c4_wldu_ubt_ugl_kmlm(returns: pd.DataFrame) -> pd.Series:
    """C4 — WLDU 40% + UBT 30% + UGL 15% + KMLM 15% (4-source diversification)."""
    return _wldu_static_blend(returns, {"WLDU": 0.40, "UBT": 0.30, "UGL": 0.15, "KMLM": 0.15})


def bt_w5_c5_wldu_wtip(returns: pd.DataFrame) -> pd.Series:
    """C5 — WLDU 50% + WTIP 50% (intl equity + 1.4× leveraged TIPS inflation hedge)."""
    return _wldu_static_blend(returns, {"WLDU": 0.50, "WTIP": 0.50})


def bt_w5_c6_wldu_wtip_kmlm(returns: pd.DataFrame) -> pd.Series:
    """C6 — WLDU 50% + WTIP 25% + KMLM 25% (intl + TIPS + MF triple diversifier)."""
    return _wldu_static_blend(returns, {"WLDU": 0.50, "WTIP": 0.25, "KMLM": 0.25})


def bt_w5_c7_wldu_wtip_ugl(returns: pd.DataFrame) -> pd.Series:
    """C7 — WLDU 50% + WTIP 25% + UGL 25% (intl + TIPS + gold)."""
    return _wldu_static_blend(returns, {"WLDU": 0.50, "WTIP": 0.25, "UGL": 0.25})


def bt_w5_c8_wldu_tyd(returns: pd.DataFrame) -> pd.Series:
    """C8 — WLDU 50% + TYD 50% (intl equity + 3× intermediate Treasury)."""
    return _wldu_static_blend(returns, {"WLDU": 0.50, "TYD": 0.50})


def bt_w5_c9_wldu_tyd_kmlm(returns: pd.DataFrame) -> pd.Series:
    """C9 — WLDU 50% + TYD 30% + KMLM 20% (intl + 3× IEF + MF)."""
    return _wldu_static_blend(returns, {"WLDU": 0.50, "TYD": 0.30, "KMLM": 0.20})


def _wldu_inv_vol_blend(returns: pd.DataFrame, tickers: list, vol_window: int = 60) -> pd.Series:
    """Inverse-vol weighted blend with monthly rebal."""
    series = {t: _wldu_series_for(returns, t) for t in tickers}
    funds = pd.DataFrame(series)
    rebal = set(_monthly_rebal_dates(funds.index))
    weights = {t: 1.0 / len(tickers) for t in tickers}
    out = []
    for d in funds.index:
        r = sum(weights[t] * funds[t].loc[d] for t in tickers)
        out.append(r)
        for t in tickers:
            weights[t] *= (1 + funds[t].loc[d])
        s = sum(weights.values())
        if s > 0:
            weights = {t: w / s for t, w in weights.items()}
        if d in rebal:
            invvols = {}
            for t in tickers:
                v = _trailing_vol(funds[t], d, vol_window)
                if v is not None and v > 0:
                    invvols[t] = 1.0 / v
            if invvols:
                tot = sum(invvols.values())
                weights = {t: (invvols[t] / tot if t in invvols else 0.0) for t in tickers}
    return pd.Series(out, index=funds.index)


def bt_w5_c10_wldu_ubt_ugl_dbc_invvol(returns: pd.DataFrame) -> pd.Series:
    """C10 — WLDU/UBT/UGL/DBC inverse-vol weighted, monthly rebal."""
    return _wldu_inv_vol_blend(returns, ["WLDU", "UBT", "UGL", "DBC"])


def bt_w5_c11_wldu_ubt_kmlm_ugl_invvol(returns: pd.DataFrame) -> pd.Series:
    """C11 — WLDU/UBT/KMLM/UGL inverse-vol weighted, monthly rebal."""
    return _wldu_inv_vol_blend(returns, ["WLDU", "UBT", "KMLM", "UGL"])


def _wldu_dd_defensive(returns: pd.DataFrame, defensive_ticker: str,
                        dd_threshold: float = 0.20, recovery_days: int = 63) -> pd.Series:
    """Hold WLDU; on DD-threshold breach swap to defensive_ticker for recovery_days, then re-engage."""
    wldu = spliced_wldu(returns)
    defensive = _wldu_series_for(returns, defensive_ticker)
    out = []
    nav = 1.0
    peak = 1.0
    in_defensive = False
    days_in_defensive = 0
    for d in returns.index:
        if in_defensive:
            r = float(defensive.loc[d])
            days_in_defensive += 1
            if days_in_defensive >= recovery_days:
                in_defensive = False
                peak = nav
        else:
            r = float(wldu.loc[d])
        out.append(r)
        nav *= (1 + r)
        peak = max(peak, nav)
        dd = (nav - peak) / peak if peak > 0 else 0
        if not in_defensive and dd < -dd_threshold:
            in_defensive = True
            days_in_defensive = 0
    return pd.Series(out, index=returns.index)


def bt_w5_c12_wldu_dd20_wtip(returns: pd.DataFrame) -> pd.Series:
    """C12 — WLDU + DD-20 stop → WTIP defensive for ~3 months."""
    return _wldu_dd_defensive(returns, "WTIP", dd_threshold=0.20, recovery_days=63)


def bt_w5_c13_wldu_dd25_ubt(returns: pd.DataFrame) -> pd.Series:
    """C13 — WLDU + DD-25 stop → UBT defensive for ~3 months."""
    return _wldu_dd_defensive(returns, "UBT", dd_threshold=0.25, recovery_days=63)


def bt_w5_c14_wldu_vix_regime(returns: pd.DataFrame, vix_df: pd.DataFrame) -> pd.Series:
    """C14 — VIX<25: 100% WLDU.  VIX≥25: 50% WLDU + 50% UBT.  Uses prior-day VIX."""
    wldu = spliced_wldu(returns)
    ubt = _lev_etf_return(returns, "UBT")
    vix_aligned = vix_df["value"].reindex(returns.index, method="ffill").shift(1).fillna(20.0)
    w_wldu = np.where(vix_aligned.values >= 25, 0.50, 1.00)
    w_ubt = np.where(vix_aligned.values >= 25, 0.50, 0.00)
    out = w_wldu * wldu.values + w_ubt * ubt.values
    return pd.Series(out, index=returns.index)


def bt_w5_c15_wldu_aaa_top3_5asset(returns: pd.DataFrame, prices: pd.DataFrame,
                                     top_n: int = 3, lookback: int = 126,
                                     dd_threshold: float = 0.25, target_vol: float = 0.20,
                                     vol_window: int = 60) -> pd.Series:
    """C15 — WLDU AAA top-3 over 5-asset universe (WLDU/UBT/UGL/DBC/KMLM).
    Expansion of C1 (top-2 over 4 assets) — adds KMLM as a fifth, more diversifiers."""
    ret_series = {
        "WLDU": spliced_wldu(returns),
        "UBT":  _lev_etf_return(returns, "UBT"),
        "UGL":  _lev_etf_return(returns, "UGL"),
        "DBC":  _lev_etf_return(returns, "DBC"),
        "KMLM": spliced_kmlm(returns),
    }
    px_series = {k: (1 + v).cumprod() * 100.0 for k, v in ret_series.items()}
    available = list(ret_series.keys())
    rebal_dates = set(_monthly_rebal_dates(returns.index))
    cash_ret = returns["SHV"].fillna(0) if "SHV" in returns.columns else pd.Series(0.0, index=returns.index)
    weights = {a: 0.0 for a in available}
    cash_weight = 1.0
    peak_nav = 1.0
    nav = 1.0
    out = []
    for d in returns.index:
        risk_r = sum(weights[a] * ret_series[a].loc[d] for a in available)
        r = risk_r + cash_weight * cash_ret.loc[d]
        out.append(r)
        nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = (nav - peak_nav) / peak_nav if peak_nav > 0 else 0
        if dd < -dd_threshold and (1 - cash_weight) > 0:
            weights = {a: 0.0 for a in available}; cash_weight = 1.0; peak_nav = nav
        for a in available:
            weights[a] *= (1 + ret_series[a].loc[d])
        cash_weight *= (1 + cash_ret.loc[d])
        s = sum(weights.values()) + cash_weight
        if s > 0:
            for k in weights:
                weights[k] /= s
            cash_weight /= s
        if d in rebal_dates and prices.index.get_indexer([d], method="ffill")[0] >= lookback:
            scores = {a: _trailing_return(px_series[a], d, lookback) for a in available}
            scores = {k: v for k, v in scores.items() if v is not None}
            if not scores:
                continue
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            picks = [a for a, sc in ranked[:top_n] if sc > 0]
            if not picks:
                weights = {a: 0.0 for a in available}; cash_weight = 1.0
                continue
            invvols = {}
            for a in picks:
                v = _trailing_vol(ret_series[a], d, vol_window)
                if v is not None:
                    invvols[a] = 1.0 / v
            if not invvols:
                w_each = 1.0 / len(picks)
                raw = {a: (w_each if a in picks else 0.0) for a in available}
            else:
                total_iv = sum(invvols.values())
                raw = {a: 0.0 for a in available}
                for a, iv in invvols.items():
                    raw[a] = iv / total_iv
            est_vol = sum(w * (_trailing_vol(ret_series[a], d, vol_window) or target_vol)
                          for a, w in raw.items() if w > 0)
            scale = min(1.0, target_vol / est_vol) if est_vol > 0 else 1.0
            weights = {a: w * scale for a, w in raw.items()}
            cash_weight = 1.0 - sum(weights.values())
            peak_nav = nav
    return pd.Series(out, index=returns.index)


# ═══════════════════════════════════════════════════════════════════════
# WAVE 6 — KMLM/duration/gold substitutes for C9 baseline
# Baseline = C9: WLDU 50% + TYD 30% + KMLM 20% (13.21% / 0.67 / -33%)
# Tests genuine alternatives: DBMF (alt MF), TLT/EDV (alt duration),
# UGL/GLD/SLV (alt diversifier). EDV is added — zero-coupon Treasury,
# ~25y duration, no daily-reset decay (cleaner than UBT/TYD/TMF).
# ═══════════════════════════════════════════════════════════════════════


def spliced_edv(returns: pd.DataFrame) -> pd.Series:
    """EDV: Vanguard Extended Duration Treasury (zero-coupon ~25y).
    Live from 2007-12-06. Pre-inception: synthesize as 1.4× TLT duration ratio
    (EDV duration ~25y vs TLT ~18y). Approximate — used only for extending the
    backtest window before EDV launch."""
    if "EDV" in returns.columns:
        live = returns["EDV"].fillna(0)
    else:
        live = pd.Series(0.0, index=returns.index)
    tlt = returns["TLT"].fillna(0) if "TLT" in returns.columns else pd.Series(0.0, index=returns.index)
    synth = tlt * 1.40
    splice = pd.Timestamp("2007-12-06")
    out = synth.copy()
    if live.abs().sum() > 0:
        out.loc[splice:] = live.loc[splice:].reindex(out.index).fillna(0).loc[splice:]
    return out


def _wave6_series_for(returns: pd.DataFrame, ticker: str) -> pd.Series:
    if ticker == "WLDU":  return spliced_wldu(returns)
    if ticker == "KMLM":  return spliced_kmlm(returns)
    if ticker == "EDV":   return spliced_edv(returns)
    if ticker == "DBMF":  return returns["DBMF"].fillna(0) if "DBMF" in returns.columns else pd.Series(0.0, index=returns.index)
    if ticker == "TLT":   return returns["TLT"].fillna(0) if "TLT" in returns.columns else pd.Series(0.0, index=returns.index)
    if ticker == "GLD":   return returns["GLD"].fillna(0) if "GLD" in returns.columns else pd.Series(0.0, index=returns.index)
    if ticker == "SLV":   return returns["SLV"].fillna(0) if "SLV" in returns.columns else pd.Series(0.0, index=returns.index)
    return _lev_etf_return(returns, ticker)


def _wave6_blend(returns: pd.DataFrame, weights: dict) -> pd.Series:
    """Static blend, quarterly drift+rebalance."""
    series = {t: _wave6_series_for(returns, t) for t in weights}
    funds = pd.DataFrame(series)
    return _drift_and_rebalance(funds, weights, _quarterly_rebal_dates(funds.index))


# ─── Group A: KMLM substitutes (managed futures alternatives) ───

def bt_w6_d1_wldu_tyd_dbmf(returns: pd.DataFrame) -> pd.Series:
    """D1 — WLDU 50% + TYD 30% + DBMF 20% (DBMF replaces KMLM as MF replicator)."""
    return _wave6_blend(returns, {"WLDU": 0.50, "TYD": 0.30, "DBMF": 0.20})


def bt_w6_d2_wldu_tyd_kmlm_dbmf(returns: pd.DataFrame) -> pd.Series:
    """D2 — WLDU 50% + TYD 30% + KMLM 10% + DBMF 10% (split MF across replicators)."""
    return _wave6_blend(returns, {"WLDU": 0.50, "TYD": 0.30, "KMLM": 0.10, "DBMF": 0.10})


# ─── Group B: Duration substitutes ───

def bt_w6_d3_wldu_tlt_kmlm(returns: pd.DataFrame) -> pd.Series:
    """D3 — WLDU 50% + TLT 30% + KMLM 20% (unleveraged TLT instead of TYD/UBT)."""
    return _wave6_blend(returns, {"WLDU": 0.50, "TLT": 0.30, "KMLM": 0.20})


def bt_w6_d4_wldu_edv_kmlm(returns: pd.DataFrame) -> pd.Series:
    """D4 — WLDU 50% + EDV 30% + KMLM 20% (zero-coupon extended-duration Treasury)."""
    return _wave6_blend(returns, {"WLDU": 0.50, "EDV": 0.30, "KMLM": 0.20})


def bt_w6_d5_wldu_edv_tyd_kmlm(returns: pd.DataFrame) -> pd.Series:
    """D5 — WLDU 40% + EDV 25% + TYD 15% + KMLM 20% (duration barbell: long-zero + intermediate-3×)."""
    return _wave6_blend(returns, {"WLDU": 0.40, "EDV": 0.25, "TYD": 0.15, "KMLM": 0.20})


# ─── Group C: Gold substitutes for KMLM ───

def bt_w6_d6_wldu_tyd_ugl(returns: pd.DataFrame) -> pd.Series:
    """D6 — WLDU 50% + TYD 30% + UGL 20% (2× gold replaces MF)."""
    return _wave6_blend(returns, {"WLDU": 0.50, "TYD": 0.30, "UGL": 0.20})


def bt_w6_d7_wldu_tyd_gld(returns: pd.DataFrame) -> pd.Series:
    """D7 — WLDU 50% + TYD 30% + GLD 20% (1× gold replaces MF)."""
    return _wave6_blend(returns, {"WLDU": 0.50, "TYD": 0.30, "GLD": 0.20})


# ─── Group D: Multi-asset additions to C9 baseline ───

def bt_w6_d8_wldu_tyd_kmlm_ugl(returns: pd.DataFrame) -> pd.Series:
    """D8 — WLDU 50% + TYD 25% + KMLM 15% + UGL 10% (adds 2× gold to C9)."""
    return _wave6_blend(returns, {"WLDU": 0.50, "TYD": 0.25, "KMLM": 0.15, "UGL": 0.10})


def bt_w6_d9_wldu_tyd_kmlm_gld_slv(returns: pd.DataFrame) -> pd.Series:
    """D9 — WLDU 50% + TYD 25% + KMLM 15% + GLD 5% + SLV 5% (adds 1× precious metals to C9)."""
    return _wave6_blend(returns, {"WLDU": 0.50, "TYD": 0.25, "KMLM": 0.15, "GLD": 0.05, "SLV": 0.05})


def bt_w6_d10_wldu_edv_kmlm_ugl(returns: pd.DataFrame) -> pd.Series:
    """D10 — WLDU 45% + EDV 25% + KMLM 20% + UGL 10% (EDV duration + KMLM + gold)."""
    return _wave6_blend(returns, {"WLDU": 0.45, "EDV": 0.25, "KMLM": 0.20, "UGL": 0.10})


# ═══════════════════════════════════════════════════════════════════════
# WAVE 7 — strict ≤2× per-ticker + zero-deployed-overlap candidates.
# Lower notional than W6 (no leveraged duration) — uses 1× TLT/EDV + 1×
# precious metals + DBMF (alt MF) + capital-efficient stacks (NTSX/GDE/RSSB).
# ═══════════════════════════════════════════════════════════════════════


def _wave7_series_for(returns: pd.DataFrame, ticker: str) -> pd.Series:
    if ticker == "WLDU":  return spliced_wldu(returns)
    if ticker == "EDV":   return spliced_edv(returns)
    if ticker == "DBMF":  return returns["DBMF"].fillna(0) if "DBMF" in returns.columns else pd.Series(0.0, index=returns.index)
    if ticker == "TLT":   return returns["TLT"].fillna(0) if "TLT" in returns.columns else pd.Series(0.0, index=returns.index)
    if ticker == "GLD":   return returns["GLD"].fillna(0) if "GLD" in returns.columns else pd.Series(0.0, index=returns.index)
    if ticker == "SLV":   return returns["SLV"].fillna(0) if "SLV" in returns.columns else pd.Series(0.0, index=returns.index)
    if ticker == "NTSX":  return synthetic_ntsx_returns(returns)
    if ticker == "NTSI":  return synthetic_ntsi_returns(returns)
    if ticker == "GDE":   return synthetic_gde_returns(returns)
    if ticker == "GDT":   return synthetic_gdt_returns(returns)
    if ticker == "RSIT":  return synthetic_rsit_returns(returns)
    if ticker == "GOLY":  return synthetic_goly_returns(returns)
    if ticker == "RSSB":  return spliced_rssb_wtip(returns)[0]
    return _lev_etf_return(returns, ticker)


def _wave7_blend(returns: pd.DataFrame, weights: dict, return_weights: bool = False):
    """Static blend, quarterly drift+rebalance."""
    series = {t: _wave7_series_for(returns, t) for t in weights}
    funds = pd.DataFrame(series)
    ret = _drift_and_rebalance(funds, weights, _quarterly_rebal_dates(funds.index))
    if not return_weights:
        return ret
    weights_df = _target_weights_from_segments(
        [(funds.index[0], weights)], funds.index, "Q"
    )
    return ret, {"weights": weights_df, "asset_returns": funds.fillna(0.0)}


def bt_w7_e1_wldu_tlt_gld(returns: pd.DataFrame) -> pd.Series:
    """E1 — WLDU 50% + TLT 30% + GLD 20%. Pure clean baseline, ≤2× per ticker."""
    return _wave7_blend(returns, {"WLDU": 0.50, "TLT": 0.30, "GLD": 0.20})


def bt_w7_e2_wldu_edv_gld(returns: pd.DataFrame) -> pd.Series:
    """E2 — WLDU 50% + EDV 30% + GLD 20% (zero-coupon ~25y duration)."""
    return _wave7_blend(returns, {"WLDU": 0.50, "EDV": 0.30, "GLD": 0.20})


def bt_w7_e3_wldu_tlt_dbmf(returns: pd.DataFrame) -> pd.Series:
    """E3 — WLDU 50% + TLT 30% + DBMF 20% (alt MF replicator)."""
    return _wave7_blend(returns, {"WLDU": 0.50, "TLT": 0.30, "DBMF": 0.20})


def bt_w7_e4_wldu_edv_dbmf(returns: pd.DataFrame) -> pd.Series:
    """E4 — WLDU 50% + EDV 30% + DBMF 20%."""
    return _wave7_blend(returns, {"WLDU": 0.50, "EDV": 0.30, "DBMF": 0.20})


def bt_w7_e5_wldu_tlt_dbmf_gld(returns: pd.DataFrame) -> pd.Series:
    """E5 — WLDU 50% + TLT 25% + DBMF 15% + GLD 10% (4-asset clean diversifier)."""
    return _wave7_blend(returns, {"WLDU": 0.50, "TLT": 0.25, "DBMF": 0.15, "GLD": 0.10})


def bt_w7_e6_wldu_edv_dbmf_gld(returns: pd.DataFrame) -> pd.Series:
    """E6 — WLDU 50% + EDV 25% + DBMF 15% + GLD 10% (4-asset, EDV duration)."""
    return _wave7_blend(returns, {"WLDU": 0.50, "EDV": 0.25, "DBMF": 0.15, "GLD": 0.10})


def bt_w7_e7_wldu_gde_tlt(returns: pd.DataFrame) -> pd.Series:
    """E7 — WLDU 40% + GDE 30% + TLT 30% (gold via capital-efficient stack)."""
    return _wave7_blend(returns, {"WLDU": 0.40, "GDE": 0.30, "TLT": 0.30})


def bt_w7_e8_wldu_rssb_gld(returns: pd.DataFrame) -> pd.Series:
    """E8 — WLDU 40% + RSSB 40% + GLD 20% (global stocks+bonds via capital-efficient stack)."""
    return _wave7_blend(returns, {"WLDU": 0.40, "RSSB": 0.40, "GLD": 0.20})


def bt_w7_e9_wldu_ntsx_gld(returns: pd.DataFrame) -> pd.Series:
    """E9 — WLDU 40% + NTSX 40% + GLD 20% (US 90/60 stack + gold)."""
    return _wave7_blend(returns, {"WLDU": 0.40, "NTSX": 0.40, "GLD": 0.20})


def bt_w7_e10_wldu_gde_dbmf(returns: pd.DataFrame) -> pd.Series:
    """E10 — WLDU 40% + GDE 30% + DBMF 30% (gold-stack + MF, no separate bonds)."""
    return _wave7_blend(returns, {"WLDU": 0.40, "GDE": 0.30, "DBMF": 0.30})


# ═══════════════════════════════════════════════════════════════════════
# WAVE 8 — capital-efficient stack expansion using PDF-discovered ETFs.
# Adds NTSI (intl 90/60), GDT (TIPS+gold), RSIT (VT+MF), GOLY (gold+MF+
# corp-bonds). All ≤2× per ticker, zero deployed-overlap. Wave 7 strategies
# get re-run with extended DBMF history (DBMFSIM back to 2000).
# ═══════════════════════════════════════════════════════════════════════


def bt_w8_f1_wldu_ntsi_gld(returns: pd.DataFrame) -> pd.Series:
    """F1 — WLDU 40% + NTSI 40% + GLD 20% (pure intl 90/60 stack + gold).
    Cleanest intl-diversification: NTSI = 90% VEA + 60% Treasuries, no US duplication."""
    return _wave7_blend(returns, {"WLDU": 0.40, "NTSI": 0.40, "GLD": 0.20})


def bt_w8_f2_wldu_gdt_tlt(returns: pd.DataFrame) -> pd.Series:
    """F2 — WLDU 50% + GDT 30% + TLT 20% (TIPS+gold stack + long duration).
    Replaces UGL/GLD with GDT (90% TIPS + 90% gold = inflation-protected gold)."""
    return _wave7_blend(returns, {"WLDU": 0.50, "GDT": 0.30, "TLT": 0.20})


def bt_w8_f3_wldu_rsit_gld(returns: pd.DataFrame) -> pd.Series:
    """F3 — WLDU 40% + RSIT 40% + GLD 20% (global stocks+MF stacked, plus gold).
    RSIT bundles VT+MF in one ticker — addresses both equity AND MF in 40% capital."""
    return _wave7_blend(returns, {"WLDU": 0.40, "RSIT": 0.40, "GLD": 0.20})


def bt_w8_f4_wldu_goly_tlt(returns: pd.DataFrame, return_weights: bool = False):
    """F4 — WLDU 40% + GOLY 30% + TLT 30% (gold+MF+corp-bonds stack + long Treasury).
    GOLY = 50% gold + 50% MF + 100% corp bonds at 200% notional."""
    return _wave7_blend(returns, {"WLDU": 0.40, "GOLY": 0.30, "TLT": 0.30},
                        return_weights=return_weights)


def bt_w8_f5_wldu_ntsi_rsit(returns: pd.DataFrame) -> pd.Series:
    """F5 — WLDU 40% + NTSI 40% + RSIT 20% (pure-stacks design).
    All three legs are capital-efficient stacks: WLDU (2× intl), NTSI (intl 90/60),
    RSIT (global stocks + MF). No standalone equity/duration sleeve."""
    return _wave7_blend(returns, {"WLDU": 0.40, "NTSI": 0.40, "RSIT": 0.20})


def bt_w8_f6_wldu_ntsi_gdt(returns: pd.DataFrame) -> pd.Series:
    """F6 — WLDU 40% + NTSI 30% + GDT 30% (intl-stack + inflation-defense).
    NTSI for nominal-duration intl, GDT for TIPS+gold inflation defense."""
    return _wave7_blend(returns, {"WLDU": 0.40, "NTSI": 0.30, "GDT": 0.30})


# ═══════════════════════════════════════════════════════════════════════
# METRICS + REPORT
# ═══════════════════════════════════════════════════════════════════════

def compute_metrics(returns: pd.Series, rf: float = RISK_FREE_RATE, fast: bool = False) -> dict:
    """Compute return-series metrics. With fast=True (used by MC bootstrap
    inner loop), skips the expensive distribution + rolling stats — keeps
    only CAGR/Vol/Sharpe/Sortino/MaxDD/Calmar/Worst-Year/Best-Year."""
    r = returns.dropna()
    if len(r) < 2:
        return {}
    ann = 252
    cum = (1 + r).cumprod()
    total = float(cum.iloc[-1] - 1)
    years = len(r) / ann
    cagr = (1 + total) ** (1 / years) - 1
    vol = float(r.std() * np.sqrt(ann))
    sharpe = (cagr - rf) / vol if vol > 0 else np.nan
    downside = r[r < 0]
    dvol = float(downside.std() * np.sqrt(ann)) if len(downside) > 0 else np.nan
    sortino = (cagr - rf) / dvol if dvol and dvol > 0 else np.nan
    rolling = cum.cummax()
    dd = (cum - rolling) / rolling
    max_dd = float(dd.min())
    calmar = cagr / abs(max_dd) if max_dd else np.nan
    yearly = r.resample("Y").apply(lambda x: (1 + x).prod() - 1)
    out = {
        "CAGR": cagr,
        "Vol (ann)": vol,
        "Sharpe": sharpe,
        "Sortino": sortino,
        "Max DD": max_dd,
        "Calmar": calmar,
        "Worst Year": float(yearly.min()) if len(yearly) > 0 else np.nan,
        "Best Year": float(yearly.max()) if len(yearly) > 0 else np.nan,
        "Total Return": total,
        "Years": years,
    }
    if fast:
        return out
    # Distribution stats on monthly returns
    monthly = r.resample("M").apply(lambda x: (1 + x).prod() - 1)
    skew = float(monthly.skew()) if len(monthly) > 2 else np.nan
    kurt = float(monthly.kurt()) if len(monthly) > 3 else np.nan
    var5 = float(monthly.quantile(0.05)) if len(monthly) > 20 else np.nan
    tail = monthly[monthly <= var5] if not np.isnan(var5) else pd.Series([], dtype=float)
    cvar5 = float(tail.mean()) if len(tail) > 0 else np.nan
    # Worst rolling N-year compound returns (vectorized via cumprod ratio)
    def worst_rolling(window_days):
        if len(r) < window_days + 1:
            return np.nan
        rolling_ret = (cum / cum.shift(window_days)) - 1
        return float(rolling_ret.min())
    underwater = dd < 0
    if underwater.any():
        runs = (underwater != underwater.shift()).cumsum()
        max_underwater_days = int(underwater.groupby(runs).sum().max())
    else:
        max_underwater_days = 0
    out.update({
        "Skew (monthly)": skew,
        "Excess Kurt (monthly)": kurt,
        "VaR 5% (monthly)": var5,
        "CVaR 5% (monthly)": cvar5,
        "Worst 1y Rolling": worst_rolling(252),
        "Worst 3y Rolling": worst_rolling(252 * 3),
        "Worst 5y Rolling": worst_rolling(252 * 5),
        "Max Days Underwater": max_underwater_days,
    })
    return out


# ═══════════════════════════════════════════════════════════════════════
# FINANCIAL-SCIENCE PROMOTION-DECISION HELPERS
# Added 2026-05-12 for the final Wave 7/8 shortlist evaluation. These tools
# go beyond standalone CAGR/Sharpe to evaluate a candidate's marginal value
# to the deployed portfolio: correlation, what-if injection, regime splits.
# ═══════════════════════════════════════════════════════════════════════


def compute_correlation_matrix(returns_dict: dict, names: list) -> pd.DataFrame:
    """Pairwise daily-return correlation between every pair in `names`, on
    their common date range. Returns a DataFrame indexed/columned by name."""
    common = None
    for n in names:
        if n not in returns_dict:
            continue
        s = returns_dict[n].dropna()
        common = s.index if common is None else common.intersection(s.index)
    if common is None or len(common) < 20:
        return pd.DataFrame()
    df = pd.DataFrame({n: returns_dict[n].reindex(common) for n in names if n in returns_dict})
    return df.corr()


def portfolio_what_if(deployed_returns_dict: dict, deployed_alloc: dict,
                       candidate_series: pd.Series, candidate_alloc: float,
                       rf: float = RISK_FREE_RATE) -> dict:
    """Simulate adding `candidate` at `candidate_alloc` to the deployed portfolio
    (with renormalization), and compare pre/post aggregate metrics.

    The deployed weights are scaled down by (1 - candidate_alloc) so the new
    aggregate sums to 1.0.
    """
    # Build common index
    series_list = [s for s in deployed_returns_dict.values() if s is not None]
    series_list.append(candidate_series)
    common = None
    for s in series_list:
        s = s.dropna()
        common = s.index if common is None else common.intersection(s.index)
    if common is None or len(common) < 20:
        return {}

    # Pre: deployed aggregate (weights as-is, normalized)
    total_dep = sum(deployed_alloc.values())
    pre_weights = {k: v / total_dep for k, v in deployed_alloc.items()}
    pre = pd.Series(0.0, index=common)
    for name, w in pre_weights.items():
        if name in deployed_returns_dict and deployed_returns_dict[name] is not None:
            pre += w * deployed_returns_dict[name].reindex(common).fillna(0)

    # Post: deployed scaled by (1 - candidate_alloc), candidate at its alloc
    scale = 1.0 - candidate_alloc
    post = pd.Series(0.0, index=common)
    for name, w in pre_weights.items():
        if name in deployed_returns_dict and deployed_returns_dict[name] is not None:
            post += scale * w * deployed_returns_dict[name].reindex(common).fillna(0)
    post += candidate_alloc * candidate_series.reindex(common).fillna(0)

    pre_m = compute_metrics(pre, rf)
    post_m = compute_metrics(post, rf)
    return {
        "common_start": str(common[0].date()),
        "common_end": str(common[-1].date()),
        "pre": pre_m,
        "post": post_m,
        "delta_cagr": post_m.get("CAGR", 0) - pre_m.get("CAGR", 0),
        "delta_sharpe": post_m.get("Sharpe", 0) - pre_m.get("Sharpe", 0),
        "delta_maxdd": post_m.get("Max DD", 0) - pre_m.get("Max DD", 0),
        "delta_vol": post_m.get("Vol (ann)", 0) - pre_m.get("Vol (ann)", 0),
    }


# Macro regime windows for sub-period analysis. Each tuple is (label, start, end).
MACRO_REGIMES = [
    ("Pre-GFC (2002-2007)",        "2002-07-30", "2007-10-09"),
    ("GFC + recovery (2007-2012)", "2007-10-10", "2012-12-31"),
    ("Bull / low-rate (2013-2019)", "2013-01-01", "2019-12-31"),
    ("Pandemic (2020-2021)",       "2020-01-01", "2021-12-31"),
    ("Inflation (2022-2026)",      "2022-01-01", "2026-12-31"),
]


def regime_split_metrics(series: pd.Series, regimes: list = None) -> list:
    """Compute per-regime CAGR / Sharpe / MaxDD for a return series.
    Returns a list of dicts, one per regime, keyed by label."""
    if regimes is None:
        regimes = MACRO_REGIMES
    out = []
    for label, start, end in regimes:
        sliced = series.loc[start:end].dropna()
        if len(sliced) < 20:
            out.append({"label": label, "n_days": len(sliced), "metrics": {}})
            continue
        m = compute_metrics(sliced)
        out.append({
            "label": label,
            "n_days": len(sliced),
            "metrics": {
                "CAGR": m.get("CAGR", np.nan),
                "Sharpe": m.get("Sharpe", np.nan),
                "Max DD": m.get("Max DD", np.nan),
                "Total Return": m.get("Total Return", np.nan),
            },
        })
    return out


def rolling_sharpe(series: pd.Series, window_years: float = 3, rf: float = RISK_FREE_RATE) -> pd.Series:
    """Annualized rolling Sharpe over a window_years window."""
    window_days = int(window_years * 252)
    if len(series) < window_days:
        return pd.Series(dtype=float)
    rolling_cagr = (1 + series).rolling(window_days).apply(np.prod, raw=True) ** (252 / window_days) - 1
    rolling_vol = series.rolling(window_days).std() * np.sqrt(252)
    return (rolling_cagr - rf) / rolling_vol


def stationary_bootstrap_indices(n: int, mean_block: int, rng: np.random.Generator) -> np.ndarray:
    """
    Politis-Romano (1994) stationary bootstrap. Each step has probability p = 1/mean_block
    of starting a fresh random block; otherwise continues the previous index by +1 (with
    wrap-around). Block lengths follow a geometric distribution with mean = mean_block.

    Returns an n-length integer index array into [0, n).
    """
    p = 1.0 / mean_block
    idx = np.empty(n, dtype=np.int64)
    idx[0] = rng.integers(0, n)
    coin = rng.random(n) < p
    rand_idx = rng.integers(0, n, size=n)
    for t in range(1, n):
        if coin[t]:
            idx[t] = rand_idx[t]
        else:
            idx[t] = (idx[t - 1] + 1) % n
    return idx


def monte_carlo_per_strategy(returns_dict: dict, strategies: list, benchmarks: list,
                              aggregate_series: pd.Series = None,
                              n_sims: int = 2000, mean_block: int = 63,
                              seed: int = 42) -> pd.DataFrame:
    """
    Per-strategy stationary block bootstrap. Each strategy is resampled on its
    OWN native window (no zero-padding). Benchmarks are sliced to the same window
    before resampling, and the same bootstrap indices are used → matched paths
    for beat-benchmark probability.

    The previous joint-resampling implementation forced a common timeline across
    all strategies, which zero-padded shorter-window strategies (e.g. Bronze /
    AAA Free 2× + NTSD start 2006-08 but the joint window was 1970+). That diluted
    their MC CAGR by spreading the same total return over 56 years instead of
    their real 19.7 years. Per-strategy MC fixes this — the deterministic value
    now falls inside the bootstrap distribution, as it should.

    The optional `aggregate_series` is treated as one more strategy (it has its
    own native window — the full deployed-aggregate timeline).

    Returns a long-format DataFrame with columns:
      sim, strategy, cagr, sharpe, max_dd, worst_yr, benchmark, bench_cagr, bench_sharpe, bench_dd, bench_worst
    One row per (sim × strategy × benchmark) so beat-benchmark probabilities can
    be computed on matched paths within the strategy's native window.
    """
    rng = np.random.default_rng(seed)

    # Build {target: native-window series} for every strategy + aggregate.
    series_map: dict[str, pd.Series] = {}
    for s in strategies:
        if s in returns_dict and returns_dict[s] is not None:
            ser = returns_dict[s].dropna()
            if len(ser) >= 252:
                series_map[s] = ser
    if aggregate_series is not None:
        agg = aggregate_series.dropna()
        # Trim leading zeros (when no deployed strategies were active yet)
        nonzero = agg[agg != 0]
        if len(nonzero) > 0:
            agg = agg.loc[nonzero.index[0]:]
        if len(agg) >= 252:
            series_map["AGGREGATE"] = agg

    # ── Fast numpy-only metric helper ───────────────────────────────
    # Avoids pd.Series construction + slow resample-apply in the inner loop.
    # CAGR/Sharpe/MaxDD on a daily-returns numpy array; "Worst Year" is
    # approximated as the worst 252-day compounded rolling window (close
    # to calendar-year worst on a daily-aligned series).
    rf = RISK_FREE_RATE
    ann = 252

    def _fast_metrics(r: np.ndarray):
        n_ = len(r)
        if n_ < 2:
            return None
        years = n_ / ann
        cum = np.cumprod(1.0 + r)
        total = cum[-1] - 1.0
        cagr_ = (1.0 + total) ** (1.0 / years) - 1.0 if total > -1.0 else -1.0
        vol_ = float(r.std() * np.sqrt(ann))
        sharpe_ = (cagr_ - rf) / vol_ if vol_ > 0 else np.nan
        running_max = np.maximum.accumulate(cum)
        dd = (cum - running_max) / running_max
        max_dd_ = float(dd.min())
        # Worst year ≈ worst 252-day rolling compounded return (vectorized)
        if n_ > ann:
            rolling_yr = cum[ann:] / cum[:-ann] - 1.0
            worst_yr = float(rolling_yr.min())
        else:
            worst_yr = total
        return cagr_, sharpe_, max_dd_, worst_yr

    rows = []
    import time as _time
    for target_name, s_series in series_map.items():
        t0 = _time.time()
        n = len(s_series)
        s_arr = s_series.values
        s_dates = s_series.index

        # Pre-slice each benchmark to the same window the strategy ran on.
        bench_arrs = {}
        for b in benchmarks:
            if b in returns_dict and returns_dict[b] is not None:
                aligned = returns_dict[b].reindex(s_dates).fillna(0).values
                bench_arrs[b] = aligned

        for sim in range(n_sims):
            idx = stationary_bootstrap_indices(n, mean_block, rng)
            sim_s = s_arr[idx]
            sm = _fast_metrics(sim_s)
            if sm is None:
                continue
            s_cagr, s_sharpe, s_dd, s_worst = sm

            for b, b_arr in bench_arrs.items():
                sim_b = b_arr[idx]
                bm = _fast_metrics(sim_b)
                if bm is None:
                    continue
                b_cagr, b_sharpe, b_dd, b_worst = bm
                rows.append({
                    "sim":          sim,
                    "strategy":     target_name,
                    "benchmark":    b,
                    "cagr":         s_cagr,
                    "sharpe":       s_sharpe,
                    "max_dd":       s_dd,
                    "worst_yr":     s_worst,
                    "bench_cagr":   b_cagr,
                    "bench_sharpe": b_sharpe,
                    "bench_dd":     b_dd,
                    "bench_worst":  b_worst,
                })
            if not bench_arrs:
                rows.append({
                    "sim":          sim,
                    "strategy":     target_name,
                    "benchmark":    None,
                    "cagr":         s_cagr,
                    "sharpe":       s_sharpe,
                    "max_dd":       s_dd,
                    "worst_yr":     s_worst,
                })
        print(f"  · {target_name:<60} n={n:>5}  done in {_time.time()-t0:.1f}s",
              flush=True)

    return pd.DataFrame(rows)


def report_monte_carlo_per_strategy(mc: pd.DataFrame, deterministic_metrics: dict,
                                      benchmarks: list, strategy_display: dict = None):
    """
    Print:
      • Per-strategy CAGR / Sharpe / MaxDD distribution (det + p5/p50/p95)
      • Per-strategy beat-benchmark probabilities for each benchmark
    """
    pct_levels = [5, 50, 95]
    strategies = sorted(mc["strategy"].unique(), key=lambda s: -mc[mc["strategy"] == s]["cagr"].median())
    display = strategy_display or {}

    def name(s):
        return display.get(s, s)

    # Use one benchmark slice to summarise per-strategy own metrics (independent of which benchmark)
    primary_bench = benchmarks[0]
    own = mc[mc["benchmark"] == primary_bench]

    print(f"\nMonte Carlo robustness — n={mc['sim'].nunique()} sims, joint stationary bootstrap, mean block 63 days")
    print("=" * 105)
    print("Per-strategy CAGR distribution:")
    print(f"  {'Strategy':<28} {'det':>7}  {'p5':>7}  {'p50':>7}  {'p95':>7}")
    print("  " + "-" * 65)
    for s in strategies:
        sub = own[own["strategy"] == s]["cagr"]
        det = deterministic_metrics.get(s, {}).get("CAGR")
        det_str = f"{det*100:+5.2f}%" if det is not None else "  n/a"
        p5, p50, p95 = sub.quantile(0.05), sub.quantile(0.50), sub.quantile(0.95)
        print(f"  {name(s):<28} {det_str:>7}  {p5*100:>+6.2f}%  {p50*100:>+6.2f}%  {p95*100:>+6.2f}%")

    print("\nPer-strategy Sharpe distribution:")
    print(f"  {'Strategy':<28} {'det':>7}  {'p5':>7}  {'p50':>7}  {'p95':>7}")
    print("  " + "-" * 65)
    for s in strategies:
        sub = own[own["strategy"] == s]["sharpe"]
        det = deterministic_metrics.get(s, {}).get("Sharpe")
        det_str = f"{det:5.2f}" if det is not None else "n/a"
        p5, p50, p95 = sub.quantile(0.05), sub.quantile(0.50), sub.quantile(0.95)
        print(f"  {name(s):<28} {det_str:>7}  {p5:>7.2f}  {p50:>7.2f}  {p95:>7.2f}")

    print("\nPer-strategy Max DD distribution:")
    print(f"  {'Strategy':<28} {'det':>7}  {'p5':>7}  {'p50':>7}  {'p95':>7}")
    print("  " + "-" * 65)
    for s in strategies:
        sub = own[own["strategy"] == s]["max_dd"]
        det = deterministic_metrics.get(s, {}).get("Max DD")
        det_str = f"{det*100:+5.2f}%" if det is not None else "  n/a"
        p5, p50, p95 = sub.quantile(0.05), sub.quantile(0.50), sub.quantile(0.95)
        print(f"  {name(s):<28} {det_str:>7}  {p5*100:>+6.2f}%  {p50*100:>+6.2f}%  {p95*100:>+6.2f}%")

    print("\nBeat-benchmark probability (matched bootstrap paths):")
    header_b = "  ".join(f"{b[:20]:>22}" for b in benchmarks)
    print(f"  {'Strategy':<28} {'metric':<7}  {header_b}")
    print("  " + "-" * (40 + 23 * len(benchmarks)))
    for s in strategies:
        for metric_name, col, comp in [
            ("CAGR",   "cagr",   lambda r: r["cagr"]   > r["bench_cagr"]),
            ("Sharpe", "sharpe", lambda r: r["sharpe"] > r["bench_sharpe"]),
            ("MaxDD",  "max_dd", lambda r: r["max_dd"] > r["bench_dd"]),  # less negative
        ]:
            cells = []
            for b in benchmarks:
                sub = mc[(mc["strategy"] == s) & (mc["benchmark"] == b)]
                if len(sub) == 0:
                    cells.append(f"{'n/a':>22}")
                    continue
                prob = comp(sub).mean()
                cells.append(f"{prob*100:>20.1f}%  ")
            print(f"  {name(s):<28} {metric_name:<7}  {'  '.join(cells)}")


def fmt_table(metrics_dict: dict) -> pd.DataFrame:
    df = pd.DataFrame(metrics_dict).T
    pct_cols = ["CAGR", "Vol (ann)", "Max DD", "Worst Year", "Best Year", "Total Return"]
    for col in pct_cols:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: f"{x:.2%}" if pd.notna(x) else "—")
    for col in ["Sharpe", "Sortino", "Calmar", "Years"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
    return df


def plot_equity_curves(rets_dict: dict, title: str, filename: str):
    fig, ax = plt.subplots(figsize=(13, 7))
    for name, r in rets_dict.items():
        cum = (1 + r).cumprod()
        ax.plot(cum.index, cum.values, lw=1.6, label=name)
    ax.set_title(title, fontsize=14, fontweight="bold")
    ax.set_ylabel("Growth of $1 (log)")
    ax.set_yscale("log")
    ax.grid(alpha=0.3)
    ax.legend(ncol=3, fontsize=9)
    plt.tight_layout()
    plt.savefig(filename, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  ✓ {filename}")


def plot_drawdowns(rets_dict: dict, title: str, filename: str):
    fig, ax = plt.subplots(figsize=(13, 7))
    for name, r in rets_dict.items():
        cum = (1 + r).cumprod()
        rolling = cum.cummax()
        dd = (cum - rolling) / rolling
        ax.plot(dd.index, dd.values, lw=1.4, label=name, alpha=0.8)
    ax.set_title(title, fontsize=14, fontweight="bold")
    ax.set_ylabel("Drawdown")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0%}"))
    ax.grid(alpha=0.3)
    ax.legend(ncol=3, fontsize=9)
    plt.tight_layout()
    plt.savefig(filename, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  ✓ {filename}")


def plot_rolling_sharpe(rets_dict: dict, window: int, title: str, filename: str):
    fig, ax = plt.subplots(figsize=(13, 7))
    for name, r in rets_dict.items():
        roll_mean = r.rolling(window).mean() * 252
        roll_vol = r.rolling(window).std() * np.sqrt(252)
        roll_sharpe = (roll_mean - RISK_FREE_RATE) / roll_vol
        ax.plot(roll_sharpe.index, roll_sharpe.values, lw=1.3, label=name, alpha=0.8)
    ax.axhline(0, color="black", lw=0.5)
    ax.set_title(title, fontsize=14, fontweight="bold")
    ax.set_ylabel(f"Rolling {window // 21}M Sharpe")
    ax.grid(alpha=0.3)
    ax.legend(ncol=3, fontsize=9)
    plt.tight_layout()
    plt.savefig(filename, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  ✓ {filename}")


def _plot_tax_drag_comparison(deployed_names: list, metrics: dict,
                              after_tax_metrics: dict, aggregate_metrics: dict,
                              filename: str):
    """Grouped bar chart: gross vs after-tax CAGR per deployed strategy + aggregate."""
    names = [n for n in deployed_names if n in after_tax_metrics] + ["AGGREGATE (deployed)"]
    gross = []
    aft = []
    drag = []
    labels = []
    for n in names:
        if n == "AGGREGATE (deployed)":
            g = aggregate_metrics.get("CAGR", 0) * 100
        else:
            g = metrics.get(n, {}).get("CAGR", 0) * 100
        a = after_tax_metrics.get(n, {}).get("After-Tax CAGR", 0) * 100
        gross.append(g)
        aft.append(a)
        drag.append(g - a)
        labels.append(n if len(n) < 30 else n[:27] + "…")
    fig, ax = plt.subplots(figsize=(13, 6.5))
    x = np.arange(len(names))
    width = 0.38
    bars_g = ax.bar(x - width/2, gross, width, label="Gross CAGR", color="#2c7fb8")
    bars_a = ax.bar(x + width/2, aft, width, label="After-Tax CAGR", color="#7fcdbb")
    # Drag annotations above the after-tax bars
    for i, (b_a, d) in enumerate(zip(bars_a, drag)):
        h = b_a.get_height()
        ax.text(b_a.get_x() + b_a.get_width()/2, h + 0.3,
                f"−{d:.2f}pp", ha="center", va="bottom",
                fontsize=9,
                color=("#0a0" if d < 1.0 else "#c80" if d < 2.0 else "#c00"))
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=25, ha="right", fontsize=10)
    ax.set_ylabel("CAGR")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.set_title("Gross vs After-Tax CAGR — German tax drag per deployed sleeve",
                 fontsize=13, fontweight="bold")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(loc="upper right")
    plt.tight_layout()
    plt.savefig(filename, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  ✓ {filename}")


def plot_retirement_projection(mc_ret: dict, det: dict, age_today: float,
                                target: float, monthly: float, filename: str,
                                swr: float = RETIREMENT_SWR,
                                mc_basis: str = "after-tax"):
    """
    Forward wealth projection chart in real dollars.

    Shows the deterministic median path + p5/p50/p95 MC bands + target line.
    X-axis is age (today → today + max_years). Annotates the median
    retirement age at the target intersection.
    """
    paths_yearly = mc_ret["paths_yearly"]
    max_years = mc_ret["max_years"]
    n_years = paths_yearly.shape[1]
    ages = age_today + np.arange(n_years)

    p5 = np.percentile(paths_yearly, 5, axis=0)
    p50 = np.percentile(paths_yearly, 50, axis=0)
    p95 = np.percentile(paths_yearly, 95, axis=0)

    fig, ax = plt.subplots(figsize=(13, 7))
    ax.fill_between(ages, p5, p95, alpha=0.18, color="#2c7fb8",
                    label="MC p5–p95 band")
    ax.plot(ages, p50, lw=2.4, color="#2c7fb8", label="MC median (p50)")
    ax.plot(ages, p5, lw=1.0, color="#2c7fb8", alpha=0.6, linestyle=":",
            label="MC p5 (pessimistic)")
    ax.plot(ages, p95, lw=1.0, color="#2c7fb8", alpha=0.6, linestyle=":",
            label="MC p95 (optimistic)")

    # Deterministic path — sample at year ends
    det_path = det["path"]
    det_yearly = det_path[::12][:n_years]
    if len(det_yearly) < n_years:
        det_yearly = np.concatenate([det_yearly,
                                     np.full(n_years - len(det_yearly), det_yearly[-1])])
    ax.plot(ages, det_yearly, lw=2.0, color="#444",
            linestyle="--", label="Deterministic (after-tax median CAGR)")

    # Target line
    ax.axhline(target, color="#c00", lw=1.6, linestyle="-",
               label=f"Target ${target/1e6:.2f}M (real)")

    # Median retirement-age marker
    yrs = mc_ret["years_to_target"]
    finite = yrs[np.isfinite(yrs)]
    if len(finite) > 0:
        p50_yrs = float(np.median(finite))
        if np.isfinite(p50_yrs):
            ret_age = age_today + p50_yrs
            ax.axvline(ret_age, color="#0a0", lw=1.2, linestyle=":", alpha=0.7)
            ax.annotate(f"Median FI: age {ret_age:.1f}\n({p50_yrs:.1f} yrs)",
                        xy=(ret_age, target), xytext=(ret_age + 1.5, target * 1.15),
                        fontsize=10, color="#0a0",
                        arrowprops=dict(arrowstyle="->", color="#0a0", lw=1))

    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(
        lambda x, _: f"${x/1e6:.1f}M" if x >= 1e6 else f"${x/1e3:.0f}k"))
    ax.set_xlabel("Age")
    ax.set_ylabel("Real wealth (today's dollars, log scale)")
    ax.set_title(f"Retirement projection — ${mc_ret['starting']:,.0f} start, "
                 f"${monthly:,.0f}/mo, target ${target/1e6:.2f}M real "
                 f"(SWR {swr*100:.1f}% → ${target*swr:,.0f}/yr) "
                 f"[MC: {mc_basis} returns]",
                 fontsize=12, fontweight="bold")
    ax.grid(alpha=0.3, which="both")
    ax.legend(loc="upper left", fontsize=9)
    plt.tight_layout()
    plt.savefig(filename, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  ✓ {filename}")


def plot_rolling_sharpe_custom(sharpe_dict: dict, title: str, filename: str):
    """Plot pre-computed rolling Sharpe series. Used for the candidate-vs-aggregate
    rolling 3Y Sharpe view on the promotion-decision page."""
    fig, ax = plt.subplots(figsize=(13, 7))
    for name, sharpe_series in sharpe_dict.items():
        if sharpe_series is None or sharpe_series.empty:
            continue
        is_agg = "AGGREGATE" in name
        ax.plot(sharpe_series.index, sharpe_series.values,
                lw=2.5 if is_agg else 1.7,
                color="black" if is_agg else None,
                linestyle="-" if is_agg else "-",
                label=name, alpha=0.95 if is_agg else 0.85)
    ax.axhline(0, color="gray", lw=0.5)
    ax.axhline(0.5, color="green", lw=0.4, linestyle="--", alpha=0.5, label="Sharpe = 0.5 (quality bar)")
    ax.set_title(title, fontsize=14, fontweight="bold")
    ax.set_ylabel("Rolling 3-Year Sharpe")
    ax.grid(alpha=0.3)
    ax.legend(ncol=2, fontsize=10, loc="upper left")
    plt.tight_layout()
    plt.savefig(filename, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  ✓ {filename}")


# ═══════════════════════════════════════════════════════════════════════
# REGISTRY-DRIVEN ORCHESTRATION
# ═══════════════════════════════════════════════════════════════════════


def _resolve_strategy_fn(fn_name: str):
    """Look up a strategy backtest function by string name in the module globals."""
    fn = globals().get(fn_name)
    if fn is None or not callable(fn):
        return None
    return fn


def run_strategy(name: str, spec: dict, results: dict,
                 rets: pd.DataFrame, px: pd.DataFrame,
                 vix_df: pd.DataFrame, fed_df: pd.DataFrame,
                 weights_out: dict = None) -> bool:
    """
    Execute a strategy from its registry spec. Returns True on success.
    Writes the result series into the supplied `results` dict.

    If `weights_out` is provided AND the strategy's function supports the
    `return_weights=True` kwarg, the target-weights timeline + per-asset
    returns are also captured in `weights_out[name]`. Strategies that don't
    support it silently fall back to returns-only — no breakage.
    """
    fn = _resolve_strategy_fn(spec["fn"])
    if fn is None:
        print(f"  ✗ {name}: function '{spec['fn']}' not found")
        return False
    needs = spec.get("needs", ["returns"])
    try:
        args = []
        for n in needs:
            if n == "returns":
                args.append(rets)
            elif n == "prices":
                args.append(px)
            elif n == "vix":
                args.append(vix_df)
            elif n == "fed":
                args.append(fed_df)

        # Try to capture weights if the caller wants them and the function
        # supports it. Detect via signature inspection.
        captured_weights = None
        if weights_out is not None:
            import inspect
            try:
                params = inspect.signature(fn).parameters
                supports_weights = "return_weights" in params
            except (TypeError, ValueError):
                supports_weights = False
            if supports_weights:
                result = fn(*args, return_weights=True)
                if isinstance(result, tuple) and len(result) == 2:
                    series, captured_weights = result
                else:
                    series = result  # function ignored the kwarg
            else:
                series = fn(*args)
        else:
            series = fn(*args)

        if series is None or len(series) == 0:
            print(f"  ✗ {name}: empty result")
            return False
        # Enforce per-strategy earliest date: discard returns before the
        # registry's declared start (so warm-up / pre-coverage windows don't
        # contaminate metrics or stress-period reads).
        earliest = spec.get("earliest")
        if earliest:
            series = series.loc[pd.Timestamp(earliest):]
            if captured_weights and "weights" in captured_weights:
                cw = captured_weights["weights"]
                if cw is not None and not cw.empty:
                    captured_weights["weights"] = cw.loc[
                        cw.index >= pd.Timestamp(earliest)
                    ]
        results[name] = series
        if weights_out is not None and captured_weights is not None:
            weights_out[name] = captured_weights
        print(f"  ✓ {name}  ({_coverage_window(series)})")
        return True
    except Exception as e:
        print(f"  ✗ {name}: {e}")
        return False


def compute_partial_coverage_aggregate(results: dict, deployed_specs: dict,
                                        common_idx: pd.DatetimeIndex) -> pd.Series:
    """
    Build a daily aggregate return series using only the deployed strategies
    that have data at each date, renormalizing their allocations so the
    weights at each point in time sum to 1.0.

    This handles the fact that QQQ-based strategies (9-Sig, DM) cannot start
    before 1999-03-10, VIX-based strategies (Regime SSO/World) cannot start
    before 1990, etc. Pre-coverage their weight is redistributed pro-rata
    among the strategies that do exist.
    """
    alloc = {name: spec["alloc"] for name, spec in deployed_specs.items() if name in results}
    if not alloc:
        return pd.Series(0.0, index=common_idx)

    # Build a per-strategy availability mask aligned to common_idx.
    mat = pd.DataFrame({n: results[n].reindex(common_idx) for n in alloc}).copy()
    mask = mat.notna()  # True where the strategy has a return that date

    # Pre-compute weight matrix: alloc broadcast, masked, renormalized row-wise.
    w = pd.DataFrame(0.0, index=common_idx, columns=list(alloc.keys()))
    for n, a in alloc.items():
        w[n] = a
    w = w.where(mask, 0.0)
    row_sum = w.sum(axis=1)
    # Avoid div-by-zero on dates where NO strategy exists yet
    w = w.div(row_sum.replace(0.0, np.nan), axis=0).fillna(0.0)

    # Element-wise: weight × return, fill NaN with 0 (strategy absent), sum across columns
    agg = (mat.fillna(0.0) * w).sum(axis=1)
    return agg


# ═══════════════════════════════════════════════════════════════════════
# RETIREMENT PROJECTION
# ═══════════════════════════════════════════════════════════════════════
# Math is in REAL dollars. real_cagr = (1+after_tax_cagr)/(1+inflation) - 1.
# Monthly contributions are assumed indexed to inflation (= constant in real $).


def retirement_age_today(birth_date_str: str) -> float:
    """Current age in years, as of today."""
    birth = pd.Timestamp(birth_date_str)
    return (pd.Timestamp.now().normalize() - birth).days / 365.25


def project_wealth_deterministic(starting: float, monthly: float,
                                 annual_return: float, target: float,
                                 max_years: int = RETIREMENT_MAX_YEARS) -> dict:
    """
    Monthly-compounding forward projection in real dollars.
    Returns {"path": np.ndarray (months+1,), "years_to_target": float (inf if never),
             "final_wealth": float}.
    """
    months = max_years * 12
    monthly_r = (1.0 + annual_return) ** (1.0 / 12) - 1.0
    path = np.empty(months + 1, dtype=np.float64)
    path[0] = starting
    years_to_target = float("inf")
    for m in range(1, months + 1):
        path[m] = path[m - 1] * (1.0 + monthly_r) + monthly
        if years_to_target == float("inf") and path[m] >= target:
            years_to_target = m / 12.0
    return {"path": path, "years_to_target": years_to_target,
            "final_wealth": float(path[-1])}


def monte_carlo_retirement(aggregate_returns: pd.Series, starting: float,
                            monthly: float, target: float, inflation: float,
                            max_years: int = RETIREMENT_MAX_YEARS,
                            n_sims: int = RETIREMENT_MC_SIMS,
                            mean_block: int = 63, seed: int = 42) -> dict:
    """
    Stationary block bootstrap of forward wealth paths in REAL dollars.

    For each sim: resample max_years*252 daily nominal returns from the
    aggregate's native window, deflate each to a real return, compound
    forward, drop in a monthly contribution every 21 trading days.

    Returns:
      paths_yearly: (n_sims, max_years+1) wealth at end of each year (year 0 = starting)
      years_to_target: (n_sims,) first year wealth crosses target (np.inf if never)
      starting, monthly, target, inflation, max_years, n_sims  (echoed for plotting)
    """
    ret = aggregate_returns.dropna()
    # Trim leading zeros (pre-coverage of any strategy)
    nz = ret[ret != 0]
    if len(nz) > 0:
        ret = ret.loc[nz.index[0]:]
    if len(ret) < 252:
        return None  # not enough history

    rng = np.random.default_rng(seed)
    nominal = ret.values
    n_source = len(nominal)

    infl_daily = (1.0 + inflation) ** (1.0 / 252) - 1.0
    # Convert nominal daily returns → real daily returns once, up front
    real_daily = (1.0 + nominal) / (1.0 + infl_daily) - 1.0

    days = max_years * 252
    monthly_step = 21  # ~21 trading days per month
    p_block = 1.0 / mean_block

    paths_yearly = np.empty((n_sims, max_years + 1), dtype=np.float64)
    paths_yearly[:, 0] = starting
    years_to_target = np.full(n_sims, np.inf, dtype=np.float64)

    # Inline stationary bootstrap into [0, n_source) for `days` output steps.
    # We can't reuse stationary_bootstrap_indices() here because it uses one
    # `n` for both source range and output length.
    def _boot(out_len: int, src_n: int):
        idx = np.empty(out_len, dtype=np.int64)
        idx[0] = rng.integers(0, src_n)
        coin = rng.random(out_len) < p_block
        rand_idx = rng.integers(0, src_n, size=out_len)
        for t in range(1, out_len):
            if coin[t]:
                idx[t] = rand_idx[t]
            else:
                idx[t] = (idx[t - 1] + 1) % src_n
        return idx

    for sim in range(n_sims):
        idx = _boot(days, n_source)
        sim_r = real_daily[idx]
        wealth = starting
        crossed = False
        for d in range(1, days + 1):
            wealth = wealth * (1.0 + sim_r[d - 1])
            if d % monthly_step == 0:
                wealth += monthly
            if (not crossed) and wealth >= target:
                years_to_target[sim] = d / 252.0
                crossed = True
            if d % 252 == 0:
                paths_yearly[sim, d // 252] = wealth

    return {
        "paths_yearly": paths_yearly,
        "years_to_target": years_to_target,
        "starting": starting,
        "monthly": monthly,
        "target": target,
        "inflation": inflation,
        "max_years": max_years,
        "n_sims": n_sims,
    }


def retirement_age_probability_grid(mc_ret: dict, age_today: float,
                                    age_buckets: list = None,
                                    target_scenarios: list = None) -> pd.DataFrame:
    """
    Probability of reaching each target by each age. Re-uses paths_yearly so
    we don't have to re-simulate per target — for targets we didn't run, we
    derive the crossing year from the same paths.
    """
    age_buckets = age_buckets or RETIREMENT_AGE_BUCKETS
    target_scenarios = target_scenarios or RETIREMENT_TARGET_SCENARIOS
    paths = mc_ret["paths_yearly"]  # (n_sims, max_years+1)
    n_sims, n_years_plus_1 = paths.shape
    max_year_idx = n_years_plus_1 - 1

    rows = []
    for age in age_buckets:
        years_from_now = age - age_today
        if years_from_now <= 0:
            row = {"age": age}
            for t in target_scenarios:
                row[t] = 1.0 if mc_ret["starting"] >= t else 0.0
            rows.append(row)
            continue
        year_idx = min(int(np.ceil(years_from_now)), max_year_idx)
        row = {"age": age}
        for t in target_scenarios:
            # P(wealth at year_idx >= target) — running max over years to
            # account for the possibility that wealth peaked then dipped.
            running_max = np.maximum.accumulate(paths[:, :year_idx + 1], axis=1)
            prob = float((running_max[:, year_idx] >= t).mean())
            row[t] = prob
        rows.append(row)
    return pd.DataFrame(rows).set_index("age")


def retirement_sensitivity_grid(starting: float, annual_return: float,
                                age_today: float,
                                monthlies: list = None,
                                targets: list = None,
                                max_years: int = RETIREMENT_MAX_YEARS) -> pd.DataFrame:
    """
    Deterministic monthly × target grid. Each cell: years to reach target
    (and the corresponding age). Returns a DataFrame indexed by monthly,
    columns are targets, values are (years, age) tuples.
    """
    monthlies = monthlies or RETIREMENT_MONTHLY_SCENARIOS
    targets = targets or RETIREMENT_TARGET_SCENARIOS
    rows = []
    for m in monthlies:
        row = {"monthly": m}
        for t in targets:
            res = project_wealth_deterministic(starting, m, annual_return, t, max_years)
            row[t] = res["years_to_target"]
        rows.append(row)
    return pd.DataFrame(rows).set_index("monthly")


# ═══════════════════════════════════════════════════════════════════════
# UNIFIED HTML REPORT
# ═══════════════════════════════════════════════════════════════════════


def _coverage_window(series: pd.Series) -> str:
    s = series.dropna()
    if len(s) == 0:
        return "—"
    return f"{s.index[0].date()} → {s.index[-1].date()}"


def _years(series: pd.Series) -> float:
    s = series.dropna()
    if len(s) < 2:
        return 0.0
    return len(s) / 252.0


def _native_window_metrics(series: pd.Series, earliest: str) -> dict:
    """Compute metrics on the strategy's NATIVE window (≥ its earliest date).
    Used to compare strategies on their own coverage rather than the common
    1970-2026 window, which unfairly penalises later-starting strategies."""
    if series is None or earliest is None:
        return {}
    s = series.loc[pd.Timestamp(earliest):].dropna()
    if len(s) < 60:
        return {}
    return compute_metrics(s)


def _quality_badge(q: str) -> str:
    """Coloured pill for a quality tier letter."""
    colors = {"A": "#0a0", "B": "#7a3", "C": "#c80", "D": "#c00"}
    c = colors.get(q, "#888")
    return f"<span style='background:{c};color:white;padding:2px 7px;border-radius:8px;font-size:0.85em;font-weight:600'>{q}</span>"


# ═══════════════════════════════════════════════════════════════════════
# TICKER GLOSSARY — used by the explainer cards + the glossary section
# ═══════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════
# DEPLOYED-TICKER USAGE MAP — for production-overlap highlighting on cards
# Maps each ticker held by a DEPLOYED strategy to (sleeve_name, allocation
# pattern, severity). Severity drives the badge color:
#   "fixed":         always-held — direct double-counting if a candidate also holds it
#   "rotation":      part of a rotation universe — sometimes held
#   "defensive":     held only when the deployed sleeve is risk-off
# ═══════════════════════════════════════════════════════════════════════
DEPLOYED_TICKER_USAGE = {
    # HFEA always-held legs
    "UPRO": [("HFEA", "45% fixed", "fixed")],
    "TMF":  [("HFEA", "25% fixed", "fixed")],
    "KMLM": [("HFEA", "30% fixed", "fixed")],
    # SPXL SMA
    "SPXL": [("SPXL SMA", "100% when in-market", "fixed")],
    # 9-Sig
    "TQQQ": [("9-Sig", "majority allocation", "fixed")],
    "AGG":  [("9-Sig", "cash buffer", "fixed")],
    # DM 2× best-of-3 rotation universe
    "SPUU": [("DM 2× best-of-3", "selected when SPY momentum wins", "rotation")],
    "QLD":  [("DM 2× best-of-3", "selected when QQQ momentum wins", "rotation")],
    "EFO":  [("DM 2× best-of-3", "selected when EFA momentum wins", "rotation")],
    # Regime SSO
    "SSO":  [("Regime SSO", "100% when in-market", "fixed")],
    "USFR": [("Regime SSO", "100% when defensive", "defensive")],
    # AAA Free 2× + NTSD rotation universe (top-3 from 7 by 6m momentum)
    "NTSD": [("7-Asset Rotator", "1 of 7 universe assets, top-3 selected", "rotation")],
    "SAA":  [("7-Asset Rotator", "1 of 7 universe assets, top-3 selected", "rotation")],
    "EET":  [("7-Asset Rotator", "1 of 7 universe assets, top-3 selected", "rotation")],
    "UBT":  [("7-Asset Rotator", "1 of 7 universe assets, top-3 selected", "rotation")],
    "UST":  [("7-Asset Rotator", "1 of 7 universe assets, top-3 selected", "rotation")],
    "UGL":  [("7-Asset Rotator", "1 of 7 universe assets, top-3 selected", "rotation")],
    "DBC":  [("7-Asset Rotator", "1 of 7 universe assets, top-3 selected", "rotation")],
    # Shared defensive cash
    "SHV":  [("DM 2× best-of-3 + AAA Free 2×", "DD/vol-target defensive cash", "defensive")],
}


TICKER_GLOSSARY = {
    # ── Equity 1× ETFs ──
    "SPY":  ("iShares Core S&P 500", "Largest US large-cap index ETF; tracks the S&P 500. Baseline US equity exposure."),
    "QQQ":  ("Invesco Nasdaq-100", "Tracks Nasdaq-100 (top 100 non-financial Nasdaq stocks). Tech-heavy growth."),
    "EFA":  ("iShares MSCI EAFE", "Developed-market international equity (Europe, Australasia, Far East); excludes US + emerging."),
    "EEM":  ("iShares MSCI Emerging Markets", "Emerging-market equity exposure (China, India, Brazil, etc.)."),
    "IWM":  ("iShares Russell 2000", "US small-cap index — broader exposure than SPY's large-caps."),
    "URTH": ("iShares MSCI World", "Global developed-market equity (US ~70% + intl ~30%)."),
    "VT":   ("Vanguard Total World Stock", "Global all-cap equity (developed + emerging)."),
    # ── Leveraged equity ──
    "UPRO": ("ProShares UltraPro S&P 500", "3× daily S&P 500. Used in HFEA. ER 0.91%."),
    "SPXL": ("Direxion Daily S&P 500 Bull 3X", "3× daily S&P 500, same family as UPRO. ER 0.91%."),
    "TQQQ": ("ProShares UltraPro QQQ", "3× daily Nasdaq-100. ER 0.86%."),
    "SSO":  ("ProShares Ultra S&P 500", "2× daily S&P 500. ER 0.89%."),
    "SPUU": ("Direxion Daily S&P 500 Bull 2X", "2× daily S&P 500. Cheaper than SSO (ER 0.64%)."),
    "QLD":  ("ProShares Ultra QQQ", "2× daily Nasdaq-100. ER 0.95%."),
    "EFO":  ("ProShares Ultra MSCI EAFE", "2× daily EFA. ER 0.95%."),
    "EET":  ("ProShares Ultra MSCI Emerging Mkts", "2× daily EEM. ER 0.95%."),
    "SAA":  ("ProShares Ultra Russell 2000", "2× daily IWM. ER 0.95%."),
    "TNA":  ("Direxion Daily Small Cap Bull 3X", "3× daily Russell 2000. ER 0.94%."),
    "EDC":  ("Direxion Daily MSCI EM Bull 3X", "3× daily Emerging Markets. ER 0.98%."),
    # ── Bonds 1× ──
    "TLT":  ("iShares 20+ Year Treasury", "Long-duration US Treasuries (20+ year). Deflation/recession hedge."),
    "IEF":  ("iShares 7-10 Year Treasury", "Intermediate-duration US Treasuries. Cleaner stocks/bonds correlation."),
    "BND":  ("Vanguard Total Bond Market", "Total US investment-grade bond market (Treasuries + corp + mortgage)."),
    "AGG":  ("iShares Core US Aggregate Bond", "Same Bloomberg Agg exposure as BND; functionally equivalent."),
    "SHV":  ("iShares Short Treasury Bond", "0-1 year T-bills. Cash equivalent."),
    "SHY":  ("iShares 1-3 Year Treasury", "1-3 year T-bills. Cash equivalent."),
    "USFR": ("WisdomTree Floating Rate Treasury", "Floating-rate T-bills — protected from rate-rise duration risk."),
    "SGOV": ("iShares 0-3 Month Treasury", "Newest, lowest-cost ultra-short T-bill ETF."),
    "BIL":  ("SPDR Bloomberg 1-3 Month T-Bill", "1-3 month T-bill ETF. Pure cash proxy."),
    # ── Leveraged bonds ──
    "TMF":  ("Direxion Daily 20+ Year Treasury Bull 3X", "3× daily TLT. Used in HFEA. ER 1.05%."),
    "UBT":  ("ProShares Ultra 20+ Year Treasury", "2× daily TLT. ER 0.94%."),
    "UST":  ("ProShares Ultra 7-10 Year Treasury", "2× daily IEF. ER 0.94%."),
    "TYD":  ("Direxion Daily 7-10 Year Treasury Bull 3X", "3× daily IEF. ER 0.94%."),
    "EDV":  ("Vanguard Extended Duration Treasury", "Zero-coupon US Treasuries with ~25y duration. No daily-reset leverage decay (unleveraged) but ~40% more duration than TLT due to longer maturity. ER 0.06%."),
    # ── Real assets ──
    "GLD":  ("SPDR Gold Shares", "Spot gold. Inflation hedge + crisis-flight asset."),
    "UGL":  ("ProShares Ultra Gold", "2× daily GLD. ER 0.95%."),
    "SLV":  ("iShares Silver Trust", "Spot silver. More volatile + industrial-tilted than gold."),
    "DBC":  ("Invesco DB Commodity Index", "Diversified commodity basket (energy, metals, agriculture)."),
    # ── Managed futures ──
    "KMLM": ("KFA Mount Lucas Managed Futures", "Systematic trend-following across rates/FX/commodities. Crisis-alpha asset."),
    "DBMF": ("iMGP DBi Managed Futures Strategy", "Similar trend-following methodology to KMLM."),
    # ── Capital-efficient stacks ──
    "NTSD": ("WisdomTree US Plus Intl Equity", "90% direct US stocks + 60% notional EAFE futures = 150% total exposure on $1 capital."),
    "NTSX": ("WisdomTree US Efficient Core", "90% US stocks + 60% US Treasury futures = 150% capital efficient."),
    "NTSI": ("WisdomTree Intl Efficient Core", "90% intl-developed equity (VEA) + 60% Treasury futures = 150% capital efficient. Intl analog of NTSX. Live 2022-08. ER 0.26%."),
    "GDE":  ("WisdomTree Efficient Gold Plus Equity", "90% stocks + 90% gold futures = 180% capital efficient."),
    "GDT":  ("Granite Gold + TIPS", "90% short-term TIPS (STIP) + 90% gold futures = 180% capital efficient. TIPS-stacked gold for inflation defense. Live 2024-09."),
    "RSSB": ("Return Stacked Global Stocks + Bonds", "100% global stocks + 100% bonds via futures = 200% efficient."),
    "RSIT": ("Return Stacked Global Stocks + MF", "100% VT (global stocks) + 100% managed futures via futures = 200% efficient. Live 2024. ER 0.97%."),
    "GOLY": ("Quantify Gold + MF + Corp Bonds", "50% gold + 50% CTA (managed futures) + 100% corp bonds = 200% efficient triple-stack. Live 2025-04. ER 0.50% (estimated)."),
    "WTIP": ("WisdomTree Inflation Plus Fund", "Multi-asset: TIPS + managed futures + commodities + BTC."),
}


# ═══════════════════════════════════════════════════════════════════════
# STRATEGY EXPLAINERS — detailed write-up per strategy (deployed + candidate)
# Keys are the bt_* function names. Each entry has:
#   - kind: type of strategy (passive / tactical / trend-managed / momentum)
#   - holdings: list of (ticker, weight_or_role, comment) for the assets held
#   - mechanism: 1-2 sentence plain-English description of what it does
#   - signal: how positions change (if tactical)
#   - rebal: rebalancing cadence / triggers
#   - wins: scenarios where this outperforms
#   - loses: scenarios where it underperforms
#   - source: academic paper / community ref
# ═══════════════════════════════════════════════════════════════════════

STRATEGY_EXPLAINERS = {
    # ── DEPLOYED ──
    "bt_hfea": {
        "kind": "Passive leveraged rebalance (modern HFEA)",
        "holdings": [
            ("UPRO", "45%", "3× S&P 500 — leveraged equity for capital appreciation"),
            ("TMF",  "25%", "3× 20+yr Treasuries — leveraged duration for deflation hedge"),
            ("KMLM", "30%", "Managed futures — trend-following across rates/FX/commodities, low correlation"),
        ],
        "mechanism": "Hold a fixed 45/25/30 UPRO/TMF/KMLM split; rebalance back to target each quarter. No tactical signal.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly (1st business day of each calendar quarter).",
        "wins": "Sustained bulls with falling rates (UPRO + TMF both rally) AND trending crises where managed futures profit (2008, 2022 oil/rates).",
        "loses": "Choppy sideways markets (leverage decay on UPRO/TMF) and rapid rate rises that hurt stocks AND bonds (2022 H1).",
        "source": "Hedgefundie 2019 (Boglehead forum, classic 55/45 UPRO/TMF) + KMLM modern variant.",
    },
    "bt_spxl_sma": {
        "kind": "Trend-following risk-managed",
        "holdings": [
            ("SPXL", "100% when bullish", "3× S&P 500 — equivalent to UPRO from a different issuer"),
            ("SGOV", "100% when bearish", "Short T-bills — capital preservation in cash"),
        ],
        "mechanism": "Hold 3× SPY when SPY price is above its 200-day SMA × 1.01; otherwise hold cash. Daily signal evaluation.",
        "signal": "Bullish: SPY > 200-SMA × 1.01 (1% buffer above). Bearish: SPY < 200-SMA × 0.99. Hysteresis prevents whipsaw.",
        "rebal": "Continuous — switches between SPXL and SGOV on signal crossover.",
        "wins": "Sustained bull markets (3× SPY captures upside); avoids long bear markets (gate triggers cash before deep drawdown).",
        "loses": "Choppy markets where the 200-SMA whips (sell at low, buy back at high); first leg of a sharp crash before the gate triggers.",
        "source": "Meb Faber (2007) trend-following + Direxion SPXL.",
    },
    "bt_nine_sig": {
        "kind": "Systematic signal-driven rebalance",
        "holdings": [
            ("TQQQ", "80% target", "3× Nasdaq-100 — leveraged tech-heavy growth"),
            ("AGG",  "20% target", "Total US bonds — defensive ballast"),
        ],
        "mechanism": "Quarterly check whether TQQQ has grown by the target 9% per quarter. If above 9%, sell TQQQ down to target weight; if below, buy from AGG. AGG never sold (acts as one-way refill source).",
        "signal": "Quarterly 9% TQQQ growth target. Excess → AGG (buy bonds). Shortfall → buy more TQQQ from AGG (averaging down).",
        "rebal": "Quarterly with 9% growth signal. Crash filter (omitted in our backtest) would halt buying during deep TQQQ drawdowns.",
        "wins": "Tech-led bull markets (Nasdaq compounding at 3×) with periodic mean reversion (sell winners).",
        "loses": "Sustained Nasdaq bears (3× drawdown unbounded; -93% peak-to-trough during dot-com 2000-2002).",
        "source": "Jason Kelly, *The 3% Signal* (2015) — 9-Sig is the leveraged TQQQ variant.",
    },
    "bt_dm_2x_best_of_3_dd30_vol25": {
        "kind": "Tactical multi-asset momentum rotation",
        "holdings": [
            ("SPUU", "rotation candidate", "2× S&P 500 (US large-cap exposure)"),
            ("QLD",  "rotation candidate", "2× Nasdaq-100 (US tech exposure)"),
            ("EFO",  "rotation candidate", "2× MSCI EAFE (intl developed exposure)"),
            ("BND",  "defensive parking", "Total US bonds when no candidate qualifies"),
        ],
        "mechanism": "Each month, pick the candidate (SPY/QQQ/EFA) with the strongest blended momentum (6m + 12m, skip last month). Hold the corresponding 2× ETF, vol-scaled to a 25% target.",
        "signal": "Blended momentum: 0.5 × 6-month return + 0.5 × 12-month return (Jegadeesh-Titman skip-1m). Pick highest; if all < 1% threshold, all-defensive.",
        "rebal": "Monthly signal evaluation. Vol-target scaling adjusts position size daily. Trailing -30% peak-NAV drawdown forces all-defensive.",
        "wins": "Trending markets (momentum signal captures the leader); volatility regime changes (vol-target prevents over-exposure).",
        "loses": "Rapid trend reversals (signal lag means buying just before a reversal); coordinated equity bears across all 3 universes.",
        "source": "Antonacci (2014) Dual Momentum + Pedersen (2013) volatility targeting + custom 2× ETF rotation.",
    },
    "bt_regime_sso": {
        "kind": "Multi-signal regime detector",
        "holdings": [
            ("SSO",  "100% when bullish", "2× S&P 500 — leveraged risk-on exposure"),
            ("USFR", "100% when defensive", "Floating-rate Treasuries — duration-protected cash"),
        ],
        "mechanism": "7-signal composite scores market regime daily; switch between SSO (risk-on) and USFR (risk-off) based on composite + entry/exit rules.",
        "signal": "Signals scored ±1: SPY vs 200-SMA, market breadth, VIX level + 5d-change, ADX trend strength, credit spread (HYG/LQD), canary (EM/IWM), Fed hike trajectory. Aggregate score triggers state changes with hysteresis.",
        "rebal": "Continuous. Exit conditions: composite ≤ -3 for 3 days (fast) or ≤ 0 for 15 days (slow). Re-entry: composite ≥ +3 for 15 days. Fed hike filter blocks re-entry.",
        "wins": "Clear regime changes (catches both directions). Best Sharpe of all deployed strategies (0.68 native).",
        "loses": "Choppy regime transitions; very low MaxDD (-23.7%) suggests it sometimes goes defensive too early.",
        "source": "Custom 7-signal composite (extended Faber 2007 + breadth + credit + canary).",
    },
    "bt_regime_world": {
        "kind": "Multi-signal regime detector (global)",
        "holdings": [
            ("WLDU", "100% when bullish", "2× MSCI World — synthetic since no real 2× world ETF exists"),
            ("USFR", "100% when defensive", "Same cash sleeve as Regime SSO"),
        ],
        "mechanism": "Same 7-signal engine as Regime SSO but applied to URTH (MSCI World) with a 255-day SMA (longer than the 200-day used for SPY). Risk asset is WLDU = 2× URTH (synthetic).",
        "signal": "Same logic as Regime SSO with URTH-based SMA and momentum.",
        "rebal": "Continuous, same triggers as Regime SSO.",
        "wins": "Global trend changes (catches non-US-led regime shifts).",
        "loses": "WLDU is synthetic — real returns may differ slightly. Sometimes WLDU is more aggressive than SSO due to lower MSCI World vs SPY vol.",
        "source": "Custom — same engine as Regime SSO with global parameters.",
    },

    # ── CANDIDATES ──
    "bt_ntsd_ubt_ugl_dbc_3x_overlay": {  # Bronze
        "kind": "NTSD-core Adaptive Asset Allocation",
        "holdings": [
            ("NTSD", "rotation candidate", "WisdomTree 90/60 US+EAFE — capital-efficient stack"),
            ("UBT",  "rotation candidate", "2× 20+y Treasuries — bond leg"),
            ("UGL",  "rotation candidate", "2× gold — inflation/crisis hedge"),
            ("DBC",  "rotation candidate", "Commodity basket — inflation hedge"),
            ("SHV",  "parking", "Short Treasury cash when DD-stop fires"),
        ],
        "mechanism": "Each month, pick the top-2 of {NTSD, UBT, UGL, DBC} by 6m momentum. Weight by inverse-volatility within picks; scale total exposure to 20% annual vol target.",
        "signal": "6-month price momentum; positive-only picks. Trailing -25% peak-NAV DD-stop forces SHV cash until next monthly rebalance.",
        "rebal": "Monthly; vol-target daily; DD-stop continuous.",
        "wins": "Crisis-alpha periods (gold/commodities/bonds rally while stocks fall). Best risk-adjusted MaxDD (-26.85%) of all candidates.",
        "loses": "Sustained bull markets where rotating into bonds/gold misses equity upside.",
        "source": "Butler-Philbrick-Gordillo (2012) Adaptive Asset Allocation + NTSD core wrapper.",
    },
    "bt_dm_2x_best_of_4_gold_dd30_vol25": {
        "kind": "Tactical multi-asset momentum + gold",
        "holdings": [
            ("SPUU", "rotation candidate", "2× US S&P 500"),
            ("QLD",  "rotation candidate", "2× Nasdaq-100"),
            ("EFO",  "rotation candidate", "2× MSCI EAFE"),
            ("GLD",  "rotation candidate", "1× gold (no leveraged version used here)"),
            ("BND",  "defensive parking", "Bonds when no candidate qualifies"),
        ],
        "mechanism": "Same as production DM 2× best-of-3, but adds GLD as a fourth rotation candidate. Allows momentum to flow into gold during equity stress.",
        "signal": "Same blended momentum as production DM (6m + 12m, skip-1m).",
        "rebal": "Monthly with vol-target + DD-stop.",
        "wins": "Equity stress periods where gold rallies; production DM would force cash, this rotates into GLD.",
        "loses": "Sustained equity bulls (gold lags); commodity bears.",
        "source": "Custom extension of production DM adding gold rotation slot.",
    },
    "bt_dm_2x_best_of_5_gold_dd30_vol25": {
        "kind": "Tactical multi-asset momentum + small-cap + gold",
        "holdings": [
            ("SPUU", "rotation candidate", "2× US S&P 500"),
            ("QLD",  "rotation candidate", "2× Nasdaq-100"),
            ("EFO",  "rotation candidate", "2× MSCI EAFE"),
            ("SAA",  "rotation candidate", "2× Russell 2000 small-cap"),
            ("GLD",  "rotation candidate", "1× gold"),
            ("BND",  "defensive parking", "Bonds defensive"),
        ],
        "mechanism": "Like #1 above but adds small-cap (SAA) as a 5th rotation slot. Broadest equity geographic + cap coverage.",
        "signal": "Same.",
        "rebal": "Same.",
        "wins": "Small-cap rallies + gold rallies + intl rallies all covered; very wide universe.",
        "loses": "Universe complexity may dilute signal (more candidates = more whipsaw potential).",
        "source": "Same as #1 with universe extension.",
    },
    "bt_aaa_free_2x_plus_ntsd": {
        "kind": "Adaptive Asset Allocation with NTSD core",
        "holdings": [
            ("NTSD", "rotation candidate", "Capital-efficient 90/60 US+EAFE"),
            ("SAA",  "rotation candidate", "2× US small-cap"),
            ("EET",  "rotation candidate", "2× emerging markets"),
            ("UBT",  "rotation candidate", "2× LT Treasuries"),
            ("UST",  "rotation candidate", "2× IT Treasuries"),
            ("UGL",  "rotation candidate", "2× gold"),
            ("DBC",  "rotation candidate", "Commodity basket"),
        ],
        "mechanism": "Top-3 of {NTSD, SAA, EET, UBT, UST, UGL, DBC} by momentum, inverse-vol weighted, DD30 + vol25. Wider universe than Bronze, no leverage cap exceeded.",
        "signal": "Same momentum + DD-stop + vol-target as Bronze.",
        "rebal": "Same.",
        "wins": "Wider rotation universe → better adaptation to regime changes. Best CAGR×Sharpe×Calmar of any candidate (-28.7% MaxDD with 15.97% CAGR).",
        "loses": "Tickers like SAA/EET have higher MaxDD individually; concentration risk if 2 underperforming candidates win the top-3 vote.",
        "source": "Butler-Philbrick-Gordillo (2012) AAA + Bronze-style NTSD inclusion.",
    },
    "bt_dm_2x_best_of_3_dd30_vol25": {  # predecessor - same fn as deployed but slightly different config history
        "kind": "Tactical multi-asset momentum (production DM predecessor)",
        "holdings": [
            ("SPUU", "rotation candidate", "2× US S&P 500"),
            ("QLD",  "rotation candidate", "2× Nasdaq-100"),
            ("EFO",  "rotation candidate", "2× MSCI EAFE"),
            ("BND",  "defensive parking", "Bonds when no candidate qualifies"),
        ],
        "mechanism": "Same configuration as production DM. Listed as candidate to track the predecessor history.",
        "signal": "Same.",
        "rebal": "Same.",
        "wins": "Same as production DM.",
        "loses": "Same as production DM.",
        "source": "Same — kept as historical comparison anchor.",
    },
    "bt_aaa_free_3x_plus_ntsd": {
        "kind": "Adaptive Asset Allocation with NTSD core (3× sleeves)",
        "holdings": [
            ("NTSD", "rotation candidate", "Capital-efficient 90/60 US+EAFE"),
            ("TNA",  "rotation candidate", "3× US small-cap"),
            ("EDC",  "rotation candidate", "3× emerging markets"),
            ("TYD",  "rotation candidate", "3× IT Treasuries"),
            ("UGL",  "rotation candidate", "2× gold"),
            ("DBC",  "rotation candidate", "Commodity basket"),
        ],
        "mechanism": "Same as AAA Free 2× + NTSD but uses 3× ETFs where available. More aggressive leverage profile.",
        "signal": "Same.",
        "rebal": "Same.",
        "wins": "Same scenarios as #4 but amplified upside.",
        "loses": "Higher leverage = bigger drawdowns when picks are wrong; exceeds ≤2× leverage rule.",
        "source": "Same as AAA Free 2× + NTSD with leverage upgrade.",
    },
    "bt_adaptive_asset_allocation_levered": {
        "kind": "Adaptive Asset Allocation with 2× sleeves",
        "holdings": [
            ("SPUU", "rotation candidate", "2× US S&P 500"),
            ("EFO",  "rotation candidate", "2× MSCI EAFE"),
            ("UBT",  "rotation candidate", "2× LT Treasuries"),
            ("UGL",  "rotation candidate", "2× gold"),
            ("DBC",  "rotation candidate", "Commodity basket"),
        ],
        "mechanism": "Original BPG 2012 AAA scaled to 2× exposure; top-3 momentum + inverse-vol + DD30 + vol25.",
        "signal": "6-month momentum + vol-targeting + DD-stop.",
        "rebal": "Monthly.",
        "wins": "Multi-asset regime captures (similar to Bronze).",
        "loses": "Uses SPUU/EFO which conflict with deployed DM — concentration risk if both ran.",
        "source": "Butler-Philbrick-Gordillo (2012) AAA with 2× leverage overlay.",
    },
    "bt_dm_2x_best_of_4_dd30_vol25": {
        "kind": "Tactical multi-asset momentum + small-cap",
        "holdings": [
            ("SPUU", "rotation candidate", "2× US S&P 500"),
            ("QLD",  "rotation candidate", "2× Nasdaq-100"),
            ("EFO",  "rotation candidate", "2× MSCI EAFE"),
            ("SAA",  "rotation candidate", "2× US small-cap"),
            ("BND",  "defensive parking", "Bonds defensive"),
        ],
        "mechanism": "Production DM + small-cap (SAA) rotation candidate. Adds US small-cap exposure.",
        "signal": "Same momentum signal.",
        "rebal": "Same.",
        "wins": "Small-cap rallies (e.g., 2017 small-cap surge).",
        "loses": "Small-cap underperformance years (most of 2014-2019).",
        "source": "Custom extension of production DM.",
    },
    "bt_dm_2x_best_of_5_dd30_vol25": {
        "kind": "Tactical multi-asset momentum + small-cap + EM",
        "holdings": [
            ("SPUU", "rotation candidate", "2× US S&P 500"),
            ("QLD",  "rotation candidate", "2× Nasdaq-100"),
            ("EFO",  "rotation candidate", "2× MSCI EAFE"),
            ("SAA",  "rotation candidate", "2× US small-cap"),
            ("EET",  "rotation candidate", "2× emerging markets"),
            ("BND",  "defensive parking", "Bonds defensive"),
        ],
        "mechanism": "Production DM + small-cap + EM rotation candidates (5 total).",
        "signal": "Same.",
        "rebal": "Same.",
        "wins": "Broadest equity universe coverage; catches EM rallies (e.g., 2003-2007 EM bull).",
        "loses": "Same dilution concern — more candidates = more whipsaw.",
        "source": "Custom extension.",
    },
    "bt_dm_2x_best_of_3_dd30_vol30": {
        "kind": "Tactical multi-asset momentum (looser vol target)",
        "holdings": [
            ("SPUU", "rotation candidate", "2× US S&P 500"),
            ("QLD",  "rotation candidate", "2× Nasdaq-100"),
            ("EFO",  "rotation candidate", "2× MSCI EAFE"),
            ("BND",  "defensive parking", "Bonds defensive"),
        ],
        "mechanism": "Same as production DM but 30% vol target instead of 25%. Higher exposure during low-vol periods.",
        "signal": "Same.",
        "rebal": "Same.",
        "wins": "Sustained bulls (higher exposure captures more upside).",
        "loses": "Wider drawdowns (less vol-target damping).",
        "source": "Custom — vol-target sensitivity test.",
    },
    "bt_ntsd_core_satellite": {  # Gold
        "kind": "Core-satellite (NTSD core + tactical satellite)",
        "holdings": [
            ("NTSD", "60% core (always held)", "Capital-efficient 90/60 US+EAFE"),
            ("UBT",  "40% satellite candidate", "2× LT Treasuries"),
            ("UGL",  "40% satellite candidate", "2× gold"),
            ("DBC",  "40% satellite candidate", "Commodity basket"),
        ],
        "mechanism": "Always hold 60% NTSD as core. Remaining 40% rotates monthly into the top-1 of {UBT, UGL, DBC} by momentum (Antonacci single-best).",
        "signal": "6-month momentum on satellite candidates. NTSD core never rotated.",
        "rebal": "Monthly satellite rotation.",
        "wins": "Captures equity beta via NTSD core + diversifier kicker from satellite.",
        "loses": "60% NTSD has no DD-stop → wider MaxDD (-48.5%) than Bronze. No risk overlay on core.",
        "source": "CAPM core-satellite + Antonacci single-best satellite.",
    },
    "bt_ntsd_cross_asset_dm": {
        "kind": "Antonacci cross-asset dual momentum",
        "holdings": [
            ("NTSD", "rotation candidate", "Capital-efficient 90/60 US+EAFE"),
            ("UBT",  "rotation candidate", "2× LT Treasuries"),
            ("UGL",  "rotation candidate", "2× gold"),
            ("DBC",  "rotation candidate", "Commodity basket"),
        ],
        "mechanism": "Single-best rotation across {NTSD, UBT, UGL, DBC} by 12-month momentum. Pure 1-asset bet at any time.",
        "signal": "12m absolute momentum; winner only if score > 0, else cash.",
        "rebal": "Monthly.",
        "wins": "Strong-trend regimes where one asset clearly dominates.",
        "loses": "Choppy / multi-asset regimes where no clear leader exists.",
        "source": "Antonacci (2014) Dual Momentum — multi-asset version.",
    },
    "bt_leveraged_all_weather": {
        "kind": "Leveraged Dalio All-Weather",
        "holdings": [
            ("UPRO", "25%", "3× S&P 500 — equity"),
            ("UBT",  "25%", "2× LT Treasuries — deflation hedge"),
            ("UGL",  "25%", "2× gold — inflation hedge"),
            ("DBC",  "25%", "Commodities — growth/inflation hedge"),
        ],
        "mechanism": "Hold 25% in each of UPRO/UBT/UGL/DBC. 4-regime risk-parity philosophy (growth/inflation/recession/deflation), scaled up with leverage.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Crisis-alpha across regimes (something always works).",
        "loses": "Coordinated stress periods (2022 H1 stocks+bonds both down); fixed weights ignore relative strength.",
        "source": "Dalio Bridgewater All-Weather (1990s) — leveraged variant.",
    },

    # ─── CANDIDATE SHORTLIST (Wave 5/6) — WLDU-based intl diversifiers ───
    "bt_w5_c9_wldu_tyd_kmlm": {
        "kind": "Passive 3-asset leveraged blend (WLDU-based, intl tilt)",
        "holdings": [
            ("WLDU", "50%", "2× MSCI World — leveraged intl equity (60% US + 40% intl developed)"),
            ("TYD",  "30%", "3× 7-10y Treasuries — leveraged intermediate duration"),
            ("KMLM", "20%", "Managed futures — crisis-alpha via trend-following ⚠ also in deployed HFEA (30%)"),
        ],
        "mechanism": "Static 50/30/20 allocation; quarterly rebalance back to target. WLDU is the equity engine, TYD provides leveraged duration without going to the long end of the curve, KMLM provides uncorrelated crisis-alpha.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly (1st business day).",
        "wins": "Mid-rate-cycle bulls with intl outperformance (WLDU + TYD both rally); trending crises where KMLM profits (2008, 2022 oil/rates).",
        "loses": "US-dominant bulls (WLDU underperforms SPY-only sleeves); choppy ranges with whipsaws in KMLM trend signals.",
        "source": "Wave 5 candidate, 2026-05-12. Baseline benchmark for the WLDU shortlist.",
    },
    "bt_w6_d5_wldu_edv_tyd_kmlm": {
        "kind": "Passive 4-asset leveraged blend with duration barbell",
        "holdings": [
            ("WLDU", "40%", "2× MSCI World — leveraged intl equity"),
            ("EDV",  "25%", "Zero-coupon ~25y Treasury — long-end duration without leverage decay"),
            ("TYD",  "15%", "3× 7-10y Treasuries — leveraged intermediate duration"),
            ("KMLM", "20%", "Managed futures ⚠ also in deployed HFEA (30%)"),
        ],
        "mechanism": "Duration barbell: long-end zero-coupon (EDV) + intermediate-3× (TYD) at two distinct points on the curve. Lower equity allocation than C9 (40 vs 50) trades off return for tighter drawdown.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Falling-rate regimes (both duration sleeves rally); curve-steepening (intermediate beats long).",
        "loses": "Rising-rate regimes (both duration sleeves drag); equity-on rallies (only 40% equity).",
        "source": "Wave 6 candidate, 2026-05-12.",
    },
    "bt_w6_d6_wldu_tyd_ugl": {
        "kind": "Passive 3-asset leveraged blend (no managed futures)",
        "holdings": [
            ("WLDU", "50%", "2× MSCI World — leveraged intl equity"),
            ("TYD",  "30%", "3× 7-10y Treasuries — leveraged intermediate duration"),
            ("UGL",  "20%", "2× gold — inflation/real-rate hedge ⚠ also in deployed AAA Free 2× + NTSD (intermittent)"),
        ],
        "mechanism": "Replaces KMLM with 2× gold. Same 50/30/20 structure as C9 but with real-rate hedge instead of trend-following. Quarterly rebal.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Falling-real-rate regimes (gold rallies — 2020 COVID, 2024-25); intl equity outperformance.",
        "loses": "Rising-real-rate regimes (gold drags); coordinated stock-bond-gold stress (rare but possible).",
        "source": "Wave 6 candidate, 2026-05-12. Eliminates KMLM concentration vs deployed HFEA.",
    },
    "bt_w6_d7_wldu_tyd_gld": {
        "kind": "Passive 3-asset partially-leveraged blend (no managed futures)",
        "holdings": [
            ("WLDU", "50%", "2× MSCI World — leveraged intl equity"),
            ("TYD",  "30%", "3× 7-10y Treasuries — leveraged intermediate duration"),
            ("GLD",  "20%", "1× gold (unleveraged) — inflation hedge"),
        ],
        "mechanism": "Same as D6 but uses unleveraged GLD instead of UGL — tests whether 1× gold can do the diversifier job at the same weight.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Same regimes as D6 (gold rallies). Lower vol drag from 1× gold means smoother ride.",
        "loses": "Same as D6 — slightly lower return contribution from GLD vs UGL in gold rallies.",
        "source": "Wave 6 candidate, 2026-05-12.",
    },
    "bt_w6_d8_wldu_tyd_kmlm_ugl": {
        "kind": "Passive 4-asset leveraged blend (WLDU + duration + MF + gold)",
        "holdings": [
            ("WLDU", "50%", "2× MSCI World — leveraged intl equity"),
            ("TYD",  "25%", "3× 7-10y Treasuries — leveraged intermediate duration"),
            ("KMLM", "15%", "Managed futures ⚠ also in deployed HFEA (30%, fixed)"),
            ("UGL",  "10%", "2× gold ⚠ also in deployed AAA Free 2× + NTSD (intermittent)"),
        ],
        "mechanism": "Four orthogonal diversifiers: intl equity (50), leveraged duration (25), crisis-alpha (15), real-rate hedge (10). KMLM reduced from C9's 20% to 15% to make room for gold. Quarterly rebal.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Best across most regimes — at least one of the four sleeves is rallying. Particularly strong in 2020 (gold + KMLM + WLDU all up) and 2022-24 (KMLM crisis-alpha + gold inflation hedge).",
        "loses": "Coordinated multi-asset stress (rare): would require simultaneous equity selloff + rate spike + commodity collapse + trend reversal. Choppy ranges still erode the leveraged sleeves.",
        "source": "Wave 6 candidate, 2026-05-12. Current shortlist leader — beats C9 on both CAGR and Sharpe.",
    },
    "bt_w6_d9_wldu_tyd_kmlm_gld_slv": {
        "kind": "Passive 5-asset leveraged blend (split precious metals)",
        "holdings": [
            ("WLDU", "50%", "2× MSCI World — leveraged intl equity"),
            ("TYD",  "25%", "3× 7-10y Treasuries — leveraged intermediate duration"),
            ("KMLM", "15%", "Managed futures ⚠ also in deployed HFEA (30%, fixed)"),
            ("GLD",  "5%",  "1× gold — stable inflation hedge"),
            ("SLV",  "5%",  "1× silver — high-vol inflation hedge with industrial-tilt upside"),
        ],
        "mechanism": "Same backbone as D8 but precious-metals sleeve split between gold (stability) and silver (variance). Silver adds upside potential in metals rallies; gold adds drawdown stability.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Strong inflation regimes (silver outperforms gold); industrial-demand rallies.",
        "loses": "Deflationary shocks (silver underperforms gold materially); modest cost of extra rebal complexity vs D8.",
        "source": "Wave 6 candidate, 2026-05-12.",
    },

    # ─── WAVE 7 CANDIDATES (strict ≤2× + zero-deployed-overlap) ───
    "bt_w7_e1_wldu_tlt_gld": {
        "kind": "Passive 3-asset blend (unleveraged duration + 1× gold)",
        "holdings": [
            ("WLDU", "50%", "2× MSCI World — leveraged intl equity (the only leveraged sleeve)"),
            ("TLT",  "30%", "1× 20+yr US Treasury — unleveraged long duration"),
            ("GLD",  "20%", "1× spot gold — inflation/real-rate hedge"),
        ],
        "mechanism": "Static 50/30/20 allocation, quarterly rebalance. Cleanest possible design under strict rules — every ticker is non-deployed and ≤2×.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Mid-cycle bulls with intl equity outperformance + falling rates. Gold rallies during real-rate compression.",
        "loses": "Rising-rate regimes (TLT drags), US-dominant bulls (WLDU underperforms US-only sleeves). Lower notional than W6 winners means less compounding.",
        "source": "Wave 7 candidate, 2026-05-12.",
    },
    "bt_w7_e2_wldu_edv_gld": {
        "kind": "Passive 3-asset blend (zero-coupon duration + 1× gold)",
        "holdings": [
            ("WLDU", "50%", "2× MSCI World — leveraged intl equity"),
            ("EDV",  "30%", "Zero-coupon ~25y Treasury — highest duration of any ≤2× option, no daily-reset decay"),
            ("GLD",  "20%", "1× spot gold"),
        ],
        "mechanism": "Same shape as E1 but EDV replaces TLT — EDV has ~40% more duration than TLT due to longer maturity, so this strategy is more rate-sensitive.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Falling-rate regimes (EDV's convexity outperforms TLT).",
        "loses": "Rate spikes (EDV losses are amplified vs TLT).",
        "source": "Wave 7 candidate, 2026-05-12.",
    },
    "bt_w7_e3_wldu_tlt_dbmf": {
        "kind": "Passive 3-asset blend (duration + alt managed futures)",
        "holdings": [
            ("WLDU", "50%", "2× MSCI World — leveraged intl equity"),
            ("TLT",  "30%", "1× long Treasury — unleveraged duration"),
            ("DBMF", "20%", "iMGP DBi Managed Futures — replicates top-20 CTA hedge funds, distinct ticker from HFEA's KMLM"),
        ],
        "mechanism": "Replaces GLD with DBMF — tests whether managed-futures crisis-alpha beats gold as the third leg.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Trending crises (2008-style) where MF profits; equity selloffs with rate cuts.",
        "loses": "Choppy ranges where MF whipsaws. DBMF only has live data from 2019 — short backtest window.",
        "source": "Wave 7 candidate, 2026-05-12.",
    },
    "bt_w7_e4_wldu_edv_dbmf": {
        "kind": "Passive 3-asset blend (zero-coupon duration + alt MF)",
        "holdings": [
            ("WLDU", "50%", "2× MSCI World — leveraged intl equity"),
            ("EDV",  "30%", "Zero-coupon ~25y Treasury"),
            ("DBMF", "20%", "iMGP DBi Managed Futures"),
        ],
        "mechanism": "EDV + DBMF combo. Tests whether EDV's convexity pairs better with MF than TLT does.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Falling rates + trending crisis combo.",
        "loses": "Stagflation (both EDV and DBMF can struggle simultaneously).",
        "source": "Wave 7 candidate, 2026-05-12.",
    },
    "bt_w7_e5_wldu_tlt_dbmf_gld": {
        "kind": "Passive 4-asset clean diversifier blend",
        "holdings": [
            ("WLDU", "50%", "2× MSCI World — leveraged intl equity"),
            ("TLT",  "25%", "1× long Treasury — duration"),
            ("DBMF", "15%", "iMGP DBi Managed Futures — crisis-alpha"),
            ("GLD",  "10%", "1× spot gold — inflation hedge"),
        ],
        "mechanism": "The W6-D8 pattern (intl-equity + duration + MF + gold) but with compliant ≤2× tickers throughout. Every leg is non-deployed.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Best across regimes — 4-asset diversification means at least one leg usually rallies.",
        "loses": "Coordinated multi-asset stress (very rare). Lower notional than the disqualified D8 means lower CAGR ceiling.",
        "source": "Wave 7 candidate, 2026-05-12. Strict-rules analog of W6's D8 leader.",
    },
    "bt_w7_e6_wldu_edv_dbmf_gld": {
        "kind": "Passive 4-asset blend with EDV duration",
        "holdings": [
            ("WLDU", "50%", "2× MSCI World — leveraged intl equity"),
            ("EDV",  "25%", "Zero-coupon ~25y Treasury"),
            ("DBMF", "15%", "iMGP DBi Managed Futures"),
            ("GLD",  "10%", "1× spot gold"),
        ],
        "mechanism": "Same shape as E5 but EDV replaces TLT — higher rate sensitivity.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Falling rates regime; multi-asset diversification across crisis types.",
        "loses": "Rising rate regimes — EDV drags more than TLT.",
        "source": "Wave 7 candidate, 2026-05-12.",
    },
    "bt_w7_e7_wldu_gde_tlt": {
        "kind": "Capital-efficient stack (gold-via-futures + clean duration)",
        "holdings": [
            ("WLDU", "40%", "2× MSCI World — leveraged intl equity"),
            ("GDE",  "30%", "WisdomTree 90% S&P 500 + 90% gold futures = 180% notional. Brings US equity AND gold via one ticker. ⚠ Internal US-stocks exposure overlaps HFEA's UPRO economically (different ticker but same beta)."),
            ("TLT",  "30%", "1× long Treasury"),
        ],
        "mechanism": "Capital-efficient stacking: GDE provides 27% effective US equity + 27% effective gold notional on $0.30 of capital. Effective notional: 0.40×2 + 0.30×1.8 + 0.30×1 = 1.64.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Gold + US equity tailwinds simultaneously (2024-25 style); efficient compounding vs separate UGL allocation.",
        "loses": "US equity selloff with gold also selling off (e.g., 1980-82 deflation). GDE has SPY beta — adds US correlation to portfolio.",
        "source": "Wave 7 candidate, 2026-05-12.",
    },
    "bt_w7_e8_wldu_rssb_gld": {
        "kind": "Capital-efficient stack (global stocks+bonds + gold)",
        "holdings": [
            ("WLDU", "40%", "2× MSCI World — leveraged intl equity"),
            ("RSSB", "40%", "Return Stacked 100% global stocks + 100% Treasury futures = 200% notional. ⚠ Internal stocks exposure overlaps WLDU economically."),
            ("GLD",  "20%", "1× spot gold"),
        ],
        "mechanism": "RSSB provides built-in stocks+bonds at 200% capital efficiency. Effective notional: 0.40×2 + 0.40×2 + 0.20×1 = 1.80. The internal global-stocks sleeve in RSSB duplicates some of WLDU's equity exposure but adds Treasury duration on top.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Bull markets with falling rates (both legs of RSSB rally).",
        "loses": "2022-style stocks+bonds-both-down (RSSB gets hit on both legs simultaneously).",
        "source": "Wave 7 candidate, 2026-05-12.",
    },
    "bt_w7_e9_wldu_ntsx_gld": {
        "kind": "Capital-efficient stack (US 90/60 + intl + gold)",
        "holdings": [
            ("WLDU", "40%", "2× MSCI World — leveraged intl equity"),
            ("NTSX", "40%", "WisdomTree 90% US stocks + 60% Treasury futures = 150% notional. ⚠ Internal US-stocks exposure overlaps HFEA's UPRO economically."),
            ("GLD",  "20%", "1× spot gold"),
        ],
        "mechanism": "NTSX provides US 90/60 capital-efficient core. Effective notional: 0.40×2 + 0.40×1.5 + 0.20×1 = 1.60.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "US equity rallies with falling rates (NTSX's 90/60 was designed for this).",
        "loses": "Rising rates + flat equity (NTSX drags on both legs). Note: previously discontinued as standalone (11.17% CAGR / 0.57 Sharpe) but used as ingredient here.",
        "source": "Wave 7 candidate, 2026-05-12.",
    },
    "bt_w7_e10_wldu_gde_dbmf": {
        "kind": "Capital-efficient stack (no bonds: stocks+gold via GDE + MF)",
        "holdings": [
            ("WLDU", "40%", "2× MSCI World — leveraged intl equity"),
            ("GDE",  "30%", "WisdomTree 90% S&P 500 + 90% gold futures = 180% notional"),
            ("DBMF", "30%", "iMGP DBi Managed Futures — crisis-alpha (DBMFSIM-extended back to 2000)"),
        ],
        "mechanism": "Removes bonds entirely; bets that MF crisis-alpha is more valuable than duration in the forward regime. Effective notional: 0.40×2 + 0.30×1.8 + 0.30×1 = 1.64.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Trending crises (2008/2022 style) where MF profits while bonds may not.",
        "loses": "Deflationary recessions where bonds rally and MF whipsaws.",
        "source": "Wave 7 candidate, 2026-05-12.",
    },

    # ─── WAVE 8 CANDIDATES (PDF-discovered capital-efficient stacks) ───
    "bt_w8_f1_wldu_ntsi_gld": {
        "kind": "Pure intl-diversification capital-efficient stack",
        "holdings": [
            ("WLDU", "40%", "2× MSCI World — leveraged intl equity"),
            ("NTSI", "40%", "WisdomTree 90% VEA (intl-dev equity) + 60% Treasury futures = 150% notional. Pure intl 90/60 stack — no US equity duplication."),
            ("GLD",  "20%", "1× spot gold — inflation hedge"),
        ],
        "mechanism": "The cleanest expression of intl diversification: WLDU brings 2× MSCI World, NTSI adds intl-developed equity + Treasury duration via capital-efficient stack, GLD adds gold. Effective notional: 0.40×2 + 0.40×1.5 + 0.20×1 = 1.60.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Intl-developed equity bull markets with falling rates (NTSI's design). The intl-tilted version of NTSX-based portfolios.",
        "loses": "US equity leadership with rising rates — both NTSI's equity (intl) and duration (US Treasury) lag.",
        "source": "Wave 8 candidate, 2026-05-12. From r/LETFs comprehensive stacked ETF list.",
    },
    "bt_w8_f2_wldu_gdt_tlt": {
        "kind": "Inflation-defense stack (TIPS+gold via GDT) + long duration",
        "holdings": [
            ("WLDU", "50%", "2× MSCI World — leveraged intl equity"),
            ("GDT",  "30%", "Granite 90% short-term TIPS + 90% gold futures = 180% notional. Inflation-protected gold stack."),
            ("TLT",  "20%", "1× 20+yr Treasury — nominal duration"),
        ],
        "mechanism": "GDT replaces UGL/GLD with a TIPS-stacked gold position. Effective notional: 0.50×2 + 0.30×1.8 + 0.20×1 = 1.74. Addresses both nominal (TLT) and real (GDT) duration risks.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Stagflation regimes (TIPS + gold both rally as real rates fall). 2022-style inflation shocks.",
        "loses": "Deflationary scenarios (TIPS underperform nominals; gold drags).",
        "source": "Wave 8 candidate, 2026-05-12.",
    },
    "bt_w8_f3_wldu_rsit_gld": {
        "kind": "Capital-efficient global stocks + MF stack + gold",
        "holdings": [
            ("WLDU", "40%", "2× MSCI World — leveraged intl equity"),
            ("RSIT", "40%", "Return Stacked 100% VT (global stocks) + 100% managed futures = 200% notional. Bundles equity AND MF in one ticker."),
            ("GLD",  "20%", "1× spot gold"),
        ],
        "mechanism": "RSIT does the work of two sleeves (equity + MF) in one ticker. Effective notional: 0.40×2 + 0.40×2 + 0.20×1 = 1.80. ER on RSIT is 0.97% (high) which is the cost of capital efficiency.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Trending markets (MF profits via RSIT) while equity also rallies. Crisis-alpha embedded.",
        "loses": "Choppy ranges where MF whipsaws; RSIT internal equity correlates with WLDU.",
        "source": "Wave 8 candidate, 2026-05-12.",
    },
    "bt_w8_f4_wldu_goly_tlt": {
        "kind": "Triple-stack diversifier (gold+MF+credit via GOLY) + duration",
        "holdings": [
            ("WLDU", "40%", "2× MSCI World — leveraged intl equity"),
            ("GOLY", "30%", "Quantify 50% gold + 50% MF + 100% corp bonds = 200% notional. Triple-stacked diversifier."),
            ("TLT",  "30%", "1× 20+yr Treasury"),
        ],
        "mechanism": "GOLY packs three diversifiers (gold, MF, credit) into one 200%-notional ticker. Effective total notional: 0.40×2 + 0.30×2 + 0.30×1 = 1.70. Maximum diversifier density.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Multi-regime: gold (inflation), MF (trends), credit (carry), Treasury (deflation) cover different scenarios.",
        "loses": "Coordinated stress (rare). Complexity: GOLY only live since 2025-04, so synth-heavy backtest.",
        "source": "Wave 8 candidate, 2026-05-12.",
    },
    "bt_w8_f5_wldu_ntsi_rsit": {
        "kind": "All-stacks design (every leg is capital-efficient)",
        "holdings": [
            ("WLDU", "40%", "2× MSCI World — leveraged intl equity"),
            ("NTSI", "40%", "Intl 90/60 capital-efficient stack"),
            ("RSIT", "20%", "Global stocks + MF capital-efficient stack"),
        ],
        "mechanism": "No standalone sleeves — every position is a capital-efficient stack. Effective notional: 0.40×2 + 0.40×1.5 + 0.20×2 = 1.80. Tests whether all-stacks beats single-ticker components.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Falling-rate intl bull markets — all three legs have intl/global equity exposure plus duration (NTSI) and MF (RSIT).",
        "loses": "Equity correlation across stacks means coordinated equity selloffs hit all three legs.",
        "source": "Wave 8 candidate, 2026-05-12.",
    },
    "bt_w8_f6_wldu_ntsi_gdt": {
        "kind": "Inflation-aware intl stack",
        "holdings": [
            ("WLDU", "40%", "2× MSCI World — leveraged intl equity"),
            ("NTSI", "30%", "Intl 90/60 — adds intl equity + nominal Treasury duration"),
            ("GDT",  "30%", "TIPS+gold stack — real-rate defense"),
        ],
        "mechanism": "Nominal-vs-real duration barbell: NTSI provides nominal-Treasury duration, GDT provides TIPS (real-rate duration) + gold. Effective notional: 0.40×2 + 0.30×1.5 + 0.30×1.8 = 1.79.",
        "signal": "None — static allocation.",
        "rebal": "Quarterly.",
        "wins": "Either nominal-rate regime works (NTSI for disinflation; GDT for stagflation). Plus intl equity diversification.",
        "loses": "Coordinated rising-real-rates regime — both duration legs hurt. Equity rally without rate help.",
        "source": "Wave 8 candidate, 2026-05-12.",
    },
}


def _deployed_overlap_for_card(holdings: list, is_candidate: bool) -> tuple:
    """For a candidate strategy's holdings, compute which tickers overlap with
    deployed production sleeves. Returns (banner_html, per_ticker_badges).

    per_ticker_badges maps ticker → HTML badge string (or empty if no overlap).
    banner_html is a top-of-card summary for the candidate; empty for deployed
    strategies (no banner needed).
    """
    if not is_candidate:
        return "", {}

    overlaps = []  # list of (ticker, sleeve, alloc_pattern, severity)
    badges = {}
    for tic, _, _ in holdings:
        if tic in DEPLOYED_TICKER_USAGE:
            for sleeve, pattern, severity in DEPLOYED_TICKER_USAGE[tic]:
                overlaps.append((tic, sleeve, pattern, severity))
            # Pick the most severe overlap for the badge color
            severities = [s for _, _, s in DEPLOYED_TICKER_USAGE[tic]]
            top = "fixed" if "fixed" in severities else ("rotation" if "rotation" in severities else "defensive")
            color = {"fixed": "#c62828", "rotation": "#ef6c00", "defensive": "#6a4c93"}[top]
            label = {"fixed": "⚠ DEPLOYED (fixed)", "rotation": "⚠ deployed (rotation)", "defensive": "⚠ deployed (defensive)"}[top]
            badges[tic] = f'<span class="deployed-badge" style="background:{color};color:white;padding:2px 6px;border-radius:3px;font-size:11px;font-weight:600;white-space:nowrap;">{label}</span>'

    if not overlaps:
        banner = (
            '<div class="overlap-banner" style="background:#e8f5e9;border-left:4px solid #2e7d32;padding:10px 14px;margin:10px 0;border-radius:4px;">'
            '<strong style="color:#1b5e20;">✓ Zero production overlap.</strong> '
            'None of this candidate\'s tickers are held by any currently deployed sleeve. '
            'A clean addition that brings genuine diversification.'
            '</div>'
        )
        return banner, badges

    # Build a compact summary table for the banner
    rows = "".join(
        f'<tr><td><strong>{tic}</strong></td><td>{sleeve}</td><td>{pattern}</td></tr>'
        for tic, sleeve, pattern, _ in overlaps
    )
    has_fixed = any(s == "fixed" for _, _, _, s in overlaps)
    banner_color = "#fff3e0" if not has_fixed else "#ffebee"
    banner_border = "#ef6c00" if not has_fixed else "#c62828"
    headline = (
        "⚠ Direct overlap with deployed production." if has_fixed
        else "⚠ Partial overlap with deployed rotation universe."
    )
    decision_prompt = (
        "Stacking these candidates on top of deployed exposure double-counts the leg. Consider whether the marginal diversification benefit justifies the concentration, or pick an alternative without the overlap."
        if has_fixed
        else "These tickers are held intermittently by existing rotation sleeves (top-N selection). Overlap depends on whether momentum picks them at the same time; consider whether the marginal benefit warrants it."
    )
    banner = (
        f'<div class="overlap-banner" style="background:{banner_color};border-left:4px solid {banner_border};padding:10px 14px;margin:10px 0;border-radius:4px;">'
        f'<strong style="color:{banner_border};">{headline}</strong>'
        f'<table style="margin-top:8px;font-size:13px;border-collapse:collapse;">'
        f'<tr style="background:rgba(0,0,0,0.05);"><th style="text-align:left;padding:4px 8px;">Ticker</th><th style="text-align:left;padding:4px 8px;">Deployed in</th><th style="text-align:left;padding:4px 8px;">Allocation pattern</th></tr>'
        f'{rows}</table>'
        f'<p style="margin:8px 0 0 0;font-size:12px;color:#444;">{decision_prompt}</p>'
        f'</div>'
    )
    return banner, badges


def _strategy_card_html(name: str, fn: str, allocation: float, role: str, native_metrics: dict,
                         registry_entry: dict) -> str:
    """Render a detailed explainer card for one strategy."""
    explainer = STRATEGY_EXPLAINERS.get(fn, {})
    if not explainer:
        return f"""<div class="strategy-card"><h4>{name}</h4>
        <p><em>No detailed explainer available. Function: <code>{fn}</code></em></p></div>"""

    # Compute deployed-overlap warnings for candidate strategies
    is_candidate = (role == "candidate")
    overlap_banner, ticker_badges = _deployed_overlap_for_card(
        explainer.get("holdings", []), is_candidate
    )

    # Holdings table (with a fourth column showing production overlap badge)
    holdings_rows = "".join(
        f"<tr><td><strong>{tic}</strong></td><td>{wt}</td><td>{cm}</td><td>{ticker_badges.get(tic, '')}</td></tr>"
        for tic, wt, cm in explainer.get("holdings", [])
    )
    holdings_table = (
        "<table class='holdings-table'><tr><th>Ticker</th><th>Role</th><th>Description</th><th>Production</th></tr>"
        + holdings_rows + "</table>"
    )

    # Metrics
    m = native_metrics or {}
    cagr = m.get("CAGR", 0) * 100
    sharpe = m.get("Sharpe", 0)
    maxdd = m.get("Max DD", 0) * 100
    worst_yr = m.get("Worst Year", 0) * 100

    alloc_str = f"{allocation*100:.2f}%" if allocation else "—"
    return f"""
    <div class="strategy-card">
        <div class="card-header">
            <h4>{name} <span class="strategy-kind">— {explainer.get('kind', '')}</span></h4>
            <div class="card-kpi-row">
                <span class="kpi-chip"><strong>Allocation</strong> {alloc_str}</span>
                <span class="kpi-chip"><strong>CAGR</strong> {cagr:.2f}%</span>
                <span class="kpi-chip"><strong>Sharpe</strong> {sharpe:.2f}</span>
                <span class="kpi-chip"><strong>MaxDD</strong> {maxdd:.1f}%</span>
                <span class="kpi-chip"><strong>Worst Yr</strong> {worst_yr:.1f}%</span>
            </div>
        </div>
        <p class="card-mechanism"><strong>What it does:</strong> {explainer.get('mechanism', '')}</p>
        {overlap_banner}
        <p><strong>Holdings:</strong></p>
        {holdings_table}
        <div class="card-detail-grid">
            <div><strong>Signal logic</strong><br><span class="muted">{explainer.get('signal', '—')}</span></div>
            <div><strong>Rebalance</strong><br><span class="muted">{explainer.get('rebal', '—')}</span></div>
        </div>
        <div class="card-wins-loses">
            <div class="card-wins"><strong>✓ When it wins:</strong><br>{explainer.get('wins', '—')}</div>
            <div class="card-loses"><strong>✗ When it loses:</strong><br>{explainer.get('loses', '—')}</div>
        </div>
        <p class="card-source"><strong>Source:</strong> {explainer.get('source', '—')}</p>
    </div>
    """


def _ticker_glossary_html() -> str:
    """Render the ticker glossary as a categorized table."""
    sections = [
        ("Equity ETFs (1× — passive)", ["SPY", "QQQ", "EFA", "EEM", "IWM", "URTH", "VT"]),
        ("Leveraged Equity ETFs", ["UPRO", "SPXL", "TQQQ", "SSO", "SPUU", "QLD", "EFO", "EET", "SAA", "TNA", "EDC"]),
        ("Bonds (1× passive + cash)", ["TLT", "IEF", "BND", "AGG", "SHV", "SHY", "USFR", "SGOV", "BIL"]),
        ("Leveraged Bonds", ["TMF", "UBT", "UST", "TYD", "EDV"]),
        ("Real Assets", ["GLD", "UGL", "SLV", "DBC"]),
        ("Managed Futures", ["KMLM", "DBMF"]),
        ("Capital-Efficient Stacks (Return Stacked / WisdomTree)", ["NTSD", "NTSX", "NTSI", "GDE", "GDT", "RSSB", "RSIT", "GOLY", "WTIP"]),
    ]
    out = []
    for section_name, tickers in sections:
        out.append(f"<h4>{section_name}</h4>")
        rows = []
        for t in tickers:
            if t not in TICKER_GLOSSARY:
                continue
            short, long = TICKER_GLOSSARY[t]
            rows.append(f"<tr><td><strong>{t}</strong></td><td>{short}</td><td>{long}</td></tr>")
        out.append("<table class='glossary-table'><tr><th>Ticker</th><th>Full Name</th><th>Description</th></tr>"
                   + "".join(rows) + "</table>")
    return "".join(out)


def _mc_comparison_html(mc: pd.DataFrame, deterministic: dict, names: list, benchmarks: list) -> str:
    """Combined MC comparison table with visual range bars.

    Replaces the 4 separate distribution tables with one master table showing
    each strategy's CAGR/Sharpe/MaxDD distributions side-by-side plus
    beat-benchmark probabilities. Visual range bars show p5-p95 spread.
    """
    if mc is None or len(mc) == 0:
        return "<p><em>Monte Carlo skipped or failed.</em></p>"

    primary = benchmarks[0] if benchmarks else None
    own = mc[mc["benchmark"] == primary] if primary else mc

    # Per-strategy aggregated stats
    rows = []
    for s in names:
        sub = own[own["strategy"] == s]
        if len(sub) == 0:
            continue
        det = deterministic.get(s, {}) or {}
        # Beat-benchmark probabilities (across both benchmarks). Skip self-pairs
        # so SPY-as-strategy doesn't show 0% beat-SPY (trivially same path).
        beats = {}
        for b in benchmarks:
            if s == b:
                continue
            sb = mc[(mc["strategy"] == s) & (mc["benchmark"] == b)]
            if len(sb) == 0:
                continue
            beats[f"{b}_cagr"]   = (sb["cagr"]   > sb["bench_cagr"]).mean()
            beats[f"{b}_sharpe"] = (sb["sharpe"] > sb["bench_sharpe"]).mean()
            beats[f"{b}_maxdd"]  = (sb["max_dd"] > sb["bench_dd"]).mean()
        rows.append({
            "strategy": s,
            "cagr_det":    det.get("CAGR"),
            "cagr_p5":     float(sub["cagr"].quantile(0.05)),
            "cagr_p50":    float(sub["cagr"].quantile(0.50)),
            "cagr_p95":    float(sub["cagr"].quantile(0.95)),
            "sharpe_det":  det.get("Sharpe"),
            "sharpe_p5":   float(sub["sharpe"].quantile(0.05)),
            "sharpe_p50":  float(sub["sharpe"].quantile(0.50)),
            "sharpe_p95":  float(sub["sharpe"].quantile(0.95)),
            "maxdd_det":   det.get("Max DD"),
            "maxdd_p5":    float(sub["max_dd"].quantile(0.05)),
            "maxdd_p50":   float(sub["max_dd"].quantile(0.50)),
            "maxdd_p95":   float(sub["max_dd"].quantile(0.95)),
            "beats": beats,
        })

    # Sort by deterministic Sharpe descending
    rows.sort(key=lambda r: -(r["sharpe_det"] or -99))

    # Compute global ranges for bar normalization
    all_cagr = [v for r in rows for v in (r["cagr_p5"], r["cagr_p95"]) if v is not None]
    all_sharpe = [v for r in rows for v in (r["sharpe_p5"], r["sharpe_p95"]) if v is not None]
    all_maxdd = [v for r in rows for v in (r["maxdd_p5"], r["maxdd_p95"]) if v is not None]
    cagr_min, cagr_max = (min(all_cagr), max(all_cagr)) if all_cagr else (0, 0.3)
    sharpe_min, sharpe_max = (min(all_sharpe), max(all_sharpe)) if all_sharpe else (-0.5, 1.5)
    maxdd_min, maxdd_max = (min(all_maxdd), max(all_maxdd)) if all_maxdd else (-1.0, 0)

    def range_bar(p5, p50, p95, det, vmin, vmax, fmt, color="#3a7"):
        """Render a CSS range bar: p5-p95 span with a marker for p50 and a star for deterministic value."""
        if any(v is None for v in (p5, p95)):
            return "—"
        span = max(vmax - vmin, 1e-9)
        left_pct = max(0, (p5 - vmin) / span * 100)
        width_pct = max(0.5, (p95 - p5) / span * 100)
        p50_pct = max(0, (p50 - vmin) / span * 100) if p50 is not None else None
        det_pct = max(0, (det - vmin) / span * 100) if det is not None else None
        det_label = fmt(det) if det is not None else ""
        bar = f"""<div class="rangebar">
            <div class="rangebar-track"></div>
            <div class="rangebar-fill" style="left:{left_pct:.1f}%; width:{width_pct:.1f}%; background:{color};"></div>"""
        if p50_pct is not None:
            bar += f'<div class="rangebar-p50" style="left:{p50_pct:.1f}%;"></div>'
        if det_pct is not None:
            bar += f'<div class="rangebar-det" style="left:{det_pct:.1f}%;" title="Deterministic: {det_label}"></div>'
        bar += "</div>"
        return f"""<div class="rangebar-row">
            <span class="rangebar-value">{det_label}</span>
            {bar}
            <span class="rangebar-range">[{fmt(p5)} – {fmt(p95)}]</span>
        </div>"""

    def beat_cell(prob):
        if prob is None:
            return "—"
        pct = prob * 100
        color = "#0a0" if prob >= 0.7 else "#7a3" if prob >= 0.5 else "#c80" if prob >= 0.3 else "#c00"
        return f"<span style='color:{color};font-weight:600'>{pct:.0f}%</span>"

    # Build table
    out = ["""<style>
    .mc-comparison-table { font-size:0.88em; }
    .rangebar-row { display:flex; align-items:center; gap:8px; min-width:280px; }
    .rangebar-value { font-weight:600; min-width:55px; text-align:right; font-variant-numeric:tabular-nums; }
    .rangebar-range { color:#888; font-size:0.85em; min-width:95px; font-variant-numeric:tabular-nums; }
    .rangebar { position:relative; flex:1; height:14px; min-width:120px; }
    .rangebar-track { position:absolute; left:0; top:6px; width:100%; height:2px; background:#e0e0e0; }
    .rangebar-fill { position:absolute; top:4px; height:6px; border-radius:3px; opacity:0.7; }
    .rangebar-p50 { position:absolute; top:1px; width:2px; height:12px; background:#222; }
    .rangebar-det { position:absolute; top:-2px; width:8px; height:18px; background:#000; clip-path:polygon(50% 0%, 100% 100%, 0% 100%); }
    </style>"""]

    out.append("<table class='mc-comparison-table'>")
    out.append("""<tr>
        <th rowspan="2" style="vertical-align:middle">Strategy</th>
        <th colspan="3" style="text-align:center">CAGR (p5–p95 across 2000 sims)</th>
        <th colspan="3" style="text-align:center">Sharpe</th>
        <th colspan="3" style="text-align:center">Max Drawdown</th>
    </tr>""")
    out.append(f"""<tr>
        <th>{primary or 'Det'}</th><th></th><th>p5 - p95</th>
        <th>{primary or 'Det'}</th><th></th><th>p5 - p95</th>
        <th>{primary or 'Det'}</th><th></th><th>p5 - p95</th>
    </tr>""")

    for r in rows:
        c_bar = range_bar(r["cagr_p5"], r["cagr_p50"], r["cagr_p95"], r["cagr_det"],
                          cagr_min, cagr_max, lambda v: f"{v*100:.2f}%", color="#3a7")
        s_bar = range_bar(r["sharpe_p5"], r["sharpe_p50"], r["sharpe_p95"], r["sharpe_det"],
                          sharpe_min, sharpe_max, lambda v: f"{v:.2f}", color="#37a")
        d_bar = range_bar(r["maxdd_p5"], r["maxdd_p50"], r["maxdd_p95"], r["maxdd_det"],
                          maxdd_min, maxdd_max, lambda v: f"{v*100:.1f}%", color="#c63")
        out.append(f"<tr><td><strong>{r['strategy']}</strong></td>"
                   f"<td colspan='3'>{c_bar}</td>"
                   f"<td colspan='3'>{s_bar}</td>"
                   f"<td colspan='3'>{d_bar}</td></tr>")
    out.append("</table>")

    # Legend
    out.append("""<p style="color:#666;font-size:0.88em;margin-top:8px">
        <strong>Reading the bars:</strong> colored band = 5th to 95th percentile across 2000 simulated bootstrap paths.
        <span style="display:inline-block;width:8px;height:14px;background:#000;clip-path:polygon(50% 0%, 100% 100%, 0% 100%);margin:0 4px;vertical-align:middle"></span>
        = deterministic backtest value.
        <span style="display:inline-block;width:2px;height:12px;background:#222;margin:0 4px;vertical-align:middle"></span>
        = median (p50). Strategies are sorted by deterministic Sharpe.
    </p>""")

    # Beat-benchmark probabilities (compact)
    out.append("<h4 style='margin-top:24px'>Beat-benchmark probability (matched bootstrap paths)</h4>")
    out.append("<table class='mc-comparison-table'>")
    bench_headers = "".join(f"<th colspan='3' style='text-align:center'>vs {b}</th>" for b in benchmarks)
    sub_headers = "".join("<th>CAGR</th><th>Sharpe</th><th>MaxDD</th>" for _ in benchmarks)
    out.append(f"<tr><th rowspan='2' style='vertical-align:middle'>Strategy</th>{bench_headers}</tr>")
    out.append(f"<tr>{sub_headers}</tr>")
    for r in rows:
        beats = r["beats"]
        cells = []
        for b in benchmarks:
            cells.append(f"<td>{beat_cell(beats.get(f'{b}_cagr'))}</td>")
            cells.append(f"<td>{beat_cell(beats.get(f'{b}_sharpe'))}</td>")
            cells.append(f"<td>{beat_cell(beats.get(f'{b}_maxdd'))}</td>")
        out.append(f"<tr><td><strong>{r['strategy']}</strong></td>" + "".join(cells) + "</tr>")
    out.append("</table>")
    out.append("""<p style="color:#666;font-size:0.88em">
        Colors: <span style="color:#0a0">≥70%</span> /
        <span style="color:#7a3">50–70%</span> /
        <span style="color:#c80">30–50%</span> /
        <span style="color:#c00">&lt;30%</span>.
        Each cell shows the % of bootstrap paths on which the strategy beat the benchmark on that metric.
    </p>""")

    return "\n".join(out)


def _metrics_table_html(names: list, results: dict, metrics: dict,
                        with_alloc: dict = None, with_proposed: dict = None,
                        with_reason: dict = None, with_quality: dict = None,
                        with_native: dict = None,
                        with_after_tax: dict = None) -> str:
    """Render a generic strategy-metrics HTML table.

    with_quality:    name → quality tier letter (A/B/C/D)
    with_native:     name → 'earliest' date for native-window metrics
    with_after_tax:  name → after-tax metrics dict (After-Tax CAGR, Tax Drag (pp))
    """
    rows = []
    headers = ["Strategy"]
    if with_alloc is not None:
        headers.append("Alloc")
    if with_proposed is not None:
        headers.append("Proposed Alloc")
    if with_quality is not None:
        headers.append("Data Q")
    headers += ["Coverage", "Years"]
    if with_native is not None:
        headers += ["Native CAGR", "Native Sharpe", "Native MaxDD"]
    headers += ["Full CAGR", "Full Sharpe", "Full MaxDD", "Worst Yr", "Calmar"]
    if with_after_tax is not None:
        headers += ["After-Tax CAGR", "Tax Drag"]
    if with_reason is not None:
        headers.append("Reason demoted")
    for n in names:
        if n not in results:
            continue
        m = metrics.get(n, {})
        cells = [n]
        if with_alloc is not None:
            cells.append(f"{with_alloc.get(n, 0)*100:.0f}%" if with_alloc.get(n) else "—")
        if with_proposed is not None:
            cells.append(f"{with_proposed.get(n, 0)*100:.0f}%" if with_proposed.get(n) else "—")
        if with_quality is not None:
            cells.append(_quality_badge(with_quality.get(n, "?")))
        cells.append(_coverage_window(results[n]))
        cells.append(f"{_years(results[n]):.1f}")
        if with_native is not None:
            earliest = with_native.get(n)
            nm = _native_window_metrics(results[n], earliest) if earliest else {}
            cells.append(f"{nm.get('CAGR', 0)*100:.2f}%" if nm else "—")
            cells.append(f"{nm.get('Sharpe', 0):.2f}" if nm else "—")
            cells.append(f"{nm.get('Max DD', 0)*100:.2f}%" if nm else "—")
        cells.append(f"{m.get('CAGR', 0)*100:.2f}%" if m else "—")
        cells.append(f"{m.get('Sharpe', 0):.2f}" if m else "—")
        cells.append(f"{m.get('Max DD', 0)*100:.2f}%" if m else "—")
        cells.append(f"{m.get('Worst Year', 0)*100:.2f}%" if m else "—")
        cells.append(f"{m.get('Calmar', 0):.2f}" if m else "—")
        if with_after_tax is not None:
            at = with_after_tax.get(n, {})
            if at:
                drag = at.get("Tax Drag (pp)", 0)
                drag_color = "#0a0" if drag < 1.0 else ("#c80" if drag < 2.0 else "#c00")
                cells.append(f"{at.get('After-Tax CAGR', 0)*100:.2f}%")
                cells.append(f"<span style='color:{drag_color}'>{drag:+.2f}pp</span>")
            else:
                cells.append("—")
                cells.append("—")
        if with_reason is not None:
            cells.append(with_reason.get(n, ""))
        rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    head = "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"
    return f"<table>{head}{''.join(rows)}</table>"


def _tax_section_html(deployed_names: list, deployed_alloc: dict,
                       after_tax_metrics: dict, tax_logs: dict,
                       tax_assumptions: dict, metrics: dict,
                       aggregate_metrics: dict) -> str:
    """Render Section 11 — German Tax Drag (Berlin, Alpaca)."""
    if not after_tax_metrics:
        return ""
    try:
        import importlib
        tax_overlay = importlib.import_module("tax_overlay")
        eff_rate_pct = tax_overlay.EFFECTIVE_TAX_RATE * 100
    except Exception:
        eff_rate_pct = 26.375

    out = []
    out.append('<h2>11. German Tax Drag <span style="font-size:0.6em;color:#666;">'
               '(Berlin · Alpaca US broker · Abgeltungsteuer + Soli · non-church-member)</span></h2>')
    out.append(
        '<div class="warn-box">'
        f'<strong>Method:</strong> per-rebalance realized-gain accounting (average-cost basis per asset). '
        f'Each strategy emits a target-weights timeline; every change triggers a buy/sell. '
        f'<strong>Teilfreistellung</strong> applied per <code>tax/config.py SYMBOL_TFS_RATE</code> '
        f'(30% equity ETFs, 15% mixed, 0% bonds & commodities). '
        f'<strong>Vorabpauschale</strong> added on Dec 31 (NAV<sub>start</sub> × Basiszins × 0.7 × (1−TFS), capped at year gain). '
        f'<strong>Verlustvortrag</strong> carryforward absorbs future gains. '
        f'<strong>Effective rate</strong>: 25% Abgeltungsteuer × (1 + 5.5% Soli) = <strong>{eff_rate_pct:.3f}%</strong>. '
        f'Sparerpauschbetrag set to €0 (allowance assumed consumed at another broker). '
        f'Kirchensteuer set to 0% (non-church-member). '
        f'US dividend withholding (15% W-8BEN, creditable) not modelled — assumed to net out against German tax.'
        '</div>'
    )

    # Per-strategy assumptions + after-tax table
    out.append("<h3>11.1. Per-strategy assumptions &amp; after-tax performance</h3>")
    out.append('<table><tr>'
               '<th>Strategy</th><th>Alloc</th><th>Tickers</th>'
               '<th>Weighted TFS</th><th>Turnover/yr</th>'
               '<th>Gross CAGR</th><th>After-Tax CAGR</th>'
               '<th>Tax Drag</th><th>Tax-Cost Ratio</th></tr>')
    for n in deployed_names:
        if n not in after_tax_metrics:
            continue
        at = after_tax_metrics.get(n, {})
        m = metrics.get(n, {})
        assump = tax_assumptions.get(n, {})
        tickers = ", ".join(t for t in assump.get("tickers", []) if t not in ("SHV", "BIL", "SGOV", "USFR"))
        if not tickers:
            tickers = ", ".join(assump.get("tickers", []))
        wt_tfs = assump.get("weighted_tfs", 0) * 100
        turn = assump.get("annual_turnover", 0) * 100
        gross = m.get("CAGR", 0) * 100
        aft = at.get("After-Tax CAGR", 0) * 100
        drag = at.get("Tax Drag (pp)", 0)
        tcr = at.get("Tax-Cost Ratio", 0) * 100 if at.get("Tax-Cost Ratio") is not None else 0
        drag_color = "#0a0" if drag < 1.0 else ("#c80" if drag < 2.0 else "#c00")
        out.append(
            f"<tr><td><strong>{n}</strong></td>"
            f"<td>{deployed_alloc.get(n, 0)*100:.0f}%</td>"
            f"<td style='font-size:0.85em;color:#555'>{tickers}</td>"
            f"<td>{wt_tfs:.0f}%</td>"
            f"<td>{turn:.0f}%</td>"
            f"<td>{gross:.2f}%</td>"
            f"<td>{aft:.2f}%</td>"
            f"<td style='color:{drag_color}'>{drag:+.2f}pp</td>"
            f"<td>{tcr:.1f}%</td></tr>"
        )
    # Aggregate row
    agg_at = after_tax_metrics.get("AGGREGATE (deployed)", {})
    if agg_at and aggregate_metrics:
        gross = aggregate_metrics.get("CAGR", 0) * 100
        aft = agg_at.get("After-Tax CAGR", 0) * 100
        drag = agg_at.get("Tax Drag (pp)", 0)
        tcr = agg_at.get("Tax-Cost Ratio", 0) * 100 if agg_at.get("Tax-Cost Ratio") is not None else 0
        drag_color = "#0a0" if drag < 1.0 else ("#c80" if drag < 2.0 else "#c00")
        out.append(
            f"<tr style='font-weight:bold;background:#f5f5f5'><td>AGGREGATE (deployed)</td>"
            f"<td>100%</td><td style='color:#888'>—</td><td>—</td><td>—</td>"
            f"<td>{gross:.2f}%</td><td>{aft:.2f}%</td>"
            f"<td style='color:{drag_color}'>{drag:+.2f}pp</td>"
            f"<td>{tcr:.1f}%</td></tr>"
        )
    out.append("</table>")

    # Per-year aggregate tax log (last 10 years)
    agg_log = tax_logs.get("AGGREGATE (deployed)")
    # Aggregate log isn't computed (only per-strategy), but we can sum across strategies for each year:
    if tax_logs:
        years = set()
        for log in tax_logs.values():
            if log is not None and not log.empty:
                years.update(log.index.tolist())
        years = sorted(years)[-10:] if years else []
        if years:
            out.append("<h3>11.2. Aggregate tax events — last 10 years</h3>")
            out.append("<p style='color:#555;font-size:0.92em'>Sum of per-strategy realized gains, "
                       "Vorabpauschale, and tax paid. Drag (pp) is computed at unit NAV per strategy "
                       "and shown as the weighted sum across the deployed sleeves.</p>")
            out.append("<table><tr><th>Year</th><th>Realized gains (TFS-adj.)</th>"
                       "<th>Vorabpauschale</th><th>Tax paid</th><th>Carryforward used</th></tr>")
            for y in years:
                rg = sum(float(log.loc[y, "realized_gains_tfs"]) * deployed_alloc.get(n, 0)
                         for n, log in tax_logs.items() if log is not None and y in log.index)
                vp = sum(float(log.loc[y, "vorabpauschale"]) * deployed_alloc.get(n, 0)
                         for n, log in tax_logs.items() if log is not None and y in log.index)
                tx = sum(float(log.loc[y, "tax_paid"]) * deployed_alloc.get(n, 0)
                         for n, log in tax_logs.items() if log is not None and y in log.index)
                cf = sum(float(log.loc[y, "carryforward_used"]) * deployed_alloc.get(n, 0)
                         for n, log in tax_logs.items() if log is not None and y in log.index)
                out.append(
                    f"<tr><td>{y}</td>"
                    f"<td>{rg*100:.2f}%</td>"
                    f"<td>{vp*100:.2f}%</td>"
                    f"<td><strong>{tx*100:.2f}%</strong></td>"
                    f"<td>{cf*100:.2f}%</td></tr>"
                )
            out.append("</table>")
            out.append("<p style='color:#888;font-size:0.85em'>Values are fractions of unit NAV, "
                       "weighted by each sleeve's production allocation.</p>")

    # Bar chart reference
    out.append("<h3>11.3. Gross vs After-Tax CAGR — visual comparison</h3>")
    out.append("<img src='tax_drag_comparison.png' style='max-width:900px'>")
    return "\n".join(out)


def _retirement_section_html(retirement: dict) -> str:
    """
    Render the Retirement Projection section (1b). Inserted between the
    Executive Summary KPI row and Section 2. Includes:
      • headline paragraph
      • deterministic + MC summary numbers
      • sensitivity grid (monthly × target → years and age)
      • probability-by-age grid
      • embedded retirement_projection.png chart
    """
    if not retirement:
        return ""

    starting = retirement["starting"]
    monthly = retirement["monthly"]
    target = retirement["target"]
    inflation = retirement["inflation"]
    swr = retirement["swr"]
    after_tax_cagr = retirement["after_tax_cagr"]
    real_cagr = retirement["real_cagr"]
    age_today = retirement["age_today"]
    det = retirement["deterministic"]
    sens = retirement["sensitivity"]
    mc_ret = retirement["mc"]
    prob_grid = retirement.get("prob_grid")

    yrs_det = det["years_to_target"]
    det_str = (f"{yrs_det:.1f} years → age {age_today + yrs_det:.1f}"
               if np.isfinite(yrs_det) else "not reached in 50 years")

    # MC summary
    mc_basis = retirement.get("mc_basis", "after-tax")
    mc_html = "<p><em>Monte Carlo simulation skipped or unavailable.</em></p>"
    if mc_ret is not None:
        yrs = mc_ret["years_to_target"]
        finite = yrs[np.isfinite(yrs)]
        pct_never = (1 - len(finite) / len(yrs)) * 100
        if len(finite) >= 10:
            p5_y, p50_y, p95_y = np.percentile(finite, [5, 50, 95])
            mc_html = (
                "<p><strong>Monte Carlo bootstrap</strong> "
                f"({mc_ret['n_sims']:,} simulations of {mc_ret['max_years']}-year wealth paths, "
                f"stationary block bootstrap on the deployed-aggregate <strong>{mc_basis}</strong> daily returns, "
                "inflation-deflated to real dollars, $X/mo contributions injected every 21 trading days):</p>"
                "<p style='color:#555;font-size:0.92em'><em>Reading the table: "
                "<strong>shorter years = luckier</strong>. p5 means \"5% of sims hit the target "
                "this fast or faster\" (best case). p95 means \"only 5% of sims took this long or "
                "longer\" (worst case). Median p50 = half of sims reach the target by this age.</em></p>"
                "<table style='max-width:700px'>"
                "<tr><th>Percentile</th><th>Years to target</th><th>Age at retirement</th></tr>"
                f"<tr><td>Optimistic case (p5 — lucky 5%)</td>"
                f"<td>{p5_y:.1f}</td><td>{age_today+p5_y:.1f}</td></tr>"
                f"<tr><td><strong>Median (p50)</strong></td>"
                f"<td><strong>{p50_y:.1f}</strong></td>"
                f"<td><strong>{age_today+p50_y:.1f}</strong></td></tr>"
                f"<tr><td>Pessimistic case (p95 — unlucky 5%)</td>"
                f"<td>{p95_y:.1f}</td><td>{age_today+p95_y:.1f}</td></tr>"
                f"<tr><td>Never reached</td><td colspan='2'>{pct_never:.1f}% of sims</td></tr>"
                "</table>"
            )

    # Callout: why deterministic and MC median don't match.
    # Both are valid — they answer slightly different questions.
    callout_html = ""
    if mc_ret is not None and np.isfinite(yrs_det):
        finite_yrs = mc_ret["years_to_target"][np.isfinite(mc_ret["years_to_target"])]
        if len(finite_yrs) >= 10:
            p50_y = float(np.median(finite_yrs))
            p95_y = float(np.percentile(finite_yrs, 95))
            callout_html = f"""
<div style="background:#fff8e1;border-left:4px solid #f80;padding:14px 20px;margin:18px 0;border-radius:4px;">
<h4 style="margin:0 0 8px 0;color:#222;">Why do deterministic and MC median disagree?</h4>
<p style="margin:6px 0;line-height:1.55;">
<strong>Deterministic ({yrs_det:.1f}y → age {age_today+yrs_det:.1f})</strong> assumes every year
delivers the exact same {real_cagr*100:.2f}% real return — a perfectly smooth path.
<strong>MC median ({p50_y:.1f}y → age {age_today+p50_y:.1f})</strong> simulates 5,000 noisy market
paths and asks <em>"by what age has half of futures hit ${target/1e6:.2f}M?"</em>
</p>
<p style="margin:6px 0;line-height:1.55;">
The distribution of years-to-target is <strong>right-skewed</strong>: lucky paths reach the target
fast and cluster on the left, unlucky paths have a long tail to the right. This is a textbook
first-passage-time effect for noisy positive-drift processes — the median is always
<em>shorter</em> than the smooth-return projection. <em>Both numbers are correct;</em> they just
answer different questions.
</p>
<p style="margin:6px 0;line-height:1.55;">
<strong>How to read this for planning:</strong>
</p>
<ul style="margin:4px 0 6px 24px;line-height:1.55;">
<li><strong>Anchor your goal on age {age_today+yrs_det:.1f}</strong> (deterministic) — the "if I get the average return every year" answer. Conservative.</li>
<li><strong>MC median age {age_today+p50_y:.1f}</strong> is the actual 50/50 statistical odds. Useful but easy to over-trust because it implicitly downweights the asymmetric pain of unlucky paths.</li>
<li><strong>Plan around age {age_today+p95_y:.1f}</strong> (MC p95, pessimistic) — the realistic worst-case floor for stress-testing.</li>
</ul>
</div>
"""

    # Sensitivity table — monthly × target
    sens_html = "<h3>Sensitivity — monthly contribution × target</h3>"
    sens_html += "<p style='color:#555;font-size:0.92em'>Deterministic projection at the median real CAGR. "
    sens_html += f"Each cell shows years to target (and age at that point — you are {age_today:.1f} today).</p>"
    sens_html += "<table style='max-width:1100px'>"
    sens_html += "<tr><th>Monthly contribution</th>"
    for t in sens.columns:
        sens_html += f"<th>${t/1e6:.2f}M (real)</th>"
    sens_html += "</tr>"
    for m in sens.index:
        sens_html += f"<tr><td><strong>${int(m):,}/mo</strong></td>"
        for t in sens.columns:
            y = sens.loc[m, t]
            if np.isfinite(y):
                age = age_today + y
                # Color highlight: green if reachable by 50, amber by 60, red beyond
                if age <= 50:
                    color = "#0a0"
                elif age <= 60:
                    color = "#c80"
                else:
                    color = "#c00"
                cell = f"<span style='color:{color}'>{y:.1f}y (age {age:.1f})</span>"
            else:
                cell = "<span style='color:#888'>never</span>"
            sens_html += f"<td>{cell}</td>"
        sens_html += "</tr>"
    sens_html += "</table>"

    # Probability-by-age grid
    prob_html = ""
    if prob_grid is not None and not prob_grid.empty:
        prob_html = "<h3>Probability of reaching target by age</h3>"
        prob_html += ("<p style='color:#555;font-size:0.92em'>From the Monte Carlo bootstrap. "
                      "Uses running-max wealth (so 'reached by age X' counts sims that touched the "
                      "target at any point through that age — closer to how you'd actually behave).</p>")
        prob_html += "<table style='max-width:900px'>"
        prob_html += "<tr><th>Age</th>"
        for t in prob_grid.columns:
            prob_html += f"<th>P(≥ ${t/1e6:.2f}M real)</th>"
        prob_html += "</tr>"
        for age in prob_grid.index:
            prob_html += f"<tr><td><strong>{age}</strong></td>"
            for t in prob_grid.columns:
                p = float(prob_grid.loc[age, t])
                if p >= 0.75:
                    color = "#0a0"
                elif p >= 0.50:
                    color = "#7a3"
                elif p >= 0.25:
                    color = "#c80"
                else:
                    color = "#c00"
                prob_html += f"<td><span style='color:{color};font-weight:600'>{p*100:.1f}%</span></td>"
            prob_html += "</tr>"
        prob_html += "</table>"

    chart_html = ""
    if os.path.exists("retirement_projection.png"):
        chart_html = "<h3>Projected wealth (real dollars)</h3><img src='retirement_projection.png'>"

    return f"""
<h2>1b. Retirement Projection
  <span style="font-size:0.6em;color:#666;">
  (forward-projects the deployed aggregate at after-tax real CAGR, in today's dollars)
  </span>
</h2>
<div class="kpi-row">
    <div class="kpi"><div class="label">Live equity (Alpaca)</div>
        <div class="value">${starting:,.0f}</div></div>
    <div class="kpi"><div class="label">Current age</div>
        <div class="value">{age_today:.1f}</div></div>
    <div class="kpi"><div class="label">Monthly contribution (real)</div>
        <div class="value">${monthly:,.0f}</div></div>
    <div class="kpi"><div class="label">Target (real)</div>
        <div class="value">${target/1e6:.2f}M</div></div>
    <div class="kpi positive"><div class="label">SWR income at target</div>
        <div class="value">${target*swr:,.0f}/yr</div></div>
</div>

<p>Math runs in <strong>real dollars</strong> (today's purchasing power). The deployed aggregate
has an after-tax CAGR of <strong>{after_tax_cagr*100:.2f}%/yr</strong> nominal; deducting
{inflation*100:.1f}% inflation leaves a <strong>real CAGR of {real_cagr*100:.2f}%/yr</strong>.
Contributions are treated as ${monthly:,.0f}/mo in today's dollars (i.e. you raise them with inflation each year — the realistic case).</p>

<p><strong>Deterministic projection:</strong> {det_str}. At a {swr*100:.1f}% safe withdrawal rate,
the ${target/1e6:.2f}M target yields <strong>${target*swr:,.0f}/yr</strong> of income in today's dollars.</p>

{mc_html}

{callout_html}

{sens_html}

{prob_html}

{chart_html}
"""


def _stress_table_html(targets: list, results: dict, windows: list) -> str:
    """Compute total return per (strategy, window) and emit an HTML table."""
    head = "<tr><th>Period</th>" + "".join(f"<th>{t}</th>" for t in targets) + "</tr>"
    rows = []
    for label, start, end, desc in windows:
        cells = [f"<td><strong>{label}</strong><br><span style='color:#888;font-size:0.85em'>{start} → {end}</span><br><span style='color:#666;font-size:0.85em'>{desc}</span></td>"]
        for t in targets:
            s = results.get(t)
            if s is None:
                cells.append("<td>—</td>")
                continue
            sub = s.loc[start:end].dropna()
            if len(sub) < 5:
                cells.append("<td>—</td>")
                continue
            tot = float((1 + sub).prod() - 1)
            color = "#0a0" if tot > 0 else "#c00"
            cells.append(f"<td style='color:{color}'>{tot*100:+.2f}%</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table>{head}{''.join(rows)}</table>"


def _mc_summary_html(mc: pd.DataFrame, deterministic: dict,
                     names: list, benchmarks: list) -> str:
    if mc is None or len(mc) == 0:
        return "<p><em>Monte Carlo skipped or failed.</em></p>"
    out = []
    primary = benchmarks[0] if benchmarks else None
    own = mc[mc["benchmark"] == primary] if primary else mc
    out.append("<h3>CAGR / Sharpe / MaxDD distributions (5th/50th/95th percentile)</h3>")
    out.append("<table><tr><th>Strategy</th><th>Metric</th><th>Deterministic</th><th>p5</th><th>p50</th><th>p95</th></tr>")
    for s in names:
        sub = own[own["strategy"] == s]
        if len(sub) == 0:
            continue
        for label, col, fmt, in_pct in [
            ("CAGR",    "cagr",   "{:+.2f}%", True),
            ("Sharpe",  "sharpe", "{:.2f}",   False),
            ("Max DD",  "max_dd", "{:+.2f}%", True),
        ]:
            det = deterministic.get(s, {}).get({"cagr": "CAGR", "sharpe": "Sharpe", "max_dd": "Max DD"}[col])
            p5, p50, p95 = sub[col].quantile(0.05), sub[col].quantile(0.50), sub[col].quantile(0.95)
            scale = 100 if in_pct else 1
            det_s = fmt.format(det * scale) if det is not None else "—"
            out.append(f"<tr><td>{s}</td><td>{label}</td><td>{det_s}</td>"
                       f"<td>{fmt.format(p5*scale)}</td>"
                       f"<td>{fmt.format(p50*scale)}</td>"
                       f"<td>{fmt.format(p95*scale)}</td></tr>")
    out.append("</table>")

    out.append("<h3>Beat-benchmark probability (matched bootstrap paths)</h3>")
    bh = "<tr><th>Strategy</th><th>Metric</th>" + "".join(f"<th>vs {b}</th>" for b in benchmarks) + "</tr>"
    out.append(f"<table>{bh}")
    for s in names:
        for label, col, comp in [
            ("CAGR",   "cagr",   lambda r: r["cagr"]   > r["bench_cagr"]),
            ("Sharpe", "sharpe", lambda r: r["sharpe"] > r["bench_sharpe"]),
            ("MaxDD",  "max_dd", lambda r: r["max_dd"] > r["bench_dd"]),
        ]:
            cells = [s, label]
            empty = True
            for b in benchmarks:
                sub = mc[(mc["strategy"] == s) & (mc["benchmark"] == b)]
                if len(sub) == 0:
                    cells.append("—")
                    continue
                empty = False
                prob = comp(sub).mean()
                color = "#0a0" if prob >= 0.5 else "#c00"
                cells.append(f"<span style='color:{color}'>{prob*100:.1f}%</span>")
            if empty:
                continue
            out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _promotion_decision_html(promotion_analysis: dict, candidate_names: list,
                              deployed_names: list, metrics: dict) -> str:
    """Render the promotion-decision section with correlation matrix,
    portfolio what-if comparison, regime splits, and tail-risk stats."""
    if not promotion_analysis:
        return ""
    out = []
    out.append("<h2>3.5. Promotion-Decision Analyses <span style='font-size:0.6em;color:#666'>(financial-science best practices)</span></h2>")
    out.append("<p style='color:#444;font-size:0.92em;max-width:900px;'>"
               "Beyond standalone CAGR/Sharpe, these tools evaluate each candidate's "
               "<strong>marginal value to the deployed portfolio</strong>. The decision criteria: "
               "(1) low correlation with deployed sleeves, (2) measurable improvement to aggregate metrics, "
               "(3) regime robustness (works across macro environments), (4) acceptable tail risk."
               "</p>")

    # ── (a) Portfolio what-if ──
    what_ifs = promotion_analysis.get("what_ifs", {})
    if what_ifs:
        out.append("<h3>3.5.a. Portfolio what-if (most important table)</h3>")
        out.append("<p style='color:#555;font-size:0.92em;max-width:900px'>"
                   "Simulates injecting each candidate at its proposed allocation into the "
                   "deployed aggregate (other weights renormalized to sum to 1.0). The deltas "
                   "show actual deployment impact on the existing portfolio. <strong>Positive ΔSharpe "
                   "+ negative ΔMaxDD = clear win.</strong></p>")
        out.append("<table style='font-size:13px'><tr>"
                   "<th>Candidate</th><th>Alloc</th>"
                   "<th>Pre Sharpe</th><th>Post Sharpe</th><th>ΔSharpe</th>"
                   "<th>Pre CAGR</th><th>Post CAGR</th><th>ΔCAGR</th>"
                   "<th>Pre MaxDD</th><th>Post MaxDD</th><th>ΔMaxDD</th>"
                   "<th>Window</th></tr>")
        for cn in candidate_names:
            wi = what_ifs.get(cn)
            if not wi:
                continue
            alloc = CANDIDATE_STRATEGIES[cn].get("proposed_alloc", 0) * 100
            pre = wi["pre"]; post = wi["post"]
            ds = wi["delta_sharpe"]
            dc = wi["delta_cagr"] * 100
            dd_delta = wi["delta_maxdd"] * 100
            ds_color = "#0a0" if ds > 0 else "#c00"
            dc_color = "#0a0" if dc > 0 else "#c00"
            dd_color = "#0a0" if dd_delta > 0 else "#c00"
            out.append(f"<tr><td><strong>{cn}</strong></td>"
                       f"<td>{alloc:.1f}%</td>"
                       f"<td>{pre.get('Sharpe',0):.3f}</td><td>{post.get('Sharpe',0):.3f}</td>"
                       f"<td style='color:{ds_color}'><strong>{ds:+.3f}</strong></td>"
                       f"<td>{pre.get('CAGR',0)*100:.2f}%</td><td>{post.get('CAGR',0)*100:.2f}%</td>"
                       f"<td style='color:{dc_color}'><strong>{dc:+.2f}pp</strong></td>"
                       f"<td>{pre.get('Max DD',0)*100:.1f}%</td><td>{post.get('Max DD',0)*100:.1f}%</td>"
                       f"<td style='color:{dd_color}'><strong>{dd_delta:+.2f}pp</strong></td>"
                       f"<td style='font-size:11px;color:#888'>{wi['common_start']} → {wi['common_end']}</td></tr>")
        out.append("</table>")

    # ── (b) Correlation matrix ──
    corr = promotion_analysis.get("correlation")
    if isinstance(corr, pd.DataFrame) and not corr.empty:
        out.append("<h3>3.5.b. Correlation matrix (daily returns, common window)</h3>")
        out.append("<p style='color:#555;font-size:0.92em;max-width:900px'>"
                   "Pairwise daily-return correlation. <strong>Lower correlation with deployed "
                   "sleeves = higher diversification value.</strong> Color: green ≤ 0.3 (genuinely "
                   "uncorrelated); yellow 0.3-0.6 (moderate); red &gt; 0.6 (concentrated).</p>")
        out.append("<table style='font-size:11px;border-collapse:collapse'><tr><th></th>")
        for col in corr.columns:
            short = col.split(":")[0].replace("🌐 ", "").strip()[:20]
            out.append(f"<th style='padding:4px 6px;writing-mode:vertical-rl;text-orientation:mixed;'>{short}</th>")
        out.append("</tr>")
        for idx in corr.index:
            short = idx.split(":")[0].replace("🌐 ", "").strip()[:25]
            row_bold = "🌐" in idx
            out.append(f"<tr><td style='font-weight:{ 'bold' if row_bold else 'normal'};padding:4px 6px'>{short}</td>")
            for col in corr.columns:
                v = corr.loc[idx, col]
                if pd.isna(v):
                    cell = "—"; bg = "transparent"
                else:
                    if idx == col:
                        bg = "#eee"
                    elif abs(v) <= 0.3:
                        bg = "#c8e6c9"
                    elif abs(v) <= 0.6:
                        bg = "#fff9c4"
                    else:
                        bg = "#ffcdd2"
                    cell = f"{v:.2f}"
                out.append(f"<td style='background:{bg};padding:4px 6px;text-align:center'>{cell}</td>")
            out.append("</tr>")
        out.append("</table>")

    # ── (c) Regime splits ──
    regime_data = promotion_analysis.get("regime_data", {})
    if regime_data:
        out.append("<h3>3.5.c. Regime splits (does the candidate work across macro environments?)</h3>")
        out.append("<p style='color:#555;font-size:0.92em;max-width:900px'>"
                   "Per-regime CAGR and Sharpe. A strategy with strong overall metrics but only 1-2 "
                   "good regimes is a one-trick pony. <strong>Look for consistent positive Sharpe "
                   "across all 5 regimes.</strong></p>")
        regime_labels = [r[0] for r in MACRO_REGIMES]
        for cn in candidate_names + ["AGGREGATE (deployed)"]:
            if cn not in regime_data:
                continue
            out.append(f"<h4 style='margin-top:18px'>{cn}</h4>")
            out.append("<table style='font-size:12px'><tr><th>Regime</th><th>CAGR</th><th>Sharpe</th><th>MaxDD</th><th>Total Return</th><th>Days</th></tr>")
            for row in regime_data[cn]:
                m = row.get("metrics", {})
                cagr = m.get("CAGR", np.nan)
                sharpe = m.get("Sharpe", np.nan)
                dd = m.get("Max DD", np.nan)
                tr = m.get("Total Return", np.nan)
                sharpe_color = "#0a0" if not pd.isna(sharpe) and sharpe >= 0.5 else ("#c00" if not pd.isna(sharpe) and sharpe < 0 else "#666")
                def fmt_pct(x): return "—" if pd.isna(x) else f"{x*100:.2f}%"
                def fmt_sr(x): return "—" if pd.isna(x) else f"{x:.2f}"
                out.append(f"<tr><td>{row['label']}</td>"
                           f"<td>{fmt_pct(cagr)}</td>"
                           f"<td style='color:{sharpe_color};font-weight:600'>{fmt_sr(sharpe)}</td>"
                           f"<td>{fmt_pct(dd)}</td>"
                           f"<td>{fmt_pct(tr)}</td>"
                           f"<td style='color:#888;font-size:11px'>{row.get('n_days', 0)}</td></tr>")
            out.append("</table>")

    # ── (d) Tail-risk + pain stats ──
    out.append("<h3>3.5.d. Tail risk & pain endurance</h3>")
    out.append("<p style='color:#555;font-size:0.92em;max-width:900px'>"
               "Beyond MaxDD. <strong>Skewness</strong>: positive = upside-tilted, negative = downside-tilted. "
               "<strong>Excess Kurtosis</strong>: high = fat-tailed (more black-swan risk). "
               "<strong>VaR/CVaR (5%)</strong>: worst monthly loss expected in 1-in-20 months / average of "
               "the worst 5%. <strong>Worst rolling 1y/3y/5y</strong>: 'how long could I be underwater'.</p>")
    out.append("<table style='font-size:12px'><tr>"
               "<th>Strategy</th><th>Skew (mo)</th><th>Excess Kurt</th><th>VaR 5%</th><th>CVaR 5%</th>"
               "<th>Worst 1Y</th><th>Worst 3Y</th><th>Worst 5Y</th><th>Max Days Underwater</th></tr>")
    for cn in candidate_names:
        if cn not in metrics:
            continue
        m = metrics[cn]
        def fmt_pct(x): return "—" if pd.isna(x) else f"{x*100:.2f}%"
        def fmt(x): return "—" if pd.isna(x) else f"{x:.2f}"
        out.append(f"<tr><td><strong>{cn}</strong></td>"
                   f"<td>{fmt(m.get('Skew (monthly)'))}</td>"
                   f"<td>{fmt(m.get('Excess Kurt (monthly)'))}</td>"
                   f"<td>{fmt_pct(m.get('VaR 5% (monthly)'))}</td>"
                   f"<td>{fmt_pct(m.get('CVaR 5% (monthly)'))}</td>"
                   f"<td>{fmt_pct(m.get('Worst 1y Rolling'))}</td>"
                   f"<td>{fmt_pct(m.get('Worst 3y Rolling'))}</td>"
                   f"<td>{fmt_pct(m.get('Worst 5y Rolling'))}</td>"
                   f"<td>{m.get('Max Days Underwater', 0)} ({m.get('Max Days Underwater', 0) / 252:.1f}y)</td></tr>")
    out.append("</table>")
    return "".join(out)


def write_unified_html_report(
    path: str,
    rets_window: tuple,
    deployed_names: list,
    candidate_names: list,
    historic_names: list,
    results: dict,
    metrics: dict,
    aggregate_series: pd.Series,
    aggregate_metrics: dict,
    mc: pd.DataFrame,
    benchmarks: list,
    plots: dict,
    include_historic: bool,
    promotion_analysis: dict = None,
    after_tax_metrics: dict = None,
    tax_logs: dict = None,
    tax_assumptions: dict = None,
    retirement: dict = None,
):
    deployed_alloc = {n: DEPLOYED_STRATEGIES[n]["alloc"] for n in deployed_names if n in DEPLOYED_STRATEGIES}
    deployed_quality = {n: DEPLOYED_STRATEGIES[n].get("quality", "?") for n in deployed_names if n in DEPLOYED_STRATEGIES}
    deployed_earliest = {n: DEPLOYED_STRATEGIES[n].get("earliest") for n in deployed_names if n in DEPLOYED_STRATEGIES}
    deployed_sources = {n: DEPLOYED_STRATEGIES[n].get("data_sources", "") for n in deployed_names if n in DEPLOYED_STRATEGIES}
    candidate_proposed = {n: CANDIDATE_STRATEGIES[n].get("proposed_alloc", 0) for n in candidate_names if n in CANDIDATE_STRATEGIES}
    candidate_quality = {n: CANDIDATE_STRATEGIES[n].get("quality", "?") for n in candidate_names if n in CANDIDATE_STRATEGIES}
    candidate_earliest = {n: CANDIDATE_STRATEGIES[n].get("earliest") for n in candidate_names if n in CANDIDATE_STRATEGIES}
    candidate_sources = {n: CANDIDATE_STRATEGIES[n].get("data_sources", "") for n in candidate_names if n in CANDIDATE_STRATEGIES}
    historic_reasons = {n: HISTORIC_STRATEGIES[n].get("reason_demoted", "") for n in historic_names if n in HISTORIC_STRATEGIES}

    # Executive summary numbers
    agg_cagr = aggregate_metrics.get("CAGR", 0) * 100
    agg_sharpe = aggregate_metrics.get("Sharpe", 0)
    agg_maxdd = aggregate_metrics.get("Max DD", 0) * 100
    agg_worst = aggregate_metrics.get("Worst Year", 0) * 100
    agg_years = aggregate_metrics.get("Years", 0)

    # After-tax aggregate KPIs
    after_tax_metrics = after_tax_metrics or {}
    tax_logs = tax_logs or {}
    tax_assumptions = tax_assumptions or {}
    agg_at = after_tax_metrics.get("AGGREGATE (deployed)", {})
    agg_at_cagr = agg_at.get("After-Tax CAGR", 0) * 100 if agg_at else None
    agg_drag_pp = agg_at.get("Tax Drag (pp)", 0) if agg_at else None
    # Tile color: green if drag < 1pp, amber 1-2pp, red > 2pp
    if agg_drag_pp is None:
        agg_drag_tile_class = ""
    elif agg_drag_pp < 1.0:
        agg_drag_tile_class = "positive"
    elif agg_drag_pp < 2.0:
        agg_drag_tile_class = "warn"
    else:
        agg_drag_tile_class = "negative"

    # Retirement KPIs (median MC years-to-target + age)
    retirement_tiles_html = ""
    if retirement is not None and retirement.get("mc") is not None:
        yrs = retirement["mc"]["years_to_target"]
        finite = yrs[np.isfinite(yrs)]
        if len(finite) >= 10:
            p50_yrs = float(np.median(finite))
            ret_age = retirement["age_today"] + p50_yrs
            retirement_tiles_html = (
                f'<div class="kpi positive"><div class="label">Years to FI '
                f'<span style="font-size:0.7em;color:#999">(MC p50)</span></div>'
                f'<div class="value">{p50_yrs:.1f}</div></div>'
                f'<div class="kpi positive"><div class="label">Age at retirement '
                f'<span style="font-size:0.7em;color:#999">(MC p50)</span></div>'
                f'<div class="value">{ret_age:.1f}</div></div>'
            )

    # Bronze (candidate) section
    candidate_section = ""
    for cn in candidate_names:
        if cn not in metrics:
            continue
        m = metrics[cn]
        spec = CANDIDATE_STRATEGIES[cn]
        cagr = m.get("CAGR", 0) * 100
        sharpe = m.get("Sharpe", 0)
        dd = m.get("Max DD", 0) * 100
        worst = m.get("Worst Year", 0) * 100
        rationale = spec.get("rationale", "")
        proposed = spec.get("proposed_alloc", 0) * 100
        evaluation_started = spec.get("evaluation_started", "")
        candidate_section += f"""
        <div class="candidate-card">
            <h3>{cn}</h3>
            <p class="card-meta">Proposed allocation: <strong>{proposed:.0f}%</strong> · Evaluation started: {evaluation_started}</p>
            <p class="rationale">{rationale}</p>
            <table class="kpi-table">
                <tr><th>CAGR</th><th>Sharpe</th><th>Max DD</th><th>Worst Year</th><th>Coverage</th></tr>
                <tr><td>{cagr:.2f}%</td><td>{sharpe:.2f}</td><td>{dd:.2f}%</td><td>{worst:.2f}%</td><td>{_coverage_window(results[cn])}</td></tr>
            </table>
        </div>
        """

    # Plots: only embed if file exists
    plot_html = ""
    for label, fname in plots.items():
        if fname and os.path.exists(fname):
            plot_html += f"<h3>{label}</h3><img src='{os.path.basename(fname)}'>"

    html = f"""<!DOCTYPE html><html><head>
<meta charset="utf-8">
<title>Mega Backtest — Unified 1970–2026</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width:1500px; margin:30px auto; padding:24px; color:#222; }}
h1 {{ border-bottom:3px solid #000; padding-bottom:12px; }}
h2 {{ border-bottom:2px solid #888; padding-bottom:8px; margin-top:40px; color:#222; }}
h3 {{ margin-top:24px; color:#444; }}
table {{ border-collapse:collapse; width:100%; margin:14px 0; font-size:0.92em; }}
th, td {{ padding:8px 10px; text-align:right; border-bottom:1px solid #e0e0e0; font-variant-numeric:tabular-nums; }}
th {{ background:#f4f4f4; text-align:left; font-weight:600; }}
td:first-child, th:first-child {{ text-align:left; }}
.kpi-row {{ display:flex; gap:20px; margin:20px 0; flex-wrap:wrap; }}
.kpi {{ background:#f8f8f8; border-left:4px solid #444; padding:14px 18px; flex:1; min-width:180px; }}
.kpi .label {{ color:#666; font-size:0.85em; text-transform:uppercase; letter-spacing:0.5px; }}
.kpi .value {{ font-size:1.8em; font-weight:700; margin-top:4px; }}
.kpi.positive {{ border-left-color:#0a0; }}
.kpi.warn {{ border-left-color:#f80; }}
.kpi.negative {{ border-left-color:#c00; }}
.candidate-card {{ background:#fffaf0; border-left:5px solid #c88; padding:18px 24px; margin:20px 0; }}
.kpi-table {{ background:white; }}
.card-meta {{ color:#666; font-size:0.92em; }}
.rationale {{ font-style:italic; color:#555; margin:8px 0; }}
.warn-box {{ background:#fff8e1; border-left:4px solid #f80; padding:12px 16px; margin:18px 0; }}
img {{ max-width:100%; margin:14px 0; }}
.tier-deployed {{ border-left:5px solid #0a0; padding-left:12px; }}
.tier-candidate {{ border-left:5px solid #c88; padding-left:12px; }}
.tier-historic {{ border-left:5px solid #888; padding-left:12px; }}

/* Strategy explainer cards */
.strategy-card {{
    background:#fafafa; border:1px solid #e0e0e0; border-radius:6px;
    padding:18px 22px; margin:14px 0;
}}
.card-header h4 {{ margin:0 0 6px 0; color:#222; font-size:1.05em; }}
.strategy-kind {{ color:#666; font-weight:normal; font-size:0.92em; }}
.card-kpi-row {{ display:flex; gap:10px; margin:8px 0 12px 0; flex-wrap:wrap; }}
.kpi-chip {{
    background:white; border:1px solid #ddd; border-radius:4px;
    padding:4px 10px; font-size:0.85em; font-variant-numeric:tabular-nums;
}}
.kpi-chip strong {{ color:#444; margin-right:6px; }}
.card-mechanism {{ margin:8px 0; line-height:1.5; }}
.holdings-table {{ font-size:0.9em; margin:6px 0 12px 0; }}
.holdings-table td {{ padding:4px 8px; }}
.holdings-table td:first-child {{ width:90px; }}
.holdings-table td:nth-child(2) {{ width:150px; color:#666; }}
.card-detail-grid {{
    display:grid; grid-template-columns:1fr 1fr; gap:14px;
    margin:10px 0; padding:10px 12px; background:#fff; border-radius:4px;
    font-size:0.92em;
}}
.card-wins-loses {{
    display:grid; grid-template-columns:1fr 1fr; gap:12px;
    margin:10px 0; font-size:0.92em;
}}
.card-wins {{ background:#e8f5e9; border-left:3px solid #0a0; padding:8px 12px; border-radius:3px; }}
.card-loses {{ background:#ffebee; border-left:3px solid #c00; padding:8px 12px; border-radius:3px; }}
.card-source {{ color:#666; font-size:0.85em; font-style:italic; margin:6px 0 0 0; }}
.muted {{ color:#666; }}
.glossary-table {{ font-size:0.9em; margin:8px 0 18px 0; }}
.glossary-table td:first-child {{ width:80px; font-weight:600; }}
.glossary-table td:nth-child(2) {{ width:280px; color:#444; }}
</style></head><body>

<h1>Mega Backtest — Unified 1970–2026</h1>
<p><em>Generated {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')} ·
Window {rets_window[0]} → {rets_window[1]} ·
Extended history via Testfolio SIMs (SPYSIM 1885+, EFASIM 1970+, GLDSIM 1968+, NTSDSIM 1970+, URTHSIM 1970+, QQQSIM 1986+, BNDSIM 1986+, SLVSIM 1968+, KMLMSIM 1988+) spliced with real ETFs at inception. Remaining mutual-fund proxies: VUSTX→TLT, VFITX→IEF, VBMFX→AGG, VIPSX→TIP, VWEHX→HYG, VWESX→LQD. EODHD / FRED / Alpaca</em></p>

<h2>1. Executive Summary</h2>
<p>Aggregate of the {len(deployed_names)} deployed strategies, weighted at production allocations,
with partial-coverage renormalization (strategies that don't extend to early dates have their
weight redistributed pro-rata among available sleeves at each point in time).</p>
<div class="kpi-row">
    <div class="kpi positive"><div class="label">CAGR (gross)</div><div class="value">{agg_cagr:.2f}%</div></div>
    {f'<div class="kpi positive"><div class="label">After-Tax CAGR <span style="font-size:0.7em;color:#999">(DE)</span></div><div class="value">{agg_at_cagr:.2f}%</div></div>' if agg_at_cagr is not None else ''}
    {f'<div class="kpi {agg_drag_tile_class}"><div class="label">Annual Tax Drag</div><div class="value">−{agg_drag_pp:.2f}pp</div></div>' if agg_drag_pp is not None else ''}
    <div class="kpi positive"><div class="label">Sharpe</div><div class="value">{agg_sharpe:.2f}</div></div>
    <div class="kpi negative"><div class="label">Max Drawdown</div><div class="value">{agg_maxdd:.1f}%</div></div>
    <div class="kpi warn"><div class="label">Worst Year</div><div class="value">{agg_worst:.1f}%</div></div>
    <div class="kpi"><div class="label">Years tested</div><div class="value">{agg_years:.1f}</div></div>
    {retirement_tiles_html}
</div>

{_retirement_section_html(retirement) if retirement is not None else ''}

<h3>Currently qualified candidate</h3>
{candidate_section if candidate_section else "<p><em>No active candidate.</em></p>"}

<h2>2. Deployed Strategies <span style="font-size:0.6em;color:#666;">({len(deployed_names)} sleeves currently in production)</span></h2>
<p style="color:#555;font-size:0.92em">
<strong>Native CAGR/Sharpe/MaxDD</strong> = strategy's own coverage window (e.g. Regime SSO starts 1990-10 because VIX history begins 1990).
<strong>Full</strong> = 1970-2026 window (older-floor strategies show shorter coverage but the same start date).
Use Native for fair cross-strategy comparison; Full for the aggregate-portfolio view.
</p>
<div class="tier-deployed">
{_metrics_table_html(deployed_names, results, metrics, with_alloc=deployed_alloc,
                     with_quality=deployed_quality, with_native=deployed_earliest,
                     with_after_tax=after_tax_metrics)}
</div>

<h3>Strategy-by-strategy explainers</h3>
<p style="color:#555;font-size:0.92em">Each deployed strategy explained below: what tickers it holds, how positions change, when it works, when it doesn't.</p>
{''.join(_strategy_card_html(
    n, DEPLOYED_STRATEGIES[n].get("fn", ""),
    DEPLOYED_STRATEGIES[n].get("alloc", 0),
    "deployed",
    _native_window_metrics(results[n], DEPLOYED_STRATEGIES[n].get("earliest")) if n in results else {},
    DEPLOYED_STRATEGIES[n]
) for n in deployed_names if n in DEPLOYED_STRATEGIES)}

<h3>Data sources per deployed strategy</h3>
<table><tr><th>Strategy</th><th>Earliest</th><th>Quality</th><th>Inputs</th></tr>
{''.join(f"<tr><td>{n}</td><td>{deployed_earliest[n]}</td><td>{_quality_badge(deployed_quality[n])}</td><td>{deployed_sources[n]}</td></tr>" for n in deployed_names)}
</table>

<h2>3. Qualified Candidate Strategies <span style="font-size:0.6em;color:#666;">({len(candidate_names)} strategies under final evaluation)</span></h2>
<div class="tier-candidate">
{_metrics_table_html(candidate_names, results, metrics, with_proposed=candidate_proposed,
                     with_quality=candidate_quality, with_native=candidate_earliest)}
</div>

<h3>Candidate explainers</h3>
{''.join(_strategy_card_html(
    n, CANDIDATE_STRATEGIES[n].get("fn", ""),
    CANDIDATE_STRATEGIES[n].get("proposed_alloc", 0),
    "candidate",
    _native_window_metrics(results[n], CANDIDATE_STRATEGIES[n].get("earliest")) if n in results else {},
    CANDIDATE_STRATEGIES[n]
) for n in candidate_names if n in CANDIDATE_STRATEGIES)}

<table><tr><th>Strategy</th><th>Earliest</th><th>Quality</th><th>Inputs</th></tr>
{''.join(f"<tr><td>{n}</td><td>{candidate_earliest[n]}</td><td>{_quality_badge(candidate_quality[n])}</td><td>{candidate_sources[n]}</td></tr>" for n in candidate_names)}
</table>

{_promotion_decision_html(promotion_analysis, candidate_names, deployed_names, metrics)}

<h2>4. Stress-Period Analysis <span style="font-size:0.6em;color:#666;">({len(STRESS_WINDOWS)} historical regimes)</span></h2>
<p>Total return per strategy in each window. Bonus: the 2000–2002 dot-com bear and the 1995–2000 melt-up
are now both included thanks to the extended-history splice.</p>
{_stress_table_html(["AGGREGATE (deployed)"] + candidate_names + benchmarks, {**results, "AGGREGATE (deployed)": aggregate_series}, STRESS_WINDOWS)}

<h2>5. Monte Carlo Robustness <span style="font-size:0.6em;color:#666;">(stationary block bootstrap, mean block 63 days)</span></h2>
<p>Joint resampling preserves cross-asset comovement: SPY/HFEA/DM/etc. are sampled together so cross-strategy
correlations stay intact in each simulated path. Strategies sorted by deterministic Sharpe.</p>
{_mc_comparison_html(mc, metrics, deployed_names + candidate_names + ["AGGREGATE"] + benchmarks, benchmarks)}

<h2>6. Historic Strategy Universe <span style="font-size:0.6em;color:#666;">(re-mine for future candidate ideas)</span></h2>
<p>Every strategy ever tested in this research stream that wasn't promoted. Preserved as a
universe to draw new candidates from. Run with <code>--include-historic</code> to compute
their actual metrics on the current spliced 1970→2026 data; otherwise this table is shown
without metrics.</p>
<div class="tier-historic">
"""

    if include_historic:
        html += _metrics_table_html(historic_names, results, metrics, with_reason=historic_reasons)
    else:
        # Static table — names + last-known reason_demoted only
        rows = []
        for n in historic_names:
            spec = HISTORIC_STRATEGIES.get(n, {})
            rows.append(f"<tr><td>{n}</td><td>{spec.get('fn','')}</td><td>{spec.get('tested','')}</td><td>{spec.get('reason_demoted','')}</td></tr>")
        html += f"<table><tr><th>Strategy</th><th>Function</th><th>Tested</th><th>Reason demoted</th></tr>{''.join(rows)}</table>"

    html += f"""
</div>

<h2>7. Discontinued Strategies <span style="font-size:0.6em;color:#666;">({len(DISCONTINUED_STRATEGIES)} actively rejected — do NOT re-test)</span></h2>
<p style="color:#555;font-size:0.92em">
Strategies that have been <strong>actively rejected</strong> after testing.
Unlike Historic (which is a universe to re-mine for new candidates), these
will <strong>not</strong> be reconsidered — they're documented here so we
don't accidentally re-run the same disappointments.
</p>
<div class="tier-historic" style="border-left:5px solid #c00;">
"""

    # Discontinued strategies — always static (we don't re-run them even with --include-historic)
    disc_rows = []
    for n, spec in DISCONTINUED_STRATEGIES.items():
        disc_rows.append(
            f"<tr><td>{n}</td><td><code>{spec.get('fn','')}</code></td>"
            f"<td>{spec.get('discontinued','')}</td>"
            f"<td style='color:#a00'>{spec.get('reason','')}</td></tr>"
        )
    html += (
        "<table>"
        "<tr><th>Strategy</th><th>Function</th><th>Discontinued</th><th>Reason rejected</th></tr>"
        + "".join(disc_rows) +
        "</table>"
    )

    html += f"""
</div>

<h2>8. Ticker Glossary</h2>
<p style="color:#555;font-size:0.92em">Every ETF / synthetic ticker referenced in the deployed and candidate strategies above, grouped by asset class.</p>
{_ticker_glossary_html()}

<h2>9. Equity curves, drawdowns, rolling Sharpe</h2>
{plot_html}

{_tax_section_html(deployed_names, deployed_alloc, after_tax_metrics, tax_logs,
                    tax_assumptions, metrics, aggregate_metrics)}

<h2>10. Methodology &amp; Caveats</h2>
<div class="warn-box">
<strong>Data quality tiers (lowest tier on a strategy's input chain caps its overall grade):</strong>
{_quality_badge("A")} Real ETF, Testfolio sim, or proxy with corr ≥ 0.90 vs the target.<br>
{_quality_badge("B")} Acceptable proxy with modest mismatch (corr 0.75–0.90 or basket/duration variance).<br>
{_quality_badge("C")} Caveated proxy — use the metrics directionally only.<br>
{_quality_badge("D")} Unreliable for the pre-real window; pre-splice numbers should not be trusted.<br>

<br><strong>Primary data layer (Tier A):</strong>
Testfolio SIM CSVs are used for the equity, gold, bond, intl, Nasdaq, silver and KMLM legs:
<ul>
<li><strong>SPYSIM (1885+)</strong> → real SPY at 1993-01-29. Replaces VFINX proxy.</li>
<li><strong>EFASIM (1970+)</strong> → real EFA at 2001-08-14. Replaces AEPGX (active fund, ~27% lower vol than EFA — meaningful contamination eliminated).</li>
<li><strong>GLDSIM (1968+)</strong> → real GLD at 2004-11-18. <strong>Replaces the XAU mining-stock proxy</strong> which had 2× the vol of spot gold and equity-like behavior — every pre-2004 backtest of a gold-using strategy is now on real gold, not miners.</li>
<li><strong>QQQSIM (1986+)</strong> → real QQQ at 1999-03-10. Unlocks 9-Sig + DM in the dot-com era.</li>
<li><strong>BNDSIM (1986+)</strong> → real BND at 2007-04-03. Fixes DM's defensive leg pre-2007.</li>
<li><strong>SLVSIM (1968+)</strong> → real SLV at 2006-04-21. Preserves legit Hunt-Brothers-era spikes.</li>
<li><strong>NTSDSIM (1970+)</strong> → real NTSD at 2026-03-19. Replaces analytical 0.9×SPY + 0.6×(EFA−BIL) formula.</li>
<li><strong>URTHSIM (1970+)</strong> → real URTH at 2012-01-12. Fixes MSCI World/WLDU pre-2008.</li>
<li><strong>KMLMSIM (1988+)</strong> → real KMLM at 2020-12-02. Replaces TMF proxy in HFEA's KMLM slot.</li>
</ul>

<strong>Mutual-fund proxies still in use (Tier A/B):</strong>
VUSTX→TLT (corr 0.96 at boundary), VFITX→IEF (0.93, slight duration variance),
VBMFX→AGG (0.81, clean), VIPSX→TIP (0.90), VWESX→LQD (0.79, duration mismatch — Tier B),
VWEHX→HYG (0.47, conservative HY fund &lt; HYG credit risk — <strong>Tier C, deployed strategies do not use this</strong>).
<br><br>
<strong>Outlier guard:</strong> single-day moves &gt; 25% in raw EODHD proxies are treated as data spikes and carried forward.
Testfolio SIM CSVs are trusted as-is (legit 1987/2008/2020 crash days preserved).
Series are reindexed to SPY's trading calendar to suppress holiday-only spikes.
<br><br>
<strong>Leveraged ETFs (UPRO/TMF/SPUU/QLD/EFO/SSO/SPXL/TQQQ/SAA/EET/UBT/UGL/etc.):</strong>
Synthetic before live inception, then <strong>real ETF returns from EODHD post-inception</strong>.
The synthetic formula matches Testfolio's public <code>?L=N</code> spec exactly: SW=1.1 swap
multiplier, SP=0.4% spread above FFR, E=0.5%×(L−1) drag, with time-varying borrow rate from
FRED DGS3MO (extended pre-1981 via TB3MS). Leverage decay (volatility drag) is captured
through daily compounding.
<br><br>
<strong>Design choice — real ETF splice vs synth-everywhere:</strong> We use real ETF data
when available. Testfolio uses their synthetic <code>SPYSIM?L=3</code> / <code>TLTSIM?L=3</code>
throughout (no real ETF splice). The all-synthetic approach gives cleaner CAGR numbers but
doesn't capture real ETF tracking error / slippage. Our approach is more realistic for
live-portfolio comparison but produces ~2.4pp/yr higher HFEA CAGR than Testfolio's full-synthetic
model (annual returns correlate 0.97 between the two approaches — same risk regime, different
methodology).
<br><br>
<strong>RSSB / WTIP:</strong> pre-2023-12 (RSSB) and pre-2025-06 (WTIP) reconstructed from components.
Synthetic-WTIP vs live-WTIP correlation ~0.57.
<br><br>
<strong>Regime models:</strong> Use 5 of 7 signals — news omitted, breadth simplified.
<br><br>
<strong>9-Sig:</strong> omits the "30 Down, Stick Around" crash filter for simplicity.
<br><br>
<strong>Dividend reinvestment:</strong> embedded in EODHD/Alpaca adjusted closes.
<strong>Whole-share / fractional constraints:</strong> not modelled.
<strong>Transaction costs:</strong> not modelled.
</div>

<h3>Glossary</h3>
<ul>
<li><strong>CAGR</strong> — Compound annual growth rate.</li>
<li><strong>Sharpe</strong> — (CAGR − 2%) ÷ annualized volatility.</li>
<li><strong>Max DD</strong> — Worst peak-to-trough drawdown over the full window.</li>
<li><strong>Calmar</strong> — CAGR ÷ |Max DD|.</li>
<li><strong>Worst Year</strong> — Worst calendar-year total return.</li>
<li><strong>Stationary block bootstrap</strong> — Politis-Romano (1994) Monte Carlo
resampling that preserves time-series autocorrelation. Joint resampling here means SPY/HFEA/DM are
sampled with the same date indices so cross-strategy correlations stay intact.</li>
<li><strong>NTSD</strong> — WisdomTree US Plus Intl Equity. Capital-efficient stack of
0.9× SPY + 0.6× EFA exposure on $1 of capital — no daily reset, no leverage decay.</li>
</ul>

</body></html>"""
    with open(path, "w") as f:
        f.write(html)


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════


def _parse_arg_float(flag: str, default: float | None) -> float | None:
    """Parse `--flag VALUE` or `--flag=VALUE` from sys.argv. Returns default if absent."""
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            try:
                return float(sys.argv[i + 1])
            except ValueError:
                return default
        if a.startswith(flag + "="):
            try:
                return float(a.split("=", 1)[1])
            except ValueError:
                return default
    return default


def main():
    include_historic = "--include-historic" in sys.argv
    skip_mc = "--no-mc" in sys.argv
    skip_plots = "--no-plots" in sys.argv
    skip_retirement = "--no-retirement" in sys.argv

    # Retirement overrides (all optional)
    cli_starting = _parse_arg_float("--starting-equity", None)
    cli_monthly = _parse_arg_float("--monthly", RETIREMENT_DEFAULT_MONTHLY)
    cli_target = _parse_arg_float("--target", RETIREMENT_TARGET_REAL)
    cli_inflation = _parse_arg_float("--inflation", RETIREMENT_INFLATION)
    cli_swr = _parse_arg_float("--swr", RETIREMENT_SWR)

    print("=" * 78)
    print(" MEGA BACKTEST — Unified 1970–2026")
    print("=" * 78)
    print(f" Tiers: {len(DEPLOYED_STRATEGIES)} deployed · "
          f"{len(CANDIDATE_STRATEGIES)} candidate · "
          f"{len(HISTORIC_STRATEGIES)} historic"
          f" {'(included)' if include_historic else '(skipped — use --include-historic)'}")
    print("=" * 78)

    # 1. Data
    closes, bars, vix_df, fed_df = fetch_all_data()
    if "SPY" not in closes:
        raise RuntimeError("SPY data missing — cannot proceed")
    common_idx = closes["SPY"].index
    px = pd.DataFrame(closes).reindex(common_idx).ffill()
    rets = px.pct_change().fillna(0)
    print(f"\nReturn series: {len(rets)} rows, {rets.index[0].date()} → {rets.index[-1].date()}")

    results: dict[str, pd.Series] = {}

    # 2. Benchmarks
    results["100% SPY"] = rets["SPY"]
    if "URTH" in rets.columns:
        results["100% URTH (MSCI World)"] = rets["URTH"]
    print("  ✓ benchmarks")
    benchmarks = [n for n in ("100% SPY", "100% URTH (MSCI World)") if n in results]

    # 3. Deployed
    print(f"\n── DEPLOYED ({len(DEPLOYED_STRATEGIES)}) ──")
    deployed_ran: list[str] = []
    deployed_weights: dict = {}   # strategy name → {"weights": df, "asset_returns": df}
    for name, spec in DEPLOYED_STRATEGIES.items():
        if run_strategy(name, spec, results, rets, px, vix_df, fed_df,
                        weights_out=deployed_weights):
            deployed_ran.append(name)

    # 4. Candidate
    print(f"\n── QUALIFIED CANDIDATE ({len(CANDIDATE_STRATEGIES)}) ──")
    candidate_ran: list[str] = []
    for name, spec in CANDIDATE_STRATEGIES.items():
        if run_strategy(name, spec, results, rets, px, vix_df, fed_df):
            candidate_ran.append(name)

    # 5. Historic (optional)
    historic_ran: list[str] = []
    if include_historic:
        print(f"\n── HISTORIC ({len(HISTORIC_STRATEGIES)}) ──")
        for name, spec in HISTORIC_STRATEGIES.items():
            if run_strategy(name, spec, results, rets, px, vix_df, fed_df):
                historic_ran.append(name)
    else:
        # Don't run, but keep names for the static report table
        historic_ran = list(HISTORIC_STRATEGIES.keys())

    # 6. Partial-coverage aggregate (deployed only)
    print("\nComputing partial-coverage aggregate over deployed strategies")
    aggregate_series = compute_partial_coverage_aggregate(results, DEPLOYED_STRATEGIES, common_idx)
    results["AGGREGATE (deployed)"] = aggregate_series

    # 6b. After-tax overlay (German Abgeltungsteuer + Soli + Vorabpauschale)
    # — Berlin, non-church-member, Alpaca US broker. See research/tax_overlay.py.
    import importlib
    _sys_path_added = os.path.dirname(os.path.abspath(__file__))
    if _sys_path_added not in sys.path:
        sys.path.insert(0, _sys_path_added)
    tax_overlay = importlib.import_module("tax_overlay")
    print(f"\n── German tax overlay (effective rate {tax_overlay.EFFECTIVE_TAX_RATE*100:.3f}%) ──")
    after_tax_results: dict = {}
    tax_logs: dict = {}
    tax_assumptions: dict = {}
    for name in deployed_ran:
        bundle = deployed_weights.get(name)
        if not bundle:
            print(f"  ⚠ {name}: no weights captured (strategy not tax-instrumented)")
            continue
        tw = bundle["weights"]
        ar = bundle["asset_returns"]
        if tw is None or tw.empty:
            continue
        # Confine asset_returns to the strategy's earliest-onward window
        earliest = DEPLOYED_STRATEGIES[name].get("earliest")
        ar_clip = ar.loc[ar.index >= pd.Timestamp(earliest)] if earliest else ar
        after_tax, tax_log = tax_overlay.simulate_after_tax(tw, ar_clip)
        after_tax_results[name] = after_tax
        tax_logs[name] = tax_log
        # Average realized annual turnover from the simulator's per-year buy volume
        avg_turn = float(tax_log["turnover"].mean()) if "turnover" in tax_log.columns and not tax_log.empty else 0.0
        tax_assumptions[name] = {
            "annual_turnover": avg_turn,
            "intentional_turnover": tax_overlay.annualized_turnover(tw),
            "weighted_tfs": tax_overlay.time_weighted_tfs(tw),
            "tickers": list(tw.columns),
        }
        at_metrics = tax_overlay.compute_after_tax_metrics(after_tax, results[name])
        drag = at_metrics.get("Tax Drag (pp)")
        if drag is not None:
            print(f"  ✓ {name}: turnover {avg_turn*100:.0f}%/yr · "
                  f"TFS {tax_assumptions[name]['weighted_tfs']*100:.0f}% · "
                  f"drag {drag:+.2f}pp/yr")
    # Aggregate after-tax via the same partial-coverage helper
    after_tax_aggregate = compute_partial_coverage_aggregate(
        after_tax_results, DEPLOYED_STRATEGIES, common_idx
    )
    after_tax_results["AGGREGATE (deployed)"] = after_tax_aggregate

    # 7. Metrics
    print("Computing metrics for all run strategies")
    metrics = {name: compute_metrics(r) for name, r in results.items()}
    aggregate_metrics = metrics["AGGREGATE (deployed)"]

    # After-tax metrics (CAGR/Sharpe/MaxDD/Drag/TaxCostRatio) per strategy + aggregate
    after_tax_metrics: dict = {}
    for name in deployed_ran + ["AGGREGATE (deployed)"]:
        if name not in after_tax_results or name not in results:
            continue
        after_tax_metrics[name] = tax_overlay.compute_after_tax_metrics(
            after_tax_results[name], results[name]
        )

    # Build a sorted summary table (text)
    table = fmt_table(metrics)
    if "Sharpe" in table.columns:
        # Sort by raw Sharpe — fmt_table already turned it into a string, so re-sort using raw dict
        sort_key = lambda n: (metrics[n].get("Sharpe") if metrics[n].get("Sharpe") is not None else -99)
        ordered = sorted(metrics.keys(), key=sort_key, reverse=True)
        table = table.reindex(ordered)

    print("\n" + "=" * 78)
    print(" RESULTS — sorted by Sharpe")
    print("=" * 78)
    print(table.to_string())
    table.to_csv("results.csv")

    # 8. Stress periods (text)
    print(f"\n── Stress / sub-period total returns ({len(STRESS_WINDOWS)} windows) ──")
    stress_targets = ["AGGREGATE (deployed)"] + candidate_ran + benchmarks
    stress_rows = []
    for label, start, end, _desc in STRESS_WINDOWS:
        row = {"period": label, "window": f"{start} → {end}"}
        for t in stress_targets:
            s = results.get(t)
            if s is None:
                row[t] = "—"
                continue
            sub = s.loc[start:end].dropna()
            row[t] = f"{(1 + sub).prod() - 1:.2%}" if len(sub) >= 5 else "—"
        stress_rows.append(row)
    stress_df = pd.DataFrame(stress_rows).set_index("period")
    print(stress_df.to_string())
    stress_df.to_csv("stress_periods.csv")

    # 9. Monte Carlo — always run for deployed strategies + benchmarks + aggregate.
    # `--no-mc` only skips the candidate-extension MC (kept for fast iteration when
    # there are candidates under evaluation). The deployed + SPY/URTH MC always runs
    # because it's the core robustness view of the production portfolio.
    mc = None
    print("\n── Monte Carlo robustness (per-strategy bootstrap on native window) ──")
    try:
        # Targets always include all deployed + both benchmarks. Candidates
        # added only when --no-mc is NOT set (they're noisy and slow when many).
        mc_targets = list(deployed_ran) + [b for b in benchmarks if b in results]
        if not skip_mc:
            mc_targets += candidate_ran
        mc = monte_carlo_per_strategy(
            returns_dict=results,
            strategies=mc_targets,
            benchmarks=benchmarks,
            aggregate_series=aggregate_series,
            n_sims=2000,
            mean_block=63,
            seed=42,
        )
        det = {n: metrics.get(n, {}) for n in mc_targets}
        det["AGGREGATE"] = aggregate_metrics
        report_monte_carlo_per_strategy(mc, det, benchmarks=benchmarks)
        mc.to_csv("monte_carlo_distribution.csv", index=False)
    except Exception as e:
        import traceback
        print(f"  Monte Carlo failed: {e}")
        traceback.print_exc()
        mc = None

    # 9b. Retirement projection — forward project the aggregate into the future
    # at the user's contribution rate. All math is in REAL dollars (today's $).
    retirement = None
    if not skip_retirement:
        print("\n── Retirement projection ──")
        starting = cli_starting
        if starting is None:
            starting = fetch_alpaca_live_equity()
        if starting is None:
            starting = float(os.environ.get("STARTING_EQUITY", 0))
        if starting <= 0:
            print("  ⚠ No starting equity (Alpaca fetch failed and no --starting-equity / "
                  "STARTING_EQUITY override). Skipping retirement section.")
        else:
            agg_at = after_tax_metrics.get("AGGREGATE (deployed)", {})
            after_tax_cagr = agg_at.get("After-Tax CAGR")
            if after_tax_cagr is None:
                # Fall back to gross aggregate CAGR if tax overlay didn't compute
                after_tax_cagr = aggregate_metrics.get("CAGR", 0.0)
                cagr_label = "gross CAGR (tax overlay unavailable)"
            else:
                cagr_label = "after-tax CAGR"
            real_cagr = (1 + after_tax_cagr) / (1 + cli_inflation) - 1.0
            age_today = retirement_age_today(RETIREMENT_BIRTH_DATE)

            print(f"  Live equity:       ${starting:,.0f}")
            print(f"  Age today:         {age_today:.2f}  (born {RETIREMENT_BIRTH_DATE})")
            print(f"  Monthly contrib:   ${cli_monthly:,.0f} (real)")
            print(f"  Target:            ${cli_target:,.0f} (real)")
            print(f"  Inflation:         {cli_inflation*100:.2f}%/yr")
            print(f"  {cagr_label}:    {after_tax_cagr*100:.2f}%/yr nominal")
            print(f"  Real CAGR:         {real_cagr*100:.2f}%/yr")
            print(f"  SWR income at target: ${cli_target * cli_swr:,.0f}/yr (today's $)")

            # Deterministic projection
            det = project_wealth_deterministic(starting, cli_monthly, real_cagr,
                                                cli_target, RETIREMENT_MAX_YEARS)
            yrs_det = det["years_to_target"]
            if np.isfinite(yrs_det):
                print(f"  → Deterministic: {yrs_det:.1f} yrs → age {age_today + yrs_det:.1f}")
            else:
                print(f"  → Deterministic: target not reached within {RETIREMENT_MAX_YEARS} years "
                      f"(final wealth ${det['final_wealth']:,.0f})")

            # Sensitivity grid
            sens = retirement_sensitivity_grid(starting, real_cagr, age_today)
            print("\n  Sensitivity: years to reach target (monthly ↓ × target →)")
            sens_disp = sens.copy()
            for col in sens_disp.columns:
                sens_disp[col] = sens_disp[col].apply(
                    lambda y: f"{y:5.1f}y (age {age_today + y:4.1f})"
                              if np.isfinite(y) else "  never  "
                )
            sens_disp.columns = [f"${t/1e6:.1f}M" for t in sens_disp.columns]
            sens_disp.index = [f"${m:,}/mo" for m in sens_disp.index]
            print(sens_disp.to_string())

            # Monte Carlo projection (real dollars). Bootstrap from the
            # AFTER-TAX aggregate so MC and deterministic are apples-to-apples.
            # (Gross aggregate would understate tax drag and produce
            # over-optimistic retirement-age percentiles.)
            mc_source = after_tax_results.get("AGGREGATE (deployed)")
            if mc_source is None or mc_source.dropna().empty:
                mc_source = aggregate_series
                print("  ⚠ Using gross aggregate for MC (after-tax aggregate unavailable)")
                mc_basis = "gross"
            else:
                mc_basis = "after-tax"
            print(f"\n  Monte Carlo retirement bootstrap (n={RETIREMENT_MC_SIMS}, "
                  f"real $, {mc_basis} returns)...")
            mc_ret = monte_carlo_retirement(
                mc_source, starting, cli_monthly, cli_target,
                cli_inflation, RETIREMENT_MAX_YEARS,
                n_sims=RETIREMENT_MC_SIMS, mean_block=63, seed=42,
            )
            if mc_ret is not None:
                yrs = mc_ret["years_to_target"]
                finite = yrs[np.isfinite(yrs)]
                pct_never = (1 - len(finite) / len(yrs)) * 100
                if len(finite) >= 10:
                    p5_y, p50_y, p95_y = np.percentile(finite, [5, 50, 95])
                    print(f"  → MC years-to-target: p5 {p5_y:.1f} (age {age_today+p5_y:.1f}) · "
                          f"p50 {p50_y:.1f} (age {age_today+p50_y:.1f}) · "
                          f"p95 {p95_y:.1f} (age {age_today+p95_y:.1f})")
                    print(f"  → P(never reached in {RETIREMENT_MAX_YEARS}y): {pct_never:.1f}%")

                # Probability-by-age grid
                prob_grid = retirement_age_probability_grid(mc_ret, age_today)
                print(f"\n  P(reached target by age) — using paths_yearly cum-max:")
                prob_disp = prob_grid.copy()
                for col in prob_disp.columns:
                    prob_disp[col] = (prob_disp[col] * 100).apply(lambda v: f"{v:5.1f}%")
                prob_disp.columns = [f"${t/1e6:.1f}M" for t in prob_disp.columns]
                print(prob_disp.to_string())

                # Save raw MC distribution
                pd.DataFrame({"years_to_target": yrs}).to_csv(
                    "retirement_mc.csv", index=False)

                retirement = {
                    "starting": starting,
                    "monthly": cli_monthly,
                    "target": cli_target,
                    "inflation": cli_inflation,
                    "swr": cli_swr,
                    "after_tax_cagr": after_tax_cagr,
                    "real_cagr": real_cagr,
                    "age_today": age_today,
                    "deterministic": det,
                    "sensitivity": sens,
                    "mc": mc_ret,
                    "prob_grid": prob_grid,
                    "mc_basis": mc_basis,
                }

    # 10. Plots
    plots = {}
    if not skip_plots:
        print("\n── Generating plots ──")
        try:
            plot_subset = {n: results[n] for n in deployed_ran + candidate_ran if n in results}
            plot_subset["AGGREGATE (deployed)"] = aggregate_series
            for b in benchmarks:
                plot_subset[b] = results[b]
            plot_equity_curves(plot_subset, "Equity Curves — deployed + candidate vs benchmarks", "equity_curves.png")
            plot_drawdowns(plot_subset, "Drawdowns", "drawdowns.png")
            plot_rolling_sharpe(plot_subset, 252, "Rolling 12M Sharpe", "rolling_sharpe.png")
            plots = {
                "Equity curves": "equity_curves.png",
                "Drawdowns": "drawdowns.png",
                "Rolling 12-month Sharpe": "rolling_sharpe.png",
            }
            # Tax-drag bar chart (gross vs after-tax CAGR)
            if after_tax_metrics:
                _plot_tax_drag_comparison(
                    deployed_ran, metrics, after_tax_metrics,
                    aggregate_metrics, "tax_drag_comparison.png"
                )
            # Retirement projection (real $) — embedded into the HTML report
            if retirement is not None:
                plot_retirement_projection(
                    retirement["mc"], retirement["deterministic"],
                    retirement["age_today"], retirement["target"],
                    retirement["monthly"], "retirement_projection.png",
                    swr=retirement["swr"],
                    mc_basis=retirement.get("mc_basis", "after-tax"),
                )
                plots["Retirement projection (real $)"] = "retirement_projection.png"
        except Exception as e:
            print(f"  Plot generation partial-failed: {e}")

    # 10b. Promotion-decision financial-science analyses (only for candidates)
    promotion_analysis = {}
    if candidate_ran:
        print("\n── Promotion-decision analyses ──")
        # (a) Correlation matrix: candidates × deployed
        corr_names = deployed_ran + candidate_ran
        promotion_analysis["correlation"] = compute_correlation_matrix(results, corr_names)
        print(f"  ✓ correlation matrix ({len(corr_names)}×{len(corr_names)})")
        # (b) Portfolio what-if for each candidate
        deployed_returns = {n: results[n] for n in deployed_ran if n in results}
        deployed_alloc = {n: DEPLOYED_STRATEGIES[n].get("alloc", 0) for n in deployed_ran}
        what_ifs = {}
        for cn in candidate_ran:
            alloc = CANDIDATE_STRATEGIES[cn].get("proposed_alloc", 0.05)
            wi = portfolio_what_if(deployed_returns, deployed_alloc, results[cn], alloc)
            what_ifs[cn] = wi
            if wi:
                print(f"  ✓ what-if {cn}: ΔSharpe {wi['delta_sharpe']:+.3f}, "
                      f"ΔCAGR {wi['delta_cagr']*100:+.2f}pp, ΔMaxDD {wi['delta_maxdd']*100:+.2f}pp")
        promotion_analysis["what_ifs"] = what_ifs
        # (c) Regime splits for candidates + deployed aggregate
        regime_data = {}
        for cn in candidate_ran:
            regime_data[cn] = regime_split_metrics(results[cn])
        regime_data["AGGREGATE (deployed)"] = regime_split_metrics(aggregate_series)
        promotion_analysis["regime_data"] = regime_data
        print(f"  ✓ regime splits across {len(MACRO_REGIMES)} macro windows")
        # (d) Rolling 3-year Sharpe series for plotting
        if not skip_plots:
            try:
                roll_subset = {cn: rolling_sharpe(results[cn], window_years=3) for cn in candidate_ran}
                roll_subset["AGGREGATE (deployed)"] = rolling_sharpe(aggregate_series, window_years=3)
                plot_rolling_sharpe_custom(roll_subset, "Rolling 3-Year Sharpe — candidates vs deployed aggregate",
                                            "rolling_3y_sharpe.png")
                plots["Rolling 3Y Sharpe (candidates vs aggregate)"] = "rolling_3y_sharpe.png"
                print(f"  ✓ rolling 3Y Sharpe chart")
            except Exception as e:
                print(f"  ⚠ rolling Sharpe plot failed: {e}")

    # 11. Unified HTML report
    print("\n── Writing unified HTML report ──")
    write_unified_html_report(
        path="report.html",
        rets_window=(rets.index[0].date(), rets.index[-1].date()),
        deployed_names=deployed_ran,
        candidate_names=candidate_ran,
        historic_names=historic_ran,
        results=results,
        metrics=metrics,
        aggregate_series=aggregate_series,
        aggregate_metrics=aggregate_metrics,
        mc=mc,
        benchmarks=benchmarks,
        plots=plots,
        include_historic=include_historic,
        promotion_analysis=promotion_analysis,
        after_tax_metrics=after_tax_metrics,
        tax_logs=tax_logs,
        tax_assumptions=tax_assumptions,
        retirement=retirement,
    )
    print("  ✓ report.html")

    print(f"\nDone. Outputs in {os.getcwd()}")


if __name__ == "__main__":
    main()
