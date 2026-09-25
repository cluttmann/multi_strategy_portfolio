"""AAA / World-Trend / Mix8 / S&P-Trend ab 2026-09-23.

Die Tests pruefen Verhalten, nicht Quelltext: dieselbe Zustandsmaschine wie
der Backtest, keine Kaeufe ueber die eigenen Erloese hinaus, keine Orders bei
fehlenden Daten, und das Sizing, auf dem die Zielallokation 25/25/50 beruht.
"""
import random
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main

ROOT = Path(__file__).resolve().parent.parent


# ── Gewichte und Zuordnung ───────────────────────────────────────────────────
def test_target_allocation():
    assert main.strategy_allocations == {"aaa_allo": 0.2125, "world_trend_allo": 0.2125,
                                         "mix8_allo": 0.425, "spx_trend_allo": 0.15}
    assert sum(main.strategy_allocations.values()) == pytest.approx(1.0)


def test_registry_matches_weights_and_tickers():
    assert {allo for allo, _ in main.SLEEVES.values()} == set(main.strategy_allocations)
    assert set(main.SLEEVES) == set(main.STRATEGY_SYMBOLS)
    # Universes may overlap. Ownership is tested against actual ledger shares
    # in test_shared_integration, rather than inferred from membership.
    for key, syms in main.STRATEGY_SYMBOLS.items():
        assert len(syms) == len(set(syms))


def test_dual_momentum_is_retired():
    assert "dual_momentum" not in main.STRATEGY_SYMBOLS
    assert not hasattr(main, "monthly_dual_momentum")
    assert not hasattr(main, "monthly_dual_momentum_strategy")
    owned = {t for syms in main.STRATEGY_SYMBOLS.values() for t in syms}
    assert owned.isdisjoint({"SPUU", "BND"})


def test_mix8_holds_kmlm_not_dbmf():
    # Die Managed-Futures-Reihe im Backtest IST KMLM.
    assert ("KMLM", "KMLM") in main.mix8_config["candidates"]
    assert "DBMF" not in main.STRATEGY_SYMBOLS["mix8"]


def test_wldu_is_sized_in_whole_shares():
    assert "WLDU" in main.NON_FRACTIONABLE_TICKERS


# ── World-Trend: Zustandsmaschine = Backtest ─────────────────────────────────
def _backtest_state(closes, sma, band, confirm):
    """Die Schleife aus research/holistic (w_trend), auf pandas-rolling."""
    s = pd.Series(closes).rolling(sma).mean()
    st, up, dn = False, 0, 0
    for p, m in zip(closes, s):
        if pd.isna(m):
            continue
        if p > m * (1 + band):
            up, dn = up + 1, 0
        elif p < m * (1 - band):
            dn, up = dn + 1, 0
        else:
            up = dn = 0
        if up >= confirm:
            st = True
        if dn >= confirm:
            st = False
    return st


@pytest.mark.parametrize("cfg", [main.world_trend_config, main.spx_trend_config],
                         ids=["world_confirm3_sma150", "spx_confirm1_sma200"])
def test_trend_state_machine_matches_backtest(cfg):
    rnd = random.Random(7)
    mismatches = 0
    for trial in range(60):
        px, closes = 100.0, []
        drift = rnd.choice([-0.0008, 0.0, 0.0008])
        for _ in range(460):
            px *= 1 + drift + rnd.gauss(0, 0.012)
            closes.append(px)
        for cut in range(cfg["sma_period"] + 5, len(closes), 17):
            got = main._wt_leg_state(closes[:cut], cfg["sma_period"], cfg["band"], cfg["confirm_days"])["on"]
            ref = _backtest_state(closes[:cut], cfg["sma_period"], cfg["band"], cfg["confirm_days"])
            mismatches += got != ref
    assert mismatches == 0


def test_world_trend_needs_full_sma_window():
    with pytest.raises(main.EodhdDataError):
        main._wt_leg_state([100.0] * 149, 150, 0.01, 3)


