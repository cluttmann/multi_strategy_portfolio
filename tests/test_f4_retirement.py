import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main

from scripts import retire_f4_live


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


def test_extract_f4_positions_uses_only_available_quantity():
    positions = [
        {
            "symbol": "WLDU",
            "qty": "20",
            "qty_available": "20",
            "current_price": "17.74",
        },
        {
            "symbol": "GOLY",
            "qty": "10",
            "qty_available": "0",
            "current_price": "26.69",
        },
        {
            "symbol": "TLT",
            "qty": "3.233387",
            "current_price": "82.12",
        },
        {
            "symbol": "UPRO",
            "qty": "12",
            "qty_available": "12",
            "current_price": "105.00",
        },
    ]

    sale_plan = retire_f4_live.extract_f4_positions(positions)

    assert [item["symbol"] for item in sale_plan] == ["WLDU", "TLT"]
    assert sale_plan[0] == {
        "symbol": "WLDU",
        "qty": 20.0,
        "estimated_price": 17.74,
        "estimated_proceeds": pytest.approx(354.8),
    }
    assert sale_plan[1]["qty"] == pytest.approx(3.233387)


def test_project_post_sale_margin_first_reduces_negative_cash():
    margin_result = {
        "allowed": True,
        "target_margin": 0.10,
        "gate_results": {"market_trend": True},
        "metrics": {"cash": -1324.74, "equity": 12964.91},
        "errors": [],
    }

    projected = retire_f4_live.project_post_sale_margin(margin_result, 887.23)

    assert projected["metrics"]["cash"] == pytest.approx(-437.51)
    assert margin_result["metrics"]["cash"] == pytest.approx(-1324.74)


def test_cap_investment_calculation_preserves_strategy_ratios():
    calculation = {
        "total_cash": 100.0,
        "total_available": 100.0,
        "margin_approved": 900.0,
        "total_investing": 1000.0,
        "strategy_amounts": {
            "hfea_allo": 250.0,
            "spxl_allo": 250.0,
            "nine_sig_allo": 100.0,
            "dual_momentum_allo": 200.0,
            "regime_sso_allo": 100.0,
            "aaa_allo": 100.0,
        },
        "reserved_amounts": {},
        "rebalance_result": {"adjusted_allocations": {}},
    }

    capped = retire_f4_live.cap_investment_calculation(calculation, 400.0)

    assert capped["total_investing"] == pytest.approx(400.0)
    assert capped["total_available"] == pytest.approx(100.0)
    assert capped["margin_approved"] == pytest.approx(300.0)
    assert capped["strategy_amounts"] == pytest.approx(
        {
            "hfea_allo": 100.0,
            "spxl_allo": 100.0,
            "nine_sig_allo": 40.0,
            "dual_momentum_allo": 80.0,
            "regime_sso_allo": 40.0,
            "aaa_allo": 40.0,
        }
    )


def test_cap_investment_calculation_never_exceeds_available_capacity():
    calculation = {
        "total_cash": -400.0,
        "total_available": 0.0,
        "margin_approved": 250.0,
        "total_investing": 250.0,
        "strategy_amounts": {"hfea_allo": 250.0},
    }

    capped = retire_f4_live.cap_investment_calculation(calculation, 900.0)

    assert capped["total_investing"] == pytest.approx(250.0)
    assert capped["strategy_amounts"]["hfea_allo"] == pytest.approx(250.0)


def _snapshot(positions=None, pending_orders=None, cash=-400.0):
    return {
        "account": {
            "cash": cash,
            "equity": 10000.0,
            "portfolio_value": 10000.0,
            "maintenance_margin": 3000.0,
        },
        "positions": positions or [],
        "pending_orders": pending_orders or [],
    }


def _f4_position(symbol="WLDU", qty="2", price="20"):
    return {
        "symbol": symbol,
        "qty": qty,
        "qty_available": qty,
        "current_price": price,
    }


def test_execute_retirement_rejects_wrong_confirmation():
    with pytest.raises(ValueError, match="RETIRE_F4_LIVE"):
        retire_f4_live.execute_retirement({}, confirmation="yes")


