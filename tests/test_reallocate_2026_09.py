"""Planer der einmaligen Umschichtung 2026-09-23: nur Differenzen handeln."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main
from scripts import reallocate_2026_09 as R

PRICES = {"SPXL": 290.0, "BIL": 91.5, "QLD": 97.6, "BND": 71.3, "DBC": 32.6, "EET": 115.0, "NTSD": 48.2, "SAA": 33.5,
          "SHV": 110.2, "WLDU": 38.0, "UGLD": 22.0, "USFR": 50.3, "SSO": 110.0, "GLD": 446.0,
          "SGOV": 100.5, "UGL": 60.0, "UBT": 18.0, "UST": 40.0, "EFO": 50.0, "EEM": 55.0,
          "IEF": 95.0, "TLT": 88.0, "KMLM": 30.0, "AGG": 96.0}
BOOK = [  # Live-Buch am 22.09.2026 abends, gerundet
    {"symbol": "QLD", "qty": "10.62", "market_value": "1036.96"},
    {"symbol": "BND", "qty": "10.71", "market_value": "764.23"},
    {"symbol": "DBC", "qty": "27.80", "market_value": "905.33"},
    {"symbol": "SAA", "qty": "21.38", "market_value": "715.61"},
    {"symbol": "EET", "qty": "3.14", "market_value": "361.38"},
    {"symbol": "NTSD", "qty": "1", "market_value": "48.16"},
    {"symbol": "SHV", "qty": "1.36", "market_value": "150.33"},
]
SLEEVES = {
    "aaa": {"weights": {"DBC": 0.30, "SAA": 0.25, "EET": 0.20, "NTSD": 0.0, "UBT": 0.0, "UST": 0.0,
                        "UGL": 0.0, "SHV": 0.25}},
    "mix8": {"weights": {"SSO": 0.35, "QLD": 0.30, "EFO": 0.0, "EEM": 0.0, "GLD": 0.0, "IEF": 0.0,
                         "TLT": 0.0, "KMLM": 0.0, "SGOV": 0.35}},
    "world_trend": {"weights": {"WLDU": 0.5, "UGLD": 0.0, "USFR": 0.5}},
    "spx_trend": {"weights": {"SPXL": 1.0, "BIL": 0.0}},
}
EQUITY = 13350.41


def _plan():
    sleeves = {k: dict(v) for k, v in SLEEVES.items()}
    investable, targets = R.build_targets(EQUITY, sleeves)
    return investable, targets, sleeves, R.plan_orders(BOOK, targets, PRICES)


def test_budgets_follow_the_weights():
    investable, targets, sleeves, _ = _plan()
    assert investable == pytest.approx(EQUITY * (1 - R.CASH_BUFFER))
    for key, (allo, _) in main.SLEEVES.items():
        assert sleeves[key]["budget"] == pytest.approx(investable * main.strategy_allocations[allo])
    assert sum(targets.values()) == pytest.approx(investable)


def test_qld_moves_to_mix8_without_a_round_trip():
    _, targets, _, (sells, buys, _) = _plan()
    qld_sells = [o for o in sells if o["symbol"] == "QLD"]
    qld_buys = [o for o in buys if o["symbol"] == "QLD"]
    assert not (qld_sells and qld_buys)
    # Ziel 0,5 x 0,995 x Equity x 30 % = ~1.993 USD > 1.037 Bestand -> nur Zukauf
    assert qld_buys and not qld_sells
    assert qld_buys[0]["est"] == pytest.approx(targets["QLD"] - 1036.96, rel=1e-6)


def test_positions_without_a_target_are_closed_not_trimmed():
    _, _, _, (sells, _, _) = _plan()
    bnd = [o for o in sells if o["symbol"] == "BND"]
    assert bnd and bnd[0]["kind"] == "close"


def test_whole_share_residual_of_wldu_lands_in_usfr():
    _, targets, _, (_, buys, _) = _plan()
    wldu = next(o for o in buys if o["symbol"] == "WLDU")
    usfr = next(o for o in buys if o["symbol"] == "USFR")
    assert wldu["qty"] == int(wldu["qty"])
    residual = targets["WLDU"] - wldu["est"]
    assert usfr["est"] == pytest.approx(targets["USFR"] + residual, abs=PRICES["USFR"])


def test_every_order_symbol_belongs_to_a_live_sleeve_or_is_an_exit():
    _, _, _, (sells, buys, _) = _plan()
    owned = {s for syms in main.STRATEGY_SYMBOLS.values() for s in syms}
    assert all(o["symbol"] in owned for o in buys)
    assert all(o["symbol"] in owned or o["kind"] == "close" for o in sells)


def test_live_execution_requires_the_token():
    with pytest.raises(ValueError):
        R.execute({}, env="live", confirmation=None)


def test_defensive_etf_is_trimmed_to_the_residual_not_closed_and_rebought():
    """NTSD ist nicht fraktional; der Rest gehoert in AAAs SHV. SHV wird also
    auf den Rest getrimmt statt geschlossen (und dann zurueckgekauft)."""
    sleeves = {k: dict(v) for k, v in SLEEVES.items()}
    sleeves["aaa"] = {"weights": {"DBC": 0.30, "EET": 0.25, "NTSD": 0.45, "SAA": 0.0, "UBT": 0.0,
                                  "UST": 0.0, "UGL": 0.0, "SHV": 0.0}}
    _, targets = R.build_targets(EQUITY, sleeves)
    prices = {**PRICES, "NTSD": 100.0}      # 14 ganze Stueck, ~46 USD Rest < 150 USD SHV
    sells, buys, _ = R.plan_orders(BOOK, targets, prices)
    shv_sells = [o for o in sells if o["symbol"] == "SHV"]
    assert shv_sells and shv_sells[0]["kind"] == "qty"
    assert not [o for o in buys if o["symbol"] == "SHV"]
    ntsd = next(o for o in buys if o["symbol"] == "NTSD")
    residual = targets["NTSD"] - 48.16 - ntsd["est"]
    assert R.TOLERANCE < residual < 150.33
    kept = 150.33 - shv_sells[0]["est"]
    assert kept == pytest.approx(residual, abs=prices["SHV"] * 1e-6 + 0.01)