def test_world_trend_weights():
    cfg = main.world_trend_config
    spx = main.spx_trend_config
    assert main.world_trend_target_weights(spx, {"SPXL": {"on": True}}) == {"SPXL": 1.0, "BIL": 0.0}
    assert main.world_trend_target_weights(spx, {"SPXL": {"on": False}}) == {"SPXL": 0.0, "BIL": 1.0}
    both = {"WLDU": {"on": True}, "UGLD": {"on": True}}
    one = {"WLDU": {"on": True}, "UGLD": {"on": False}}
    none = {"WLDU": {"on": False}, "UGLD": {"on": False}}
    assert main.world_trend_target_weights(cfg, both) == {"WLDU": 0.5, "UGLD": 0.5, "USFR": 0.0}
    assert main.world_trend_target_weights(cfg, one) == {"WLDU": 0.5, "UGLD": 0.0, "USFR": 0.5}
    assert main.world_trend_target_weights(cfg, none) == {"WLDU": 0.0, "UGLD": 0.0, "USFR": 1.0}


# ── World-Trend: handelt nur innerhalb der eigenen Positionen ────────────────
class _FakeBroker:
    def __init__(self, positions, prices):
        self.pos = dict(positions)          # symbol -> shares
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
            return {"id": f"o{len(self.orders)}", "_n": qty * self.prices[symbol]}
        monkeypatch.setattr(main, "submit_order", submit)
        monkeypatch.setattr(main, "wait_for_order_fill",
                            lambda api, oid: [o for o in self.orders][int(oid[1:]) - 1][2]
                            * self.prices[[o for o in self.orders][int(oid[1:]) - 1][1]])


PRICES = {"WLDU": 38.0, "UGLD": 22.0, "USFR": 50.0,
          "NTSD": 48.0, "SAA": 33.0, "EET": 115.0, "SSO": 110.0}


def _spent_vs_raised(orders, prices):
    raised = sum(q * prices[s] for side, s, q in orders if side == "sell")
    spent = sum(q * prices[s] for side, s, q in orders if side == "buy")
    return spent, raised


@pytest.mark.parametrize("start,target", [
    ({"WLDU": 44, "USFR": 33}, {"WLDU": 0.5, "UGLD": 0.5, "USFR": 0.0}),   # Gold schaltet an
    ({"WLDU": 44, "UGLD": 76}, {"WLDU": 0.5, "UGLD": 0.0, "USFR": 0.5}),   # Gold schaltet ab
    ({"WLDU": 44, "UGLD": 76}, {"WLDU": 0.0, "UGLD": 0.0, "USFR": 1.0}),   # beides aus
])
def test_world_trend_never_buys_beyond_its_own_proceeds(monkeypatch, start, target):
    # Fremde Positionen liegen daneben und duerfen nicht angefasst werden.
    broker = _FakeBroker({**start, "SAA": 10, "SSO": 5}, PRICES)
    broker.install(monkeypatch)
    main._wt_trade_to_targets({}, main.world_trend_config, target)
    spent, raised = _spent_vs_raised(broker.orders, PRICES)
    assert spent <= raised + 1e-6
    assert all(s in main.STRATEGY_SYMBOLS["world_trend"] for _, s, _ in broker.orders)
    assert broker.pos["SAA"] == 10 and broker.pos["SSO"] == 5


def test_world_trend_whole_share_residual_goes_to_usfr(monkeypatch):
    broker = _FakeBroker({"USFR": 100}, PRICES)              # 5.000 USD, alles aus
    broker.install(monkeypatch)
    main._wt_trade_to_targets({}, main.world_trend_config, {"WLDU": 0.5, "UGLD": 0.5, "USFR": 0.0})
    bought = {s: q for side, s, q in broker.orders if side == "buy"}
    assert bought["WLDU"] == int(bought["WLDU"])            # ganze Stuecke
    assert "USFR" in bought                                 # Rundungsrest zurueck in USFR