def test_execute_retirement_rejects_closed_market(monkeypatch):
    monkeypatch.setattr(retire_f4_live, "get_market_clock", lambda api: {"is_open": False})

    with pytest.raises(RuntimeError, match="market is closed"):
        retire_f4_live.execute_retirement(
            {}, confirmation=retire_f4_live.CONFIRMATION_TOKEN
        )


def test_execute_retirement_rejects_existing_open_orders(monkeypatch):
    monkeypatch.setattr(retire_f4_live, "get_market_clock", lambda api: {"is_open": True})
    monkeypatch.setattr(
        retire_f4_live,
        "capture_snapshot",
        lambda api: _snapshot(
            positions=[_f4_position()],
            pending_orders=[{"id": "open-1", "symbol": "UPRO"}],
        ),
    )

    with pytest.raises(RuntimeError, match="open order"):
        retire_f4_live.execute_retirement(
            {}, confirmation=retire_f4_live.CONFIRMATION_TOKEN
        )


def test_execute_retirement_is_idempotent_when_f4_is_already_gone(monkeypatch):
    monkeypatch.setattr(retire_f4_live, "get_market_clock", lambda api: {"is_open": True})
    monkeypatch.setattr(
        retire_f4_live,
        "capture_snapshot",
        lambda api: _snapshot(positions=[_f4_position("UPRO")]),
    )

    result = retire_f4_live.execute_retirement(
        {}, confirmation=retire_f4_live.CONFIRMATION_TOKEN
    )

    assert result["status"] == "already_retired"


def test_execute_retirement_stops_when_sell_submission_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(retire_f4_live, "get_market_clock", lambda api: {"is_open": True})
    monkeypatch.setattr(
        retire_f4_live,
        "capture_snapshot",
        lambda api: _snapshot(positions=[_f4_position()]),
    )
    monkeypatch.setattr(
        retire_f4_live.bot,
        "submit_order",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sell rejected")),
    )
    monkeypatch.setattr(
        retire_f4_live,
        "execute_strategy_buys",
        lambda *args, **kwargs: pytest.fail("buys must not run"),
    )

    with pytest.raises(RuntimeError, match="sell rejected"):
        retire_f4_live.execute_retirement(
            {},
            confirmation=retire_f4_live.CONFIRMATION_TOKEN,
            audit_path=tmp_path / "audit.json",
        )


def test_execute_retirement_stops_when_f4_residual_remains(monkeypatch, tmp_path):
    snapshots = iter(
        [
            _snapshot(positions=[_f4_position()]),
            _snapshot(positions=[_f4_position(qty="0.5")]),
        ]
    )
    monkeypatch.setattr(retire_f4_live, "get_market_clock", lambda api: {"is_open": True})
    monkeypatch.setattr(retire_f4_live, "capture_snapshot", lambda api: next(snapshots))
    monkeypatch.setattr(
        retire_f4_live.bot,
        "submit_order",
        lambda *args, **kwargs: {"id": "sell-1"},
    )
    monkeypatch.setattr(
        retire_f4_live,
        "strict_wait_for_fill",
        lambda *args, **kwargs: {
            "id": "sell-1",
            "status": "filled",
            "filled_qty": "1.5",
            "filled_avg_price": "20",
        },
    )

    with pytest.raises(RuntimeError, match="F4 position remains"):
        retire_f4_live.execute_retirement(
            {},
            confirmation=retire_f4_live.CONFIRMATION_TOKEN,
            audit_path=tmp_path / "audit.json",
        )


