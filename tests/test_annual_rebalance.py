"""Jaehrliches Sleeve-Rebalancing (ab Januar 2027) und die Gates der Rotatoren.

Backtest 2026-09-23: einmal im Jahr alle Sleeves auf Zielallokation schlaegt den
reinen Einzahlungs-Tilt in 65 von 65 Fuenfzehnjahresfenstern. Die Tests pruefen
das Verhalten, auf das es live ankommt: Ziel minus Ist je Sleeve, erst abgeben,
dann auffuellen, nie mehr kaufen als die Verkaeufe wirklich gebracht haben, und
eine Rotation, die nicht mehr ausfaellt, nur weil kein neues Geld da ist.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main

PRICES = {"NTSD": 48.0, "WLDU": 18.0, "USFR": 50.0, "SSO": 110.0, "QLD": 97.6, "SGOV": 100.5,
          "SPXL": 290.0, "BIL": 91.5}
# 15.000 $ in den Sleeves: AAA 32 % (Ziel 21,25), World 12 % (21,25), Mix8 36,7 % (42,5), S&P 19,3 % (15)
BOOK = {"NTSD": 100, "WLDU": 100, "SSO": 50, "SPXL": 10}


class _Broker:
    def __init__(self, positions, prices=PRICES):
        self.pos = dict(positions)
        self.prices = prices
        self.orders = []

    def install(self, monkeypatch):
        monkeypatch.setattr(main, "list_positions", lambda api: [
            {"symbol": s, "qty": str(q), "market_value": str(q * self.prices[s])}
            for s, q in self.pos.items() if q > 0])
        monkeypatch.setattr(main, "get_latest_trade", lambda api, s: self.prices[s])

        def submit(api, symbol, qty, side):
            self.orders.append((side, symbol, qty))
            self.pos[symbol] = self.pos.get(symbol, 0) + (qty if side == "buy" else -qty)
            return {"id": str(len(self.orders) - 1)}
        monkeypatch.setattr(main, "submit_order", submit)
        monkeypatch.setattr(main, "wait_for_order_fill",
                            lambda api, oid: self.orders[int(oid)][2] * self.prices[self.orders[int(oid)][1]])

    def flow(self):
        raised = sum(q * self.prices[s] for side, s, q in self.orders if side == "sell")
        spent = sum(q * self.prices[s] for side, s, q in self.orders if side == "buy")
        return raised, spent


def _rebalance_calc(amounts, total_available=0.0, margin_approved=0.0):
    return {"strategy_amounts": amounts, "total_available": total_available,
            "margin_approved": margin_approved, "annual_rebalance": {"sleeves": {}}}


# ── Ziel minus Ist ───────────────────────────────────────────────────────────
def test_rebalance_amounts_bring_every_sleeve_to_target(monkeypatch):
    _Broker(BOOK).install(monkeypatch)
    res = main.calculate_annual_rebalance({}, total_investing=500.0)
    amounts = res["strategy_amounts"]
    assert sum(amounts.values()) == pytest.approx(500.0)          # genau das Budget
    total = 15000.0 + 500.0
    values = {"aaa": 4800.0, "world_trend": 1800.0, "mix8": 5500.0, "spx_trend": 2900.0}
    for key, (allo, _) in main.SLEEVES.items():
        assert values[key] + amounts[allo] == pytest.approx(main.strategy_allocations[allo] * total)
    assert amounts["aaa_allo"] < 0 and amounts["spx_trend_allo"] < 0
    assert amounts["world_trend_allo"] > 0 and amounts["mix8_allo"] > 0


def test_rebalance_refuses_to_run_on_unreadable_positions(monkeypatch):
    def boom(api):
        raise RuntimeError("Alpaca 503")
    monkeypatch.setattr(main, "list_positions", boom)
    calc = main.calculate_monthly_investments(
        {}, {"target_margin": 0, "metrics": {"cash": 500.0, "equity": 15000.0}}, "paper", annual_rebalance=True)
    assert "error" in calc["annual_rebalance"]
    assert all(v >= 0 for v in calc["strategy_amounts"].values())   # normaler Tilt, nichts abgeben
    assert not main._is_rebalance_run(calc)


def test_rebalance_amounts_only_when_the_orchestrator_asks(monkeypatch):
    _Broker(BOOK).install(monkeypatch)
    margin = {"target_margin": 0, "metrics": {"cash": 500.0, "equity": 15000.0}}
    normal = main.calculate_monthly_investments({}, margin, "paper")
    assert normal["annual_rebalance"] is None
    assert all(v >= 0 for v in normal["strategy_amounts"].values())
    reb = main.calculate_monthly_investments({}, margin, "paper", annual_rebalance=True)
    assert main._is_rebalance_run(reb)
    assert reb["strategy_amounts"]["aaa_allo"] < 0


@pytest.mark.parametrize("cash", [-2000.0, -1.92, 0.0, 500.0])
@pytest.mark.parametrize("target_margin", [0.0, 0.10])
def test_margin_budget_is_the_monthly_rule(monkeypatch, cash, target_margin):
    _Broker(BOOK).install(monkeypatch)
    calc = main.calculate_monthly_investments(
        {}, {"target_margin": target_margin, "metrics": {"cash": cash, "equity": 13000.0}}, "paper")
    available, margin, used = main._margin_budget(cash, 13000.0, target_margin)
    assert (available, margin, used) == pytest.approx(
        (calc["total_available"], calc["margin_approved"], calc["used_margin"]))


# ── Orchestrator: erst abgeben, dann auffuellen ──────────────────────────────
def _orchestrate(monkeypatch, cash_after, annual_rebalance=True, positions_fail=False, cash_before=500.0):
    broker = _Broker(BOOK)
    broker.install(monkeypatch)
    if positions_fail:
        def boom(api):
            raise RuntimeError("Alpaca 503")
        monkeypatch.setattr(main, "list_positions", boom)
    monkeypatch.setattr(main, "recalculate_all_strategies_cost_basis", lambda *a, **k: {"success": True})
    margin = {"allowed": False, "target_margin": 0, "gate_results": {}, "errors": [],
              "metrics": {"cash": cash_before, "equity": 15500.0, "portfolio_value": 15500.0,
                          "leverage": 1.0}}
    monkeypatch.setattr(main, "check_margin_conditions", lambda api, env="live": margin)
    monkeypatch.setattr(main, "get_account_info", lambda api: {
        "cash": cash_after, "equity": 15500.0, "portfolio_value": 15500.0, "maintenance_margin": 0.0})
    calls = []

    def runner(key):
        def fn(api, force_execute=False, investment_calc=None, margin_result=None, skip_order_wait=False, env="live"):
            calls.append((key, investment_calc["strategy_amounts"][main.SLEEVES[key][0]], investment_calc))
            return f"{key} ok"
        return fn
    for key, name in (("aaa", "make_monthly_buys_aaa"), ("world_trend", "make_monthly_buys_world_trend"),
                      ("mix8", "make_monthly_buys_mix8"), ("spx_trend", "make_monthly_buys_spx_trend")):
        monkeypatch.setattr(main, name, runner(key))
    main.monthly_invest_all_strategies({}, force_execute=True, env="paper", annual_rebalance=annual_rebalance)
    return calls


def test_orchestrator_sells_first_and_never_buys_more_than_the_sells_raised(monkeypatch, _no_outside_world):
    # Geplant: AAA -1.506,25, S&P -575, World +1.493,75, Mix8 +1.087,50 (Summe = 500 Budget).
    # Die Verkaeufe bringen aber nur 1.500 statt 2.081,25 -> Cash danach 2.000.
    calls = _orchestrate(monkeypatch, cash_after=2000.0)
    order = [k for k, _, _ in calls]
    assert set(order[:2]) == {"aaa", "spx_trend"} and set(order[2:]) == {"world_trend", "mix8"}
    amounts = {k: a for k, a, _ in calls}
    assert amounts["aaa"] == pytest.approx(-1506.25) and amounts["spx_trend"] == pytest.approx(-575.0)
    assert amounts["world_trend"] + amounts["mix8"] == pytest.approx(2000.0)   # gekappt aufs echte Cash
    assert amounts["world_trend"] / amounts["mix8"] == pytest.approx(1493.75 / 1087.5)
    msg = next(m for _, m in _no_outside_world if "Account Status" in m)
    assert "Jährliches Rebalancing" in msg and "Budget per strategy" not in msg


def test_orchestrator_does_not_scale_up_beyond_the_plan(monkeypatch):
    calls = _orchestrate(monkeypatch, cash_after=9000.0)
    amounts = {k: a for k, a, _ in calls}
    assert amounts["world_trend"] == pytest.approx(1493.75) and amounts["mix8"] == pytest.approx(1087.5)


def test_normal_month_runs_every_sleeve_once_in_registry_order(monkeypatch):
    calls = _orchestrate(monkeypatch, cash_after=0.0, annual_rebalance=False)
    assert [k for k, _, _ in calls] == list(main.SLEEVES)
    assert all(a >= 0 for _, a, _ in calls)
    assert len({id(c) for _, _, c in calls}) == 1            # ein gemeinsames Budget wie bisher


def test_unreadable_positions_fall_back_to_a_normal_month(monkeypatch, _no_outside_world):
    calls = _orchestrate(monkeypatch, cash_after=0.0, positions_fail=True)
    assert [k for k, _, _ in calls] == list(main.SLEEVES)
    assert all(a >= 0 for _, a, _ in calls)
    msg = next(m for _, m in _no_outside_world if "Account Status" in m)
    assert "Rebalancing ausgesetzt" in msg


# ── Rotator ──────────────────────────────────────────────────────────────────
def _rotator(monkeypatch, positions, weights, cash_weight, calc, peak_nav=0.0, key="mix8"):
    broker = _Broker(positions)
    broker.install(monkeypatch)
    picks = [p for p, w in weights.items() if w > 0]
    monkeypatch.setattr(main, "plan_rotator_weights", lambda api, cfg: {
        "scores": {p: 0.1 for p in picks}, "picks": picks,
        "weights": {**{pos: 0.0 for _, pos in cfg["candidates"]}, **weights},
        "cash_weight": cash_weight, "scale": 1.0, "realized_vols": {}})
    monkeypatch.setattr(main, "load_balances", lambda env="live": {key: {"peak_nav": peak_nav}})
    saved = {}
    monkeypatch.setattr(main, "save_balance", lambda k, d, env="live", merge=False: saved.update(d))
    margin = {"target_margin": 0, "metrics": {"leverage": 1.0, "portfolio_value": 0, "equity": 0}}
    run = main.make_monthly_buys_mix8 if key == "mix8" else main.make_monthly_buys_aaa
    res = run({}, force_execute=True, investment_calc=calc, margin_result=margin, env="paper")
    return res, broker, saved


def test_rotator_rotates_when_there_is_no_new_money(monkeypatch):
    """Gate zu, Cash negativ -> 0 $ fuer jede Sleeve. Frueher: ganzer Lauf uebersprungen."""
    calc = {"strategy_amounts": {"mix8_allo": 0.0}, "total_available": 0.0, "margin_approved": 0.0}
    res, broker, _ = _rotator(monkeypatch, {"SSO": 50}, {"QLD": 1.0}, 0.0, calc)
    assert ("sell", "SSO", 50) in broker.orders
    assert any(side == "buy" and s == "QLD" for side, s, _ in broker.orders)
    raised, spent = broker.flow()
    assert spent <= raised + 1e-6                              # kein neues Geld


def test_rotator_blocked_contribution_still_rotates_without_it(monkeypatch):
    calc = {"strategy_amounts": {"mix8_allo": 300.0}, "total_available": 0.0, "margin_approved": 0.0}
    res, broker, _ = _rotator(monkeypatch, {"SSO": 50}, {"QLD": 1.0}, 0.0, calc)
    raised, spent = broker.flow()
    assert raised > 0 and spent <= raised + 1e-6               # die 300 $ bleiben draussen


def test_rotator_gives_back_its_rebalancing_amount(monkeypatch):
    res, broker, saved = _rotator(monkeypatch, {"SSO": 100}, {"SSO": 1.0}, 0.0,
                                  _rebalance_calc({"mix8_allo": -2000.0}), peak_nav=12000.0)
    raised, spent = broker.flow()
    assert raised == pytest.approx(2000.0) and spent == 0
    assert broker.pos["SSO"] * 110.0 == pytest.approx(9000.0)


@pytest.mark.parametrize("amount", [-2000.0, 1000.0])
def test_rotator_peak_moves_with_the_flow(monkeypatch, amount):
    """Peak 12.000, Wert 11.000 (DD -8,3 %). Nach der Januar-Abgabe darf der
    Folgemonat keinen Drawdown von -25 % sehen - sonst droht ein falscher DD-30-Stopp."""
    calc = _rebalance_calc({"mix8_allo": amount}) if amount < 0 else \
        {"strategy_amounts": {"mix8_allo": amount}, "total_available": 5000.0, "margin_approved": 0.0}
    _, broker, saved = _rotator(monkeypatch, {"SSO": 100}, {"SSO": 1.0}, 0.0, calc, peak_nav=12000.0)
    after = broker.pos["SSO"] * 110.0
    assert saved["peak_nav"] == pytest.approx(12000.0 * (11000.0 + amount) / 11000.0)
    assert after / saved["peak_nav"] - 1 == pytest.approx(11000.0 / 12000.0 - 1)   # DD unveraendert
    assert saved["last_momentum_check"]["dd_triggered"] is False


# ── Trend-Sleeves ────────────────────────────────────────────────────────────
def test_trend_sleeve_over_target_sells_pro_rata_in_whole_wldu_shares(monkeypatch):
    broker = _Broker({"WLDU": 87, "USFR": 30})                # 1.566 + 1.500 = 3.066 $
    broker.install(monkeypatch)
    monkeypatch.setattr(main, "load_balances", lambda env="live": {"world_trend": {"total_invested": 3000.0}})
    saved = {}
    monkeypatch.setattr(main, "save_balance", lambda k, d, env="live", merge=False: saved.update(d))
    monkeypatch.setattr(main, "trend_signals", lambda *a, **k: pytest.fail("Abgeben braucht kein Signal"))
    margin = {"target_margin": 0, "metrics": {"leverage": 1.0}}
    res = main.make_monthly_buys_world_trend({}, force_execute=True, investment_calc=_rebalance_calc(
        {"world_trend_allo": -600.0}), margin_result=margin, env="paper")
    sold = {s: q for side, s, q in broker.orders if side == "sell"}
    frac = 600.0 / 3066.0
    assert sold["WLDU"] == int(87 * frac)                     # ganze Stuecke, abgerundet
    assert sold["USFR"] == pytest.approx(30 * frac)
    assert not any(side == "buy" for side, _, _ in broker.orders)
    assert "rebalancing" in res
    assert saved["total_invested"] == pytest.approx(3000.0 - broker.flow()[0])


def test_trend_rebalancing_buy_is_not_blocked_by_the_contribution_gate(monkeypatch):
    """Die Kaeufe im Rebalancing sind durch Verkaeufe gedeckt; das Budget haelt der
    Orchestrator ein. Das normale Gate (hier: 0 $ Kaufkraft) darf sie nicht stoppen."""
    broker = _Broker({})
    broker.install(monkeypatch)
    monkeypatch.setattr(main, "trend_signals", lambda cfg, today_iso=None: {
        "SPXL": {"on": True, "price": 1.0, "sma": 1.0, "diff_pct": 0.0, "up": 0, "dn": 0}})
    monkeypatch.setattr(main, "load_balances", lambda env="live": {})
    monkeypatch.setattr(main, "save_balance", lambda k, d, env="live", merge=False: None)
    margin = {"target_margin": 0, "metrics": {"leverage": 1.0}}
    main.make_monthly_buys_spx_trend({}, force_execute=True, investment_calc=_rebalance_calc(
        {"spx_trend_allo": 870.0}), margin_result=margin, env="paper")
    assert [s for side, s, _ in broker.orders if side == "buy"] == ["SPXL"]


def test_rotator_peak_ignores_money_that_never_arrived(monkeypatch):
    """Paper-Lauf 2026-09-23: AAA sollte 386,87 $ bekommen, NTSD geht nur in ganzen
    Stuecken, 24,73 $ blieben liegen. Der Peak darf nur den echten Wertsprung
    mitnehmen, sonst steht die Sleeve ohne jeden Verlust im Drawdown."""
    calc = {"strategy_amounts": {"aaa_allo": 100.0}, "total_available": 5000.0, "margin_approved": 0.0}
    _, broker, saved = _rotator(monkeypatch, {"NTSD": 10}, {"NTSD": 1.0}, 0.0, calc, peak_nav=480.0, key="aaa")
    assert broker.pos["NTSD"] == 12                           # 2 ganze Stuecke fuer 100 $
    assert saved["peak_nav"] == pytest.approx(12 * 48.0)      # am Hoch geblieben, kein Schein-DD


def test_rebalance_buys_get_the_full_sell_proceeds_despite_margin_debt(monkeypatch, _no_outside_world):
    """Die Margin wird nur durch Einzahlungen abgebaut, nie durch Verkaufserloese.

    Konto: -1.500 Margin-Schuld, Gates zu (kein neues Budget). Ziel minus Ist
    ergibt Verkaeufe von 2.262,50 und Kaeufe von 2.262,50. Der Erloes gehoert
    vollstaendig den auffuellenden Sleeves; das Cash bleibt bei -1.500.
    Vorher band `max(0, cash)` den Erloes an die Schuld und kappte die Kaeufe
    auf 762,50 (34 %).
    """
    calls = _orchestrate(monkeypatch, cash_after=762.5, cash_before=-1500.0)
    amounts = {k: a for k, a, _ in calls}
    assert amounts["aaa"] == pytest.approx(-1612.5)
    assert amounts["spx_trend"] == pytest.approx(-650.0)
    assert amounts["world_trend"] + amounts["mix8"] == pytest.approx(2262.5)


def test_leverage_gate_measures_positions_against_equity(monkeypatch, _no_outside_world):
    """Gate 4 muss den ECHTEN Kontohebel messen, nicht portfolio_value/equity.

    Alpaca liefert portfolio_value und equity als synonyme Felder - der Quotient
    ist bei jedem reglichen Long-Konto exakt 1,0, egal wie viel Margin laeuft.
    Das Gate 'leverage < 1.14' konnte deshalb nie ausloesen. Gemessen am Live-
    Konto 2026-09-24: equity 13.188,75, portfolio_value 13.188,75 (identisch),
    long_market_value 14.508,07 -> echter Hebel 1,10.

    Hier: 1,20x Hebel, also ueber der 1,14-Grenze. Das Gate muss fallen.
    """
    monkeypatch.setattr(main, "get_all_market_data",
                        lambda sym, env="live": {"price": 200.0, "sma200": 100.0})
    monkeypatch.setattr(main, "get_fred_rate", lambda: 4.0)
    monkeypatch.setattr(main, "get_account_info", lambda api: {
        "cash": -2000.0, "equity": 10000.0, "portfolio_value": 10000.0,
        "long_market_value": 12000.0, "maintenance_margin": 3000.0})
    res = main.check_margin_conditions({}, env="paper")
    assert res["metrics"]["leverage"] == pytest.approx(1.20)
    assert res["gate_results"]["leverage"] is False
    assert res["target_margin"] == 0.0
