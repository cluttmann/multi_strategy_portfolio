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
FREED_TICKERS = {"UPRO", "TMF", "KMLM", "SPXL", "SGOV"}


def test_only_active_sleeves_carry_weight():
    assert set(main.strategy_allocations) == {"dual_momentum_allo", "aaa_allo"}
    assert sum(main.strategy_allocations.values()) == pytest.approx(1.0)


def test_interim_weights_keep_the_one_to_two_ratio():
    # Uebergangsgewichte bis zu den neuen Sleeves: DM und AAA behalten ihr
    # Verhaeltnis vom 21.09. (25:50). Wer das aendert, soll es bewusst tun.
    w = main.strategy_allocations
    assert w["aaa_allo"] == pytest.approx(2 * w["dual_momentum_allo"])


def test_retired_sleeves_own_no_tickers():
    assert "hfea" not in main.STRATEGY_SYMBOLS
    assert "spxl_sma" not in main.STRATEGY_SYMBOLS
    owned = {t for syms in main.STRATEGY_SYMBOLS.values() for t in syms}
    assert owned.isdisjoint(FREED_TICKERS)


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


@pytest.mark.parametrize("aaa_value,dm_value", [
    (2180.81, 1801.19),   # Buch am 22.09.2026: AAA 54,8 %, DM 45,2 %
    (1000.0, 3000.0),     # AAA stark untergewichtet
    (3500.0, 500.0),      # AAA uebergewichtet
])
def test_contribution_tilt_never_rewards_the_overweight_sleeve(monkeypatch, aaa_value, dm_value):
    """Mit zwei Sleeves hatte die 50-%-Kappe AAA unter sein Ziel gedrueckt und
    den Ueberschuss an DM gegeben — auch wenn DM uebergewichtet war."""
    total = aaa_value + dm_value
    monkeypatch.setattr(main, "get_all_strategy_values", lambda api: {
        "dual_momentum": dm_value, "aaa": aaa_value, "total": total})
    adj = main.calculate_rebalanced_allocations({})["adjusted_allocations"]
    assert sum(adj.values()) == pytest.approx(1.0)
    target = main.strategy_allocations
    aaa_under = aaa_value / total < target["aaa_allo"]
    if aaa_under:
        assert adj["aaa_allo"] >= target["aaa_allo"] - 1e-9
        assert adj["dual_momentum_allo"] <= target["dual_momentum_allo"] + 1e-9
    else:
        assert adj["aaa_allo"] <= target["aaa_allo"] + 1e-9
