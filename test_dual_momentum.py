"""
Unit tests for the Dual Momentum Strategy (TestFolio A/B/C decision tree).

Tests the pure decision logic in determine_dual_momentum_target() which is shared
by monthly_dual_momentum_strategy (252-day lookback).

Decision tree under test:
  Signal A: SPY lookback return > 1%
  Signal B: EFA lookback return > 1%
  Signal C: SPY lookback return > (EFA lookback return + 1%)

  Allocation 1 (SPUU): A AND B AND C
  Allocation 2 (EFO):  Else if B
  Fallback (BND):      Else

Reference: Gary Antonacci, "Dual Momentum Investing" (2014)
Branching validated against the user's TestFolio setup.
"""

import sys
import os
import unittest

# Allow running from repo root without installing the package
sys.path.insert(0, os.path.dirname(__file__))

from main import determine_dual_momentum_target


class TestDetermineTargetRelativeMomentum(unittest.TestCase):
    """Tests that relative momentum correctly selects between SPUU and EFO."""

    def test_spy_higher_return_selects_spuu(self):
        """When SPY has a higher return than EFA, winner is SPUU (US equities)."""
        target, winner, winner_return, winner_underlying = determine_dual_momentum_target(
            spy_return=0.05, efa_return=0.03
        )
        self.assertEqual(target, "SPUU")
        self.assertEqual(winner, "SPUU")
        self.assertEqual(winner_underlying, "SPY")
        self.assertAlmostEqual(winner_return, 0.05)

    def test_efa_higher_return_selects_efo(self):
        """When EFA has a higher return than SPY, winner is EFO (international equities)."""
        target, winner, winner_return, winner_underlying = determine_dual_momentum_target(
            spy_return=0.03, efa_return=0.05
        )
        self.assertEqual(target, "EFO")
        self.assertEqual(winner, "EFO")
        self.assertEqual(winner_underlying, "EFA")
        self.assertAlmostEqual(winner_return, 0.05)

    def test_efa_wins_when_equal(self):
        """When returns are equal, SPUU gate fails so EFO branch is selected."""
        target, winner, winner_return, winner_underlying = determine_dual_momentum_target(
            spy_return=0.05, efa_return=0.05
        )
        # spy_return > efa_return is False when equal, so EFO wins
        self.assertEqual(winner, "EFO")
        self.assertEqual(winner_underlying, "EFA")


class TestDetermineTargetAbsoluteMomentum(unittest.TestCase):
    """Tests positive/negative handling in the TestFolio branch tree."""

    def test_positive_winner_return_stays_in_equities(self):
        """Winner with positive return -> invest in the equity winner."""
        target, _, winner_return, _ = determine_dual_momentum_target(
            spy_return=0.05, efa_return=0.03
        )
        self.assertEqual(target, "SPUU")
        self.assertGreater(winner_return, 0)

    def test_negative_winner_return_goes_to_bonds(self):
        """Winner with negative return -> go to BND regardless of which side won."""
        # SPY wins relative but is negative
        target, winner, _, _ = determine_dual_momentum_target(
            spy_return=-0.02, efa_return=-0.05
        )
        self.assertEqual(winner, "SPUU")      # SPY won relative comparison
        self.assertEqual(target, "BND")        # but negative -> bonds

    def test_zero_winner_return_goes_to_bonds(self):
        """Exactly zero return is not positive -> go to bonds."""
        target, _, _, _ = determine_dual_momentum_target(
            spy_return=0.0, efa_return=-0.01
        )
        self.assertEqual(target, "BND")


