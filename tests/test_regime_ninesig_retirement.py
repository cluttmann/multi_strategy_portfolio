"""Sichert das Retirement von Regime SSO und 9-Sig vom 21.09.2026 ab.

Der Sinn dieser Tests ist nicht, die Entfernung zu wiederholen, sondern zu
verhindern, dass sie versehentlich rueckgaengig gemacht wird — etwa durch einen
Merge, der die alten Gewichte zurueckholt, oder durch einen cloudbuild-Step,
der eine Function ohne Entry-Point deployen will.
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
    "monthly_nine_sig_contributions",
    "quarterly_nine_sig_signal",
    "daily_regime_check_route",
    "monthly_buy_regime_sso",
    "backfill_regime_scores_route",
]


def test_retired_allocations_stay_out_and_weights_sum_to_one():
    # Die exakten Gewichte stehen in test_hfea_spxl_retirement.py — HFEA und
    # SPXL gingen einen Tag spaeter. Hier bleibt nur, was dieses Retirement
    # dauerhaft garantieren muss.
    assert "nine_sig_allo" not in main.strategy_allocations
    assert "regime_sso_allo" not in main.strategy_allocations
    assert sum(main.strategy_allocations.values()) == pytest.approx(1.0)


def test_retired_sleeves_own_no_tickers():
    assert "nine_sig" not in main.STRATEGY_SYMBOLS
    assert "regime_sso" not in main.STRATEGY_SYMBOLS
    owned = {t for syms in main.STRATEGY_SYMBOLS.values() for t in syms}
    # Die vier freigewordenen Ticker duerfen von keinem Sleeve mehr beansprucht
    # werden, sonst ordnet die Kostenbasis-Rechnung eine tote Position zu.
    assert owned.isdisjoint({"TQQQ", "AGG"})
    # SSO und USFR wurden am 2026-09-23 bewusst wieder vergeben.
    assert "SSO" in main.STRATEGY_SYMBOLS["mix8"]
    assert "USFR" in main.STRATEGY_SYMBOLS["world_trend"]


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
        src = inspect.getsource(fn).lower()
        assert "nine_sig" not in src, f"{fn.__name__} referenziert 9-Sig"
        assert "regime_sso" not in src, f"{fn.__name__} referenziert Regime SSO"


def test_every_cloudbuild_entry_point_resolves():
    """Der Fehler von 2026-05-17: Route geloescht, Deploy-Step blieb stehen.
    Der Container baut dann durch und stirbt beim Import."""
    cb = (ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    for entry in sorted(set(re.findall(r"--entry-point=(\w+)", cb))):
        assert hasattr(main, entry), f"cloudbuild deployt {entry}, main.py kennt es nicht"
        assert callable(getattr(main, entry))


def test_cloudbuild_retires_instead_of_deploying():
    cb = (ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    for sid in ("deploy-nine-sig-monthly", "deploy-nine-sig-quarterly",
                "deploy-regime-check", "deploy-regime-sso", "deploy-regime-backfill"):
        assert f"id: '{sid}'" not in cb
    assert "jobs update http quarterly_nine_sig_signal" not in cb
    assert "jobs update http daily_regime_check" not in cb
    assert "retire-regime-ninesig-services" in cb
    assert "gcloud functions delete $$FN" in cb