def test_execute_retirement_leaves_proceeds_as_deleveraging_when_budget_is_zero(
    monkeypatch, tmp_path
):
    snapshots = iter(
        [
            _snapshot(positions=[_f4_position()]),
            _snapshot(),
            _snapshot(),
        ]
    )
    monkeypatch.setattr(retire_f4_live, "get_market_clock", lambda api: {"is_open": True})
    monkeypatch.setattr(retire_f4_live, "capture_snapshot", lambda api: next(snapshots))
    monkeypatch.setattr(
        retire_f4_live.bot,
        "submit_order",
        lambda *args, **kwargs: {"id": "sell-1"},
    )
    monkeypatch.setattr(
        retire_f4_live,
        "strict_wait_for_fill",
        lambda *args, **kwargs: {
            "id": "sell-1",
            "status": "filled",
            "filled_qty": "2",
            "filled_avg_price": "20",
        },
    )
    monkeypatch.setattr(retire_f4_live, "mark_f4_retired", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        retire_f4_live.bot,
        "check_margin_conditions",
        lambda api, env: {
            "allowed": False,
            "target_margin": 0.0,
            "gate_results": {},
            "metrics": {"cash": -360.0, "equity": 10000.0},
            "errors": [],
        },
    )
    monkeypatch.setattr(
        retire_f4_live.bot,
        "calculate_monthly_investments",
        lambda api, margin_result, env: {
            "total_available": 0.0,
            "margin_approved": 0.0,
            "total_investing": 0.0,
            "strategy_amounts": {key: 0.0 for key in main.strategy_allocations},
        },
    )
    monkeypatch.setattr(
        retire_f4_live,
        "execute_strategy_buys",
        lambda *args, **kwargs: pytest.fail("buys must not run"),
    )
    monkeypatch.setattr(
        retire_f4_live.bot,
        "recalculate_all_strategies_cost_basis",
        lambda *args, **kwargs: {"success": True},
    )
    monkeypatch.setattr(retire_f4_live.bot, "send_telegram_message", lambda message: None)

    result = retire_f4_live.execute_retirement(
        {},
        confirmation=retire_f4_live.CONFIRMATION_TOKEN,
        audit_path=tmp_path / "audit.json",
    )

    assert result["status"] == "sold_without_reallocation"
    assert result["actual_proceeds"] == pytest.approx(40.0)


def test_execute_retirement_caps_buys_and_runs_all_six_strategies(monkeypatch, tmp_path):
    snapshots = iter(
        [
            _snapshot(positions=[_f4_position(qty="10", price="30")]),
            _snapshot(),
            _snapshot(),
        ]
    )
    monkeypatch.setattr(retire_f4_live, "get_market_clock", lambda api: {"is_open": True})
    monkeypatch.setattr(retire_f4_live, "capture_snapshot", lambda api: next(snapshots))
    monkeypatch.setattr(
        retire_f4_live.bot,
        "submit_order",
        lambda *args, **kwargs: {"id": "sell-1"},
    )
    monkeypatch.setattr(
        retire_f4_live,
        "strict_wait_for_fill",
        lambda *args, **kwargs: {
            "id": "sell-1",
            "status": "filled",
            "filled_qty": "10",
            "filled_avg_price": "30",
        },
    )
    monkeypatch.setattr(retire_f4_live, "mark_f4_retired", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        retire_f4_live.bot,
        "check_margin_conditions",
        lambda api, env: {
            "allowed": True,
            "target_margin": 0.10,
            "gate_results": {},
            "metrics": {"cash": -100.0, "equity": 10000.0},
            "errors": [],
        },
    )
    monkeypatch.setattr(
        retire_f4_live.bot,
        "calculate_monthly_investments",
        lambda api, margin_result, env: {
            "total_available": 0.0,
            "margin_approved": 1000.0,
            "total_investing": 1000.0,
            "strategy_amounts": {
                key: 1000.0 / 6 for key in main.strategy_allocations
            },
        },
    )
    captured = {}

    def fake_buys(api, investment_calc, margin_result, env, audit, audit_path):
        captured["calculation"] = investment_calc
        return ["hfea", "spxl", "nine_sig", "dual_momentum", "regime_sso", "aaa"]

    monkeypatch.setattr(retire_f4_live, "execute_strategy_buys", fake_buys)
    monkeypatch.setattr(
        retire_f4_live.bot,
        "recalculate_all_strategies_cost_basis",
        lambda *args, **kwargs: {"success": True},
    )
    monkeypatch.setattr(retire_f4_live.bot, "send_telegram_message", lambda message: None)

    result = retire_f4_live.execute_retirement(
        {},
        confirmation=retire_f4_live.CONFIRMATION_TOKEN,
        audit_path=tmp_path / "audit.json",
    )

    assert captured["calculation"]["total_investing"] == pytest.approx(300.0)
    assert result["strategy_results"] == [
        "hfea",
        "spxl",
        "nine_sig",
        "dual_momentum",
        "regime_sso",
        "aaa",
    ]