def _signals(wldu_on, ugld_on):
    base = dict(price=1.0, sma=1.0, diff_pct=0.0, up=0, dn=0, last_close_date="2026-09-22")
    return {"WLDU": {**base, "on": wldu_on}, "UGLD": {**base, "on": ugld_on}}


def _daily(monkeypatch, broker, signals, stored_states):
    broker.install(monkeypatch)
    monkeypatch.setattr(main, "check_trading_day", lambda mode="daily": True)
    monkeypatch.setattr(main, "trend_signals", lambda cfg, today_iso=None: signals)
    monkeypatch.setattr(main, "load_balances", lambda env="live": {"world_trend": {"leg_states": stored_states}})
    saved = {}
    monkeypatch.setattr(main, "save_balance", lambda k, d, env="live", merge=False: saved.update(d))
    return main.daily_world_trend({}, env="paper"), saved


def test_daily_world_trend_does_nothing_without_a_flip(monkeypatch):
    broker = _FakeBroker({"WLDU": 44, "UGLD": 76}, PRICES)
    res, saved = _daily(monkeypatch, broker, _signals(True, True), {"WLDU": True, "UGLD": True})
    assert broker.orders == []
    assert saved["leg_states"] == {"WLDU": True, "UGLD": True}


def test_daily_world_trend_trades_on_a_flip(monkeypatch):
    broker = _FakeBroker({"WLDU": 44, "UGLD": 76}, PRICES)
    res, saved = _daily(monkeypatch, broker, _signals(True, False), {"WLDU": True, "UGLD": True})
    sold = {s for side, s, _ in broker.orders if side == "sell"}
    bought = {s for side, s, _ in broker.orders if side == "buy"}
    assert "UGLD" in sold and "USFR" in bought


def test_daily_world_trend_data_error_means_no_orders(monkeypatch):
    broker = _FakeBroker({"WLDU": 44, "UGLD": 76}, PRICES)
    broker.install(monkeypatch)
    monkeypatch.setattr(main, "check_trading_day", lambda mode="daily": True)

    def boom(cfg, today_iso=None):
        raise main.EodhdDataError("URTH.US: Live-Kurs ist 300 min alt")
    monkeypatch.setattr(main, "trend_signals", boom)
    res = main.daily_world_trend({}, env="paper")
    assert res.startswith("❌") and broker.orders == []


# ── Rotator: Sizing und Datenfehler ──────────────────────────────────────────
def test_rotator_sizing_is_weighted_sum_and_normalized(monkeypatch):
    cfg = main.mix8_config
    scores = {"SSO": 0.30, "QLD": 0.25, "EFO": 0.10, "EET": 0.05, "UGLD": -0.1,
              "IEF": 0.0, "UBT": -0.2, "KMLM": 0.01}
    by_signal = {sig: scores[pos] for sig, pos in cfg["candidates"]}
    monkeypatch.setattr(main, "_rotator_momentum", lambda api, s, lbs, ws: by_signal[s])
    vols = {"SSO": 0.40, "QLD": 0.50}
    monkeypatch.setattr(main, "_rotator_realized_vol", lambda api, s, window=60: vols[s])
    plan = main.plan_rotator_weights({}, cfg)
    assert plan["picks"] == ["SSO", "QLD"]
    raw = {"SSO": (1 / 0.4) / (1 / 0.4 + 1 / 0.5), "QLD": (1 / 0.5) / (1 / 0.4 + 1 / 0.5)}
    est = raw["SSO"] * 0.40 + raw["QLD"] * 0.50           # gewichtete Summe, nicht Wurzel
    k = min(1.0, 0.25 / est)
    assert plan["weights"]["SSO"] == pytest.approx(raw["SSO"] * k)
    assert plan["weights"]["QLD"] == pytest.approx(raw["QLD"] * k)
    assert plan["cash_weight"] == pytest.approx(1 - k)


