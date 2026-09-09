import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main


def test_active_allocations_exclude_f4_and_sum_to_one():
    assert main.strategy_allocations == {
        "hfea_allo": 0.1829,
        "spxl_allo": 0.1829,
        "nine_sig_allo": 0.0610,
        "dual_momentum_allo": 0.2439,
        "regime_sso_allo": 0.1464,
        "aaa_allo": 0.1829,
    }
    assert sum(main.strategy_allocations.values()) == pytest.approx(1.0)


def test_active_ticker_ownership_excludes_f4():
    assert "f4" not in main.STRATEGY_SYMBOLS


def test_strategy_values_ignore_retired_f4_positions(monkeypatch):
    monkeypatch.setattr(
        main,
        "list_positions",
        lambda api: [
            {"symbol": "UPRO", "market_value": "10"},
            {"symbol": "WLDU", "market_value": "100"},
        ],
    )
    monkeypatch.setattr(
        main,
        "regime_state",
        lambda cfg, env: {"risk_shares": 0, "safe_shares": 0},
    )

    values = main.get_all_strategy_values({})

    assert "f4" not in values
    assert values["total"] == pytest.approx(10.0)


def test_contribution_rebalancing_returns_only_active_allocations(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_all_strategy_values",
        lambda api: {
            "hfea": 100.0,
            "spxl_sma": 100.0,
            "nine_sig": 100.0,
            "dual_momentum": 100.0,
            "regime_sso": 100.0,
            "aaa": 100.0,
            "total": 600.0,
        },
    )

    result = main.calculate_rebalanced_allocations({})

    assert set(result["adjusted_allocations"]) == set(main.strategy_allocations)
    assert "f4_allo" not in result["adjusted_allocations"]
