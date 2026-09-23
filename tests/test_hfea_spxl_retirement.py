"""Sichert das Retirement von HFEA und SPXL SMA vom 22.09.2026 ab.

Wie beim Regime-SSO/9-Sig-Test darunter: nicht die Entfernung wiederholen,
sondern verhindern, dass sie still rueckgaengig gemacht wird. Der teuerste
Rueckfall waere hier ein Scheduler, der wieder feuert — daily_trade_spxl_200sma
tauscht bei bullischem Signal ein vorhandenes SGOV-Polster automatisch zurueck
in SPXL, und monthly_invest_all kauft mit jedem Gewicht, das in
strategy_allocations steht.
"""
import inspect
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main

ROOT = Path(__file__).resolve().parent.parent
RETIRED_FUNCTIONS = [
    "monthly_buy_hfea",
    "rebalance_hfea",
    "monthly_buy_spxl",
    "daily_trade_spxl_200sma",
]
FREED_TICKERS = {"UPRO", "TMF"}
# Am 2026-09-23 bewusst wieder vergeben: KMLM und SGOV an Mix8, SPXL an den
# S&P-Trend (der SPXL SMA unter einem anderen Ziel).
REUSED_BY_MIX8 = {"KMLM", "SGOV"}


def test_retired_sleeves_carry_no_weight():
    assert "hfea_allo" not in main.strategy_allocations
    assert "spxl_allo" not in main.strategy_allocations
    assert sum(main.strategy_allocations.values()) == pytest.approx(1.0)


def test_retired_sleeves_own_no_tickers():
    assert "hfea" not in main.STRATEGY_SYMBOLS
    assert "spxl_sma" not in main.STRATEGY_SYMBOLS
    owned = {t for syms in main.STRATEGY_SYMBOLS.values() for t in syms}
    assert owned.isdisjoint(FREED_TICKERS)
    assert REUSED_BY_MIX8 <= set(main.STRATEGY_SYMBOLS["mix8"])
    assert "SPXL" in main.STRATEGY_SYMBOLS["spx_trend"]


def test_no_http_route_for_retired_functions():
    routes = {rule.rule for rule in main.app.url_map.iter_rules()}
    for name in RETIRED_FUNCTIONS:
        assert not hasattr(main, name), f"{name} ist wieder da"
        assert f"/{name}" not in routes


def test_orchestrator_and_reporting_ignore_retired_sleeves():
    for fn in (main.monthly_invest_all_strategies,
               main.get_all_strategy_values,
               main.calculate_rebalanced_allocations,
               main.print_allocation_dashboard,
               main.audit_monthly_run):
        src = inspect.getsource(fn)
        for needle in ("hfea", "spxl", "SPXL", "UPRO", "TMF", "KMLM"):
            assert needle not in src, f"{fn.__name__} referenziert {needle}"


def test_audit_watchdog_survives_missing_sleeves():
    # audit_monthly_run indiziert STRATEGY_SYMBOLS direkt. Ein Label ohne
    # Eintrag waere ein KeyError am 8. des Monats — genau dann, wenn der
    # Waechter laufen soll.
    src = inspect.getsource(main.audit_monthly_run)
    for key in re.findall(r'STRATEGY_SYMBOLS\["(\w+)"\]', src):
        assert key in main.STRATEGY_SYMBOLS, f"audit greift auf fehlendes {key} zu"


def test_cloudbuild_retires_instead_of_deploying():
    cb = (ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    for sid in ("deploy-hfea", "deploy-rebalance-hfea", "deploy-spxl", "deploy-spxl-200sma"):
        assert f"id: '{sid}'" not in cb
    for job in ("rebalance_hfea", "daily_trade_spxl_200sma"):
        assert f"'update', 'http', '{job}'" not in cb
        assert f"jobs update http {job}" not in cb
    assert "retire-hfea-spxl-services" in cb
    retire = cb[cb.index("retire-hfea-spxl-services"):]
    retire = retire[:retire.index("waitFor")]
    for name in RETIRED_FUNCTIONS:
        assert name in retire, f"Bereinigung loescht {name} nicht"