def test_rotator_single_positive_pick_gets_full_weight_before_scaling(monkeypatch):
    cfg = main.aaa_config
    by_signal = {sig: (0.2 if pos == "UGL" else -0.1) for sig, pos in cfg["candidates"]}
    monkeypatch.setattr(main, "_rotator_momentum", lambda api, s, lbs, ws: by_signal[s])
    monkeypatch.setattr(main, "_rotator_realized_vol", lambda api, s, window=60: 0.20)
    plan = main.plan_rotator_weights({}, cfg)
    assert plan["picks"] == ["UGL"] and plan["weights"]["UGL"] == pytest.approx(1.0)


def test_rotator_uses_dividend_adjusted_prices(monkeypatch):
    seen = []

    def bars(api, sym, days=400, raw=False, adjustment="split"):
        seen.append(adjustment)
        return [100.0 + i * 0.1 for i in range(400)]
    monkeypatch.setattr(main, "get_alpaca_historical_bars", bars)
    main._rotator_momentum({}, "KMLM", [63, 126, 252], [1 / 3] * 3)
    main._rotator_realized_vol({}, "KMLM")
    assert seen == ["all", "all"]


def test_rotator_data_error_sends_no_orders(monkeypatch):
    monkeypatch.setattr(main, "get_alpaca_historical_bars", lambda *a, **k: None)
    monkeypatch.setattr(main, "load_balances", lambda env="live": {})
    monkeypatch.setattr(main, "list_positions", lambda api: [{"symbol": "SSO", "qty": "10", "market_value": "1100"}])
    orders = []
    monkeypatch.setattr(main, "submit_order", lambda *a, **k: orders.append(a) or {"id": "x"})
    calc = {"strategy_amounts": {"mix8_allo": 500.0}, "total_available": 5000.0, "margin_approved": 0.0}
    margin = {"target_margin": 0, "metrics": {"leverage": 1.0}}
    res = main.make_monthly_buys_mix8({}, force_execute=True, investment_calc=calc, margin_result=margin, env="paper")
    assert res.startswith("❌") and orders == []


# ── Beitrags-Kippung mit drei Sleeves ────────────────────────────────────────
@pytest.mark.parametrize("values", [
    {"aaa": 2125.0, "world_trend": 2125.0, "mix8": 4250.0, "spx_trend": 1500.0},   # im Ziel
    {"aaa": 3000.0, "world_trend": 2000.0, "mix8": 3500.0, "spx_trend": 1500.0},   # AAA ueber, Mix8 unter
    {"aaa": 2500.0, "world_trend": 2500.0, "mix8": 3000.0, "spx_trend": 2000.0},   # Mix8 weit unter
    {"aaa": 1500.0, "world_trend": 3000.0, "mix8": 4500.0, "spx_trend": 1000.0},   # World ueber, S&P unter
    {"aaa": 2000.0, "world_trend": 2000.0, "mix8": 5500.0, "spx_trend": 500.0},    # Mix8 ueber, S&P weit unter
])
def test_contribution_tilt_never_rewards_an_overweight_sleeve(monkeypatch, values):
    total = sum(values.values())
    monkeypatch.setattr(main, "get_all_strategy_values", lambda api: {**values, "total": total})
    adj = main.calculate_rebalanced_allocations({})["adjusted_allocations"]
    assert sum(adj.values()) == pytest.approx(1.0)
    for key, (allo, _) in main.SLEEVES.items():
        target = main.strategy_allocations[allo]
        if values[key] / total > target + 1e-9:
            assert adj[allo] <= target + 1e-9, f"{key} ist uebergewichtet und bekommt {adj[allo]:.3f}"
        assert adj[allo] >= 0.5 * target - 1e-9          # Floor
    # Der am staerksten untergewichtete Sleeve muss mindestens sein Ziel bekommen,
    # sonst kann er nie aufholen (so war es bei Kappe = Ziel fuer Mix8).
    gaps = {key: main.strategy_allocations[allo] - values[key] / total for key, (allo, _) in main.SLEEVES.items()}
    worst = max(gaps, key=gaps.get)
    if gaps[worst] > 1e-9:
        allo = main.SLEEVES[worst][0]
        assert adj[allo] >= main.strategy_allocations[allo] - 1e-9, \
            f"{worst} ist am weitesten unter Ziel und bekommt nur {adj[allo]:.3f}"


