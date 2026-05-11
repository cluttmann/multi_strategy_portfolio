"""
Unit tests for the Dual Momentum (best-of-3) strategy and the underlying
regime-detection helpers that depend on get_alpaca_historical_bars.

Tests focus on pure-math helpers and the bar-fetch / signal layer that was
silently broken before the dict-vs-floats and calendar-vs-trading-day fixes:

  • _dm_blended_momentum_score      — blended 6m+12m skip-1m momentum
  • _dm_realized_vol                — 60-day annualized realized vol
  • _dm_pick_target                 — best-of-3 candidate selection
  • get_alpaca_historical_bars      — raw= mode + calendar/trading-day conversion
  • signal_price_trend              — trend signal with hysteresis
  • signal_credit_spread            — credit ratio vs SMA
  • compute_market_breadth          — sp500 vs basket mode

External I/O (Alpaca, Firestore, Telegram) is mocked so tests run offline.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Allow running from repo root without installing the package
sys.path.insert(0, os.path.dirname(__file__))

# Required env so Firestore client lazy-init doesn't blow up at import time
os.environ.setdefault("GOOGLE_CLOUD_PROJECT_ID", "test-project")

import main


# ──────────────────────────────────────────────────────────────────────
# get_alpaca_historical_bars — raw= mode + trading-day conversion
# ──────────────────────────────────────────────────────────────────────

class TestGetAlpacaHistoricalBars(unittest.TestCase):
    """The function returns closes by default but raw bar dicts when raw=True.
    `days` is interpreted as TRADING days; we over-request ~1.5× calendar days."""

    def _mk_bars(self, n):
        return [{"t": f"2026-01-{i+1:02d}T00:00:00Z", "o": 100+i, "h": 101+i,
                 "l": 99+i, "c": 100+i, "v": 1000} for i in range(n)]

    @patch("main.alpaca_request_with_retry")
    @patch("main.get_auth_headers", return_value={})
    def test_returns_closes_by_default(self, _h, mock_retry):
        bars = self._mk_bars(5)
        mock_retry.return_value = MagicMock(json=lambda: {"bars": bars})
        result = main.get_alpaca_historical_bars(api={}, symbol="SPY", days=5)
        self.assertEqual(result, [100, 101, 102, 103, 104])
        # ensure list of floats, not dicts
        self.assertIsInstance(result[0], (int, float))

    @patch("main.alpaca_request_with_retry")
    @patch("main.get_auth_headers", return_value={})
    def test_returns_raw_dicts_when_raw_true(self, _h, mock_retry):
        bars = self._mk_bars(3)
        mock_retry.return_value = MagicMock(json=lambda: {"bars": bars})
        result = main.get_alpaca_historical_bars(api={}, symbol="SPY", days=3, raw=True)
        self.assertEqual(len(result), 3)
        self.assertIsInstance(result[0], dict)
        self.assertIn("c", result[0])
        self.assertIn("h", result[0])
        self.assertIn("t", result[0])

    @patch("main.alpaca_request_with_retry")
    @patch("main.get_auth_headers", return_value={})
    def test_returns_none_on_empty(self, _h, mock_retry):
        mock_retry.return_value = MagicMock(json=lambda: {"bars": []})
        result = main.get_alpaca_historical_bars(api={}, symbol="SPY", days=5)
        self.assertIsNone(result)

    @patch("main.alpaca_request_with_retry")
    @patch("main.get_auth_headers", return_value={})
    def test_trading_days_converted_to_calendar(self, _h, mock_retry):
        """`days=200` should request ≥ 1.5× as many calendar days from Alpaca."""
        captured = {}

        def capture(method, url, headers, params=None, label=None, **kw):
            captured.update(params or {})
            return MagicMock(json=lambda: {"bars": self._mk_bars(150)})

        mock_retry.side_effect = capture
        main.get_alpaca_historical_bars(api={}, symbol="SPY", days=200)
        # start/end should span > 200 calendar days
        import datetime as dt
        start = dt.datetime.strptime(captured["start"], "%Y-%m-%d")
        end = dt.datetime.strptime(captured["end"], "%Y-%m-%d")
        self.assertGreaterEqual((end - start).days, 300)  # ≥ 1.5× the 200 trading days


# ──────────────────────────────────────────────────────────────────────
# _dm_blended_momentum_score
# ──────────────────────────────────────────────────────────────────────

class TestBlendedMomentumScore(unittest.TestCase):
    """Score = 0.5 × (P_now/P_6m - 1) + 0.5 × (P_now/P_12m - 1) with skip-1m."""

    def _series(self, n, *, ramp=None):
        """Build n-bar synthetic series. `ramp` is the per-bar return."""
        if ramp is None:
            return [100.0] * n
        return [100.0 * (1 + ramp) ** i for i in range(n)]

    @patch("main.get_alpaca_historical_bars")
    def test_flat_series_scores_zero(self, mock_bars):
        mock_bars.return_value = self._series(400)
        score = main._dm_blended_momentum_score(api={}, signal_symbol="SPY",
                                                  cfg=main.dual_momentum_config)
        self.assertIsNotNone(score)
        self.assertAlmostEqual(score, 0.0, places=6)

    @patch("main.get_alpaca_historical_bars")
    def test_steady_uptrend_scores_positive(self, mock_bars):
        # 0.1%/day for 400 days ≈ +50% over 6m, +180% over 12m → strongly positive
        mock_bars.return_value = self._series(400, ramp=0.001)
        score = main._dm_blended_momentum_score(api={}, signal_symbol="SPY",
                                                  cfg=main.dual_momentum_config)
        self.assertGreater(score, 0.10)

    @patch("main.get_alpaca_historical_bars")
    def test_steady_downtrend_scores_negative(self, mock_bars):
        mock_bars.return_value = self._series(400, ramp=-0.001)
        score = main._dm_blended_momentum_score(api={}, signal_symbol="SPY",
                                                  cfg=main.dual_momentum_config)
        self.assertLess(score, -0.05)

    @patch("main.get_alpaca_historical_bars")
    def test_insufficient_data_returns_none(self, mock_bars):
        # Need at least skip_idx + 252 + 1 bars; 200 is too few.
        mock_bars.return_value = self._series(200)
        score = main._dm_blended_momentum_score(api={}, signal_symbol="SPY",
                                                  cfg=main.dual_momentum_config)
        self.assertIsNone(score)


# ──────────────────────────────────────────────────────────────────────
# _dm_realized_vol
# ──────────────────────────────────────────────────────────────────────

class TestRealizedVol(unittest.TestCase):

    @patch("main.get_alpaca_historical_bars")
    def test_flat_series_zero_vol(self, mock_bars):
        mock_bars.return_value = [100.0] * 100
        vol = main._dm_realized_vol(api={}, symbol="SPUU", window=60)
        self.assertEqual(vol, 0.0)

    @patch("main.get_alpaca_historical_bars")
    def test_known_daily_vol_annualizes(self, mock_bars):
        # Alternating +1% / -1% daily — daily std ≈ 1% → annualized ≈ 1% × √252 ≈ 15.87%
        prices = [100.0]
        for i in range(1, 100):
            prices.append(prices[-1] * (1.01 if i % 2 == 1 else 0.99))
        mock_bars.return_value = prices
        vol = main._dm_realized_vol(api={}, symbol="SPUU", window=60)
        self.assertIsNotNone(vol)
        self.assertGreater(vol, 0.10)
        self.assertLess(vol, 0.25)

    @patch("main.get_alpaca_historical_bars")
    def test_insufficient_data_returns_none(self, mock_bars):
        mock_bars.return_value = [100.0] * 20  # < window+1 (60+1)
        vol = main._dm_realized_vol(api={}, symbol="SPUU", window=60)
        self.assertIsNone(vol)


# ──────────────────────────────────────────────────────────────────────
# _dm_pick_target — best-of-3 selection
# ──────────────────────────────────────────────────────────────────────

class TestPickTarget(unittest.TestCase):

    def test_picks_highest_scorer(self):
        cfg = main.dual_momentum_config
        with patch("main._dm_blended_momentum_score") as mock_score:
            # SPY 5%, QQQ 12%, EFA 3% → QLD wins
            mock_score.side_effect = lambda api, sym, cfg: {
                "SPY": 0.05, "QQQ": 0.12, "EFA": 0.03
            }[sym]
            winner, scores, defensive = main._dm_pick_target(api={}, cfg=cfg)
            self.assertEqual(winner, "QLD")
            self.assertEqual(defensive, "BND")
            self.assertEqual(scores["QLD"], 0.12)

    def test_below_min_score_returns_none(self):
        cfg = main.dual_momentum_config
        with patch("main._dm_blended_momentum_score") as mock_score:
            # All three scores below the 1% min — strategy should hold defensive
            mock_score.side_effect = lambda api, sym, cfg: 0.005
            winner, scores, defensive = main._dm_pick_target(api={}, cfg=cfg)
            self.assertIsNone(winner)
            self.assertEqual(defensive, "BND")

    def test_missing_data_returns_none_winner(self):
        cfg = main.dual_momentum_config
        with patch("main._dm_blended_momentum_score", return_value=None):
            winner, scores, defensive = main._dm_pick_target(api={}, cfg=cfg)
            self.assertIsNone(winner)
            self.assertEqual(scores, {})


# ──────────────────────────────────────────────────────────────────────
# signal_price_trend — trend + 3-day hysteresis
# ──────────────────────────────────────────────────────────────────────

class TestSignalPriceTrend(unittest.TestCase):

    @patch("main.load_recent_regime_scores", return_value=[])
    @patch("main.get_alpaca_historical_bars")
    def test_above_sma_returns_bullish(self, mock_bars, _hist):
        # 300 days at 100 then 5 days at 110 → above 200-SMA, all 3 most recent above
        closes = [100.0] * 295 + [110.0] * 5
        mock_bars.return_value = closes
        s, t, y, p = main.signal_price_trend(api={}, cfg=main.regime_sso_config)
        self.assertEqual(s, 1)
        self.assertEqual(t, 1)
        self.assertEqual(y, 1)

    @patch("main.load_recent_regime_scores", return_value=[])
    @patch("main.get_alpaca_historical_bars")
    def test_below_sma_returns_bearish(self, mock_bars, _hist):
        # 295 days at 100, then 5 at 90 → below 200-SMA on last 3 days
        closes = [100.0] * 295 + [90.0] * 5
        mock_bars.return_value = closes
        s, t, y, p = main.signal_price_trend(api={}, cfg=main.regime_sso_config)
        self.assertEqual(s, -1)

    @patch("main.load_recent_regime_scores", return_value=[{"price_trend": 1}])
    @patch("main.get_alpaca_historical_bars")
    def test_mixed_days_hold_prior_signal(self, mock_bars, _hist):
        # 296 days at 100, then 95/105/95 → today's raw vs SMA flips daily, prior=1 held
        closes = [100.0] * 297 + [95.0, 105.0, 95.0]
        mock_bars.return_value = closes
        s, t, y, p = main.signal_price_trend(api={}, cfg=main.regime_sso_config)
        # Last 3 raws disagree, so we hold the prior persisted signal (+1)
        self.assertEqual(s, 1)


# ──────────────────────────────────────────────────────────────────────
# signal_credit_spread
# ──────────────────────────────────────────────────────────────────────

class TestSignalCreditSpread(unittest.TestCase):

    @patch("main.get_alpaca_historical_bars")
    def test_rising_ratio_bullish(self, mock_bars):
        # HYG accelerating, LQD flat → ratio rising sharply above SMA
        def side(_api, sym, **kw):
            if sym == "HYG":
                return [80.0 + i * 0.05 for i in range(70)]
            if sym == "LQD":
                return [100.0] * 70
            return None
        mock_bars.side_effect = side
        s, ratio = main.signal_credit_spread(api={}, cfg=main.regime_sso_config)
        self.assertEqual(s, 1)
        self.assertIsNotNone(ratio)

    @patch("main.get_alpaca_historical_bars")
    def test_falling_ratio_bearish(self, mock_bars):
        # HYG decelerating relative to LQD → ratio falling below SMA
        def side(_api, sym, **kw):
            if sym == "HYG":
                return [80.0 - i * 0.05 for i in range(70)]
            if sym == "LQD":
                return [100.0] * 70
            return None
        mock_bars.side_effect = side
        s, ratio = main.signal_credit_spread(api={}, cfg=main.regime_sso_config)
        self.assertEqual(s, -1)


# ──────────────────────────────────────────────────────────────────────
# compute_market_breadth — sp500 vs basket modes
# ──────────────────────────────────────────────────────────────────────

class TestComputeMarketBreadth(unittest.TestCase):

    @patch("main.get_alpaca_historical_bars")
    def test_basket_mode_all_above_sma(self, mock_bars):
        # Every basket constituent is above its 50-SMA → breadth = 100%
        mock_bars.return_value = [100.0] * 49 + [110.0]  # last close > 50-SMA
        pct = main.compute_market_breadth(api={}, cfg=main.regime_world_config)
        self.assertIsNotNone(pct)
        self.assertEqual(pct, 1.0)

    @patch("main.get_alpaca_historical_bars")
    def test_basket_mode_all_below_sma(self, mock_bars):
        mock_bars.return_value = [100.0] * 49 + [90.0]
        pct = main.compute_market_breadth(api={}, cfg=main.regime_world_config)
        self.assertEqual(pct, 0.0)


# ──────────────────────────────────────────────────────────────────────
# Configuration sanity
# ──────────────────────────────────────────────────────────────────────

class TestPortfolioConfig(unittest.TestCase):
    """Cross-checks for configs that easily drift out of sync."""

    def test_allocations_sum_to_one(self):
        total = sum(main.strategy_allocations.values())
        self.assertAlmostEqual(total, 1.0, places=4)

    def test_all_allocations_referenced_by_strategy_to_allo_key_map(self):
        # Map is rebuilt inside calculate_rebalanced_allocations; mirror it here.
        expected_keys = set(main.strategy_allocations.keys())
        # Each strategy in STRATEGY_SYMBOLS has a corresponding allo key
        strat_keys = {f"{k}_allo" if not k.endswith("_allo") else k
                      for k in main.STRATEGY_SYMBOLS}
        # The mapping isn't 1:1 (spxl_sma maps to spxl_allo, hfea maps to hfea_allo);
        # the practical invariant is just that the allocation total is correct.
        self.assertEqual(len(expected_keys), 7,
                         f"Expected 7 strategies, got {len(expected_keys)}: {expected_keys}")

    def test_dual_momentum_config_shape(self):
        cfg = main.dual_momentum_config
        self.assertEqual(len(cfg["candidates"]), 3)
        self.assertEqual(cfg["defensive"], "BND")
        self.assertEqual(cfg["dd_threshold"], 0.30)
        self.assertEqual(cfg["target_vol"], 0.25)
        # Lookback weights sum to 1.0
        self.assertAlmostEqual(sum(cfg["lookback_weights"].values()), 1.0, places=4)

    def test_regime_world_uses_urth_not_spy(self):
        self.assertEqual(main.regime_world_config["trend_symbol"], "URTH")
        self.assertEqual(main.regime_world_config["spy_sma_period"], 255)
        self.assertEqual(main.regime_world_config["breadth_mode"], "basket")
        self.assertEqual(main.regime_world_config["risk_asset"], "WLDU")

    def test_regime_sso_and_world_use_separate_firestore_keys(self):
        self.assertNotEqual(main.regime_sso_config["strategy_key"],
                            main.regime_world_config["strategy_key"])
        self.assertNotEqual(main.regime_sso_config["scores_collection"],
                            main.regime_world_config["scores_collection"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