class TestDetermineTargetCombinedScenarios(unittest.TestCase):
    """
    Full integration tests matching the TestFolio setup.

    Verified scenarios from the TestFolio setup:
      Signal A: SPY 252-day return > 1%  (absolute momentum)
      Signal B: EFA 252-day return > 1%  (absolute momentum)
      Signal C: SPY 252-day return > EFA 252-day return + 1%  (relative momentum)
      Allocation 1 (SPUU): A AND B AND C
      Allocation 2 (EFO):  Else if B
      Fallback (BND):      Else
    """

    def test_spy_positive_spy_wins_relative(self):
        """SPY +5%, EFA +3% -> SPUU: US market wins relative and absolute."""
        target, winner, _, _ = determine_dual_momentum_target(0.05, 0.03)
        self.assertEqual(target, "SPUU")
        self.assertEqual(winner, "SPUU")

    def test_spy_positive_efa_wins_relative(self):
        """SPY +3%, EFA +5% -> EFO: international wins relative and is positive."""
        target, winner, _, _ = determine_dual_momentum_target(0.03, 0.05)
        self.assertEqual(target, "EFO")
        self.assertEqual(winner, "EFO")

    def test_spy_negative_efa_positive_efa_wins(self):
        """
        SPY -2%, EFA +5% -> EFO.
        Allocation 1 fails (A is false), but B is true so Allocation 2 is selected.
        """
        target, winner, winner_return, _ = determine_dual_momentum_target(-0.02, 0.05)
        self.assertEqual(winner, "EFO")
        self.assertGreater(winner_return, 0)
        self.assertEqual(target, "EFO")

    def test_spy_positive_efa_negative_spy_wins(self):
        """SPY +2%, EFA -3% -> BND: B is false, so fallback applies."""
        target, winner, _, _ = determine_dual_momentum_target(0.02, -0.03)
        self.assertEqual(winner, "SPUU")
        self.assertEqual(target, "BND")

    def test_spy_negative_efa_negative_spy_wins_relative(self):
        """SPY -2%, EFA -5% -> BND: SPY wins relative but is negative -> bonds."""
        target, winner, winner_return, _ = determine_dual_momentum_target(-0.02, -0.05)
        self.assertEqual(winner, "SPUU")       # SPY won relative
        self.assertLess(winner_return, 0)
        self.assertEqual(target, "BND")

    def test_spy_negative_efa_negative_efa_wins_relative(self):
        """SPY -5%, EFA -2% -> BND: EFA wins relative but is negative -> bonds."""
        target, winner, winner_return, _ = determine_dual_momentum_target(-0.05, -0.02)
        self.assertEqual(winner, "EFO")        # EFA won relative
        self.assertLess(winner_return, 0)
        self.assertEqual(target, "BND")

    def test_large_positive_spy(self):
        """Strong bull market: both positive, SPY leads -> SPUU."""
        target, _, _, _ = determine_dual_momentum_target(0.30, 0.15)
        self.assertEqual(target, "SPUU")

    def test_large_positive_efa(self):
        """International outperformance: EFA leads, both positive -> EFO."""
        target, _, _, _ = determine_dual_momentum_target(0.10, 0.25)
        self.assertEqual(target, "EFO")

    def test_below_absolute_threshold_falls_back_to_bnd(self):
        """EFA at +0.9% fails B gate -> fallback BND."""
        target, _, _, _ = determine_dual_momentum_target(0.03, 0.009)
        self.assertEqual(target, "BND")

    def test_relative_threshold_requires_full_one_percent_spread(self):
        """SPY needs >1% lead over EFA to select SPUU; exact 1% spread keeps EFO."""
        target, _, _, _ = determine_dual_momentum_target(0.05, 0.04)
        self.assertEqual(target, "EFO")

    def test_relative_threshold_crossing_selects_spuu(self):
        """SPY lead above 1% spread passes C gate -> SPUU."""
        target, _, _, _ = determine_dual_momentum_target(0.051, 0.04)
        self.assertEqual(target, "SPUU")


class TestDetermineTargetReturnValues(unittest.TestCase):
    """Tests that returned metadata is accurate for downstream use."""

    def test_winner_return_matches_spy_when_spy_wins(self):
        spy_return, efa_return = 0.12, 0.07
        _, winner, winner_return, winner_underlying = determine_dual_momentum_target(spy_return, efa_return)
        self.assertEqual(winner, "SPUU")
        self.assertAlmostEqual(winner_return, spy_return)
        self.assertEqual(winner_underlying, "SPY")

    def test_winner_return_matches_efa_when_efa_wins(self):
        spy_return, efa_return = 0.07, 0.12
        _, winner, winner_return, winner_underlying = determine_dual_momentum_target(spy_return, efa_return)
        self.assertEqual(winner, "EFO")
        self.assertAlmostEqual(winner_return, efa_return)
        self.assertEqual(winner_underlying, "EFA")

    def test_returns_tuple_of_four(self):
        result = determine_dual_momentum_target(0.05, 0.03)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 4)

    def test_target_is_valid_symbol(self):
        valid_symbols = {"SPUU", "EFO", "BND"}
        for spy_r, efa_r in [(0.05, 0.03), (0.03, 0.05), (-0.02, -0.05)]:
            target, _, _, _ = determine_dual_momentum_target(spy_r, efa_r)
            self.assertIn(target, valid_symbols)


if __name__ == "__main__":
    unittest.main(verbosity=2)