def test_tilt_order_follows_the_gap_to_target(monkeypatch):
    """AAA 30 % (ueber), World 20 % (knapp unter), Mix8 35 % (weit unter), S&P im Ziel.
    Bei Kappe = Ziel bekam der am weitesten untergewichtete Sleeve weniger als sein Ziel."""
    vals = {"aaa": 3000.0, "world_trend": 2000.0, "mix8": 3500.0, "spx_trend": 1500.0}
    monkeypatch.setattr(main, "get_all_strategy_values", lambda api: {**vals, "total": 10000.0})
    adj = main.calculate_rebalanced_allocations({})["adjusted_allocations"]
    assert adj["aaa_allo"] == pytest.approx(0.5 * 0.2125)       # nur der Floor
    assert adj["mix8_allo"] > adj["world_trend_allo"] > adj["aaa_allo"]
    assert adj["mix8_allo"] >= 0.425


# ── Build ────────────────────────────────────────────────────────────────────
def test_cloudbuild_deploys_and_schedules_the_new_sleeves():
    cb = (ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    assert "--entry-point=monthly_buy_mix8" in cb
    assert "--entry-point=daily_trend_sleeves_route" in cb
    block = cb[cb.index("jobs describe daily_trend_sleeves"):]
    block = block[:block.index("waitFor")]
    assert "jobs create http daily_trend_sleeves" in block      # neuer Job: create-Zweig
    assert "--schedule='50 15 * * 1-5'" in block and "America/New_York" in block
    assert "id: 'deploy-dm'" not in cb
    assert "gcloud functions delete monthly_dual_momentum" in cb
    assert "gcloud scheduler jobs delete daily_world_trend" in cb
    assert "gcloud functions delete daily_world_trend" in cb


# ── S&P-Trend und gemeinsamer Tagesjob ───────────────────────────────────────
def test_daily_trend_sleeves_isolates_a_failing_sleeve(monkeypatch):
    seen = []

    def fake(api, cfg, env="live", force=False, skip_order_wait=False):
        seen.append(cfg["strategy_key"])
        if cfg["strategy_key"] == "world_trend":
            raise main.EodhdDataError("URTH.US: kaputt")
        return f"{cfg['display_name']}: no change"
    monkeypatch.setattr(main, "daily_trend_sleeve", fake)
    res = main.daily_trend_sleeves({}, env="paper")
    assert seen == ["world_trend", "spx_trend"]                 # S&P laeuft trotzdem
    assert res.startswith("❌") and "S&P-Trend 3x: no change" in res


@pytest.mark.parametrize("on,expect", [(True, "SPXL"), (False, "BIL")])
def test_spx_contribution_follows_the_signal(monkeypatch, on, expect):
    bought = []
    monkeypatch.setattr(main, "trend_signals", lambda cfg, today_iso=None: {
        "SPXL": {"on": on, "price": 1.0, "sma": 1.0, "diff_pct": 0.0, "up": 0, "dn": 0}})
    monkeypatch.setattr(main, "get_latest_trade", lambda api, s: {"SPXL": 290.0, "BIL": 91.5}[s])
    monkeypatch.setattr(main, "list_positions", lambda api: [])
    monkeypatch.setattr(main, "submit_order", lambda api, s, q, side: bought.append((s, q)) or {"id": "x"})
    monkeypatch.setattr(main, "wait_for_order_fill", lambda api, oid: None)
    monkeypatch.setattr(main, "load_balances", lambda env="live": {})
    monkeypatch.setattr(main, "save_balance", lambda k, d, env="live", merge=False: None)
    calc = {"strategy_amounts": {"spx_trend_allo": 75.0}, "total_available": 500.0, "margin_approved": 0.0}
    margin = {"target_margin": 0, "metrics": {"leverage": 1.0}}
    main.make_monthly_buys_spx_trend({}, force_execute=True, investment_calc=calc, margin_result=margin, env="paper")
    assert [s for s, _ in bought] == [expect]

