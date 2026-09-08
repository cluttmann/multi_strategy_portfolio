"""Unit-Tests fuer den EODHD-Zweig des Index-Alerts.

Lauf:  python3 -m pytest tests/test_eodhd_index_alert.py -v
"""
import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main


TODAY = "2026-09-07"


def _hist(n, start=100.0, step=0.1, end_date=TODAY, include_today=False):
    """n aufsteigende Bars, die auf `end_date` enden (Kalendertage, ohne Feiertage)."""
    last = datetime.date.fromisoformat(end_date)
    if not include_today:
        last -= datetime.timedelta(days=1)
    return [((last - datetime.timedelta(days=n - 1 - i)).isoformat(),
             start + i * step) for i in range(n)]


# ── _eodhd_sma_series ──────────────────────────────────────────────────────

def test_sma_series_hat_genau_period_werte_und_endet_auf_live():
    hist = _hist(300)
    live = hist[-1][1] * 1.01          # plausibler Tagesschritt
    series = main._eodhd_sma_series(hist, live, 255, TODAY)
    assert len(series) == 255
    assert series[-1] == live


def test_sma_series_verwirft_heutigen_bar():
    """Ein heute schon geschlossener Bar darf den Tag nicht doppelt einbringen."""
    hist = _hist(300, include_today=True)
    live = hist[-1][1] * 1.01
    series = main._eodhd_sma_series(hist, live, 255, TODAY)
    assert len(series) == 255
    assert series[-1] == live
    assert series[-2] != hist[-1][1]   # der heutige Bar ist nicht im Fenster


def test_sma_series_wirft_bei_zu_wenig_historie():
    with pytest.raises(main.EodhdDataError, match="historische Schlusskurse"):
        main._eodhd_sma_series(_hist(50), 100.0, 255, TODAY)


def test_sma_series_wirft_bei_veralteter_historie():
    hist = _hist(300, end_date="2026-08-01")
    with pytest.raises(main.EodhdDataError, match="Kalendertage alt"):
        main._eodhd_sma_series(hist, 130.0, 255, TODAY)


def test_sma_series_wirft_bei_implausiblem_sprung():
    hist = _hist(300)
    with pytest.raises(main.EodhdDataError, match="weicht"):
        main._eodhd_sma_series(hist, hist[-1][1] * 1.5, 255, TODAY)


def test_sma_series_akzeptiert_sprung_knapp_unter_der_grenze():
    hist = _hist(300)
    series = main._eodhd_sma_series(hist, hist[-1][1] * 1.07, 255, TODAY)
    assert len(series) == 255


# ── _eodhd_close_series (reconcile) ────────────────────────────────────────

def test_close_series_nimmt_heutigen_schluss_mit():
    hist = _hist(300, include_today=True)
    series = main._eodhd_close_series(hist, 255, TODAY)
    assert len(series) == 255
    assert series[-1] == hist[-1][1]


def test_close_series_wirft_wenn_heutiger_schluss_fehlt():
    with pytest.raises(main.EodhdDataError, match="kein Schlusskurs"):
        main._eodhd_close_series(_hist(300), 255, TODAY)


# ── _sma_state_from_diff ───────────────────────────────────────────────────

@pytest.mark.parametrize("diff,erwartet", [
    (10.5, "above"), (1.01, "above"),
    (0.99, "neutral"), (0.0, "neutral"), (-0.99, "neutral"),
    (-1.01, "below"), (-9.6, "below"),
])
def test_state_klassifikation(diff, erwartet):
    assert main._sma_state_from_diff(diff, 1.0) == erwartet


# ── Fetcher (mit gefaelschten HTTP-Antworten) ──────────────────────────────

_KEIN_JSON = object()


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if self._payload is _KEIN_JSON:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(main, "get_secret_or_env", lambda *a, **k: "testtoken")


def test_eod_series_parst_und_sortiert(monkeypatch, token):
    payload = [
        {"date": "2026-09-02", "close": 106.0, "adjusted_close": 106.0, "volume": 1},
        {"date": "2026-09-01", "close": 105.0, "adjusted_close": 105.0, "volume": 1},
    ]
    monkeypatch.setattr(main.requests, "get", lambda *a, **k: _FakeResponse(payload))
    assert main.fetch_eodhd_eod_series("IUSQ.XETRA") == [
        ("2026-09-01", 105.0), ("2026-09-02", 106.0)]


def test_eod_series_wirft_bei_http_fehler(monkeypatch, token):
    monkeypatch.setattr(main.requests, "get", lambda *a, **k: _FakeResponse([], 403))
    with pytest.raises(main.EodhdDataError, match="HTTP 403"):
        main.fetch_eodhd_eod_series("IUSQ.XETRA")


def test_eod_series_wirft_bei_leerer_antwort(monkeypatch, token):
    monkeypatch.setattr(main.requests, "get", lambda *a, **k: _FakeResponse([]))
    with pytest.raises(main.EodhdDataError, match="keine Bars"):
        main.fetch_eodhd_eod_series("QUATSCH.XETRA")


def test_eod_series_wirft_ohne_token(monkeypatch):
    monkeypatch.setattr(main, "get_secret_or_env", lambda *a, **k: None)
    with pytest.raises(main.EodhdDataError, match="EODHD_TOKEN"):
        main.fetch_eodhd_eod_series("IUSQ.XETRA")


def test_realtime_liefert_kurs_und_zeitstempel(monkeypatch, token):
    payload = {"code": "IUSQ.XETRA", "close": 107.48, "timestamp": 1788795360}
    monkeypatch.setattr(main.requests, "get", lambda *a, **k: _FakeResponse(payload))
    kurs, ts = main.fetch_eodhd_realtime("IUSQ.XETRA")
    assert kurs == 107.48
    assert ts == datetime.datetime.utcfromtimestamp(1788795360)


def test_realtime_wirft_bei_NA(monkeypatch, token):
    """EODHD antwortet fuer unbekannte Ticker mit HTTP 200 und 'NA' -
    geprueft an SWRD.XETRA und IWDA.XETRA am 2026-09-07."""
    payload = {"code": "SWRD.XETRA", "close": "NA", "timestamp": "NA"}
    monkeypatch.setattr(main.requests, "get", lambda *a, **k: _FakeResponse(payload))
    with pytest.raises(main.EodhdDataError, match="NA"):
        main.fetch_eodhd_realtime("SWRD.XETRA")


# ── State-Persistenz ───────────────────────────────────────────────────────

class _FakeDoc:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class _FakeDocRef:
    def __init__(self, store, key):
        self._store, self._key = store, key

    def get(self):
        return _FakeDoc(self._store.get(self._key))

    def set(self, data, merge=False):
        if merge and self._key in self._store:
            self._store[self._key].update(data)
        else:
            self._store[self._key] = dict(data)


class _FakeCollection:
    def __init__(self, store, name):
        self._store, self._name = store, name

    def document(self, doc_id):
        return _FakeDocRef(self._store, f"{self._name}/{doc_id}")


class _FakeFirestore:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return _FakeCollection(self.store, name)


@pytest.fixture
def firestore(monkeypatch):
    fake = _FakeFirestore()
    monkeypatch.setattr(main, "get_firestore_client", lambda: fake)
    return fake


def test_state_ist_anfangs_none(firestore):
    assert main.get_eodhd_sma_state("IUSQ.XETRA", 255, env="paper") is None


def test_state_wird_angelegt_wenn_dokument_fehlt(firestore):
    """Der eigentliche Grund fuer diese Helper: save_index_sma_state steigt aus,
    wenn das Dokument nicht existiert - fuer EODHD-Symbole legt
    update_market_data() aber nie eines an."""
    main.save_eodhd_sma_state("IUSQ.XETRA", 255, "above", 107.48, 97.27, env="paper")
    assert main.get_eodhd_sma_state("IUSQ.XETRA", 255, env="paper") == "above"


def test_state_ueberschreibt_sich(firestore):
    main.save_eodhd_sma_state("IUSQ.XETRA", 255, "above", 107.0, 97.0, env="paper")
    main.save_eodhd_sma_state("IUSQ.XETRA", 255, "below", 96.0, 97.0, env="paper")
    assert main.get_eodhd_sma_state("IUSQ.XETRA", 255, env="paper") == "below"


def test_zwei_symbole_kollidieren_nicht(firestore):
    main.save_eodhd_sma_state("IUSQ.XETRA", 255, "above", 107.0, 97.0, env="paper")
    main.save_eodhd_sma_state("EUNL.XETRA", 250, "below", 127.0, 116.0, env="paper")
    assert main.get_eodhd_sma_state("IUSQ.XETRA", 255, env="paper") == "above"
    assert main.get_eodhd_sma_state("EUNL.XETRA", 250, env="paper") == "below"


def test_zwei_fenster_auf_einem_symbol_kollidieren_nicht(firestore):
    main.save_eodhd_sma_state("IUSQ.XETRA", 255, "above", 107.0, 97.0, env="paper")
    main.save_eodhd_sma_state("IUSQ.XETRA", 160, "below", 107.0, 109.0, env="paper")
    assert main.get_eodhd_sma_state("IUSQ.XETRA", 255, env="paper") == "above"
    assert main.get_eodhd_sma_state("IUSQ.XETRA", 160, env="paper") == "below"


def test_state_liegt_nicht_im_alpaca_cache(firestore):
    """Der Preis-Cache von Alpaca darf nicht mit Symbolen verschmutzt werden,
    die update_market_data() nie auffrischen kann."""
    main.save_eodhd_sma_state("IUSQ.XETRA", 255, "above", 107.0, 97.0, env="paper")
    assert all(not k.startswith("market-data-") for k in firestore.store)
    assert "index-alert-state-paper/IUSQ_XETRA" in firestore.store


# ── Handler ────────────────────────────────────────────────────────────────

@pytest.fixture
def handler_umgebung(monkeypatch, firestore):
    """Historie mit konstant 100.0, damit die SMA exakt vorhersagbar ist."""
    gesendet = []
    basis = datetime.date.fromisoformat(TODAY)
    hist = [((basis - datetime.timedelta(days=300 - i)).isoformat(), 100.0)
            for i in range(300)]
    monkeypatch.setattr(main, "fetch_eodhd_eod_series", lambda *a, **k: list(hist))
    monkeypatch.setattr(main, "send_telegram_message", lambda m: gesendet.append(m))
    monkeypatch.setattr(main, "_heute_iso", lambda: TODAY)
    return {"gesendet": gesendet, "hist": hist, "firestore": firestore}


def _setze_live(monkeypatch, kurs, tag=TODAY):
    ts = datetime.datetime.fromisoformat(f"{tag}T15:36:00")
    monkeypatch.setattr(main, "fetch_eodhd_realtime", lambda *a, **k: (kurs, ts))


def test_decisive_initialisiert_und_meldet(monkeypatch, handler_umgebung):
    _setze_live(monkeypatch, 100.0)
    with main.app.app_context():
        resp, code = main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "decisive", "paper")
    assert code == 200
    assert resp.get_json()["status"] == "initialised"
    assert len(handler_umgebung["gesendet"]) == 1
    assert main.get_eodhd_sma_state("IUSQ.XETRA", 255, env="paper") == "neutral"


def test_decisive_meldet_crossover_und_schreibt_state(monkeypatch, handler_umgebung):
    main.save_eodhd_sma_state("IUSQ.XETRA", 255, "above", 110.0, 100.0, env="paper")
    _setze_live(monkeypatch, 94.0)          # -6 % => below
    with main.app.app_context():
        resp, _ = main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "decisive", "paper")
    assert resp.get_json()["status"] == "crossover_below"
    assert "€" in handler_umgebung["gesendet"][0]
    assert "$" not in handler_umgebung["gesendet"][0]
    assert main.get_eodhd_sma_state("IUSQ.XETRA", 255, env="paper") == "below"


def test_decisive_schweigt_ohne_zustandswechsel(monkeypatch, handler_umgebung):
    main.save_eodhd_sma_state("IUSQ.XETRA", 255, "above", 110.0, 100.0, env="paper")
    _setze_live(monkeypatch, 106.0)         # bleibt above
    with main.app.app_context():
        resp, _ = main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "decisive", "paper")
    assert handler_umgebung["gesendet"] == []
    assert resp.get_json()["status"] == "above_no_change"


def test_decisive_wirft_bei_kurs_von_gestern(monkeypatch, handler_umgebung):
    _setze_live(monkeypatch, 100.0, tag="2026-09-04")
    with pytest.raises(main.EodhdDataError, match="nicht von heute"):
        main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "decisive", "paper")


def test_advisory_schreibt_keinen_state(monkeypatch, handler_umgebung):
    main.save_eodhd_sma_state("IUSQ.XETRA", 255, "above", 110.0, 100.0, env="paper")
    _setze_live(monkeypatch, 94.0)          # wuerde below ergeben
    with main.app.app_context():
        main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "advisory", "paper")
    assert main.get_eodhd_sma_state("IUSQ.XETRA", 255, env="paper") == "above"
    assert len(handler_umgebung["gesendet"]) == 1


def test_advisory_schweigt_weit_ausserhalb_des_bands(monkeypatch, handler_umgebung):
    main.save_eodhd_sma_state("IUSQ.XETRA", 255, "above", 110.0, 100.0, env="paper")
    _setze_live(monkeypatch, 106.0)
    with main.app.app_context():
        main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "advisory", "paper")
    assert handler_umgebung["gesendet"] == []


def test_advisory_warnt_im_band(monkeypatch, handler_umgebung):
    main.save_eodhd_sma_state("IUSQ.XETRA", 255, "above", 110.0, 100.0, env="paper")
    _setze_live(monkeypatch, 100.4)         # +0,4 % => neutral, also im Band
    with main.app.app_context():
        main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "advisory", "paper")
    assert "Vorwarnung" in handler_umgebung["gesendet"][0]


def test_reconcile_korrigiert_und_meldet(monkeypatch, handler_umgebung):
    """Der 17:20-Lauf hat 'above' gespeichert, der echte Schluss ergibt 'neutral'."""
    hist = handler_umgebung["hist"] + [(TODAY, 100.0)]
    monkeypatch.setattr(main, "fetch_eodhd_eod_series", lambda *a, **k: list(hist))
    main.save_eodhd_sma_state("IUSQ.XETRA", 255, "above", 110.0, 100.0, env="paper")
    with main.app.app_context():
        resp, _ = main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "reconcile", "paper")
    assert resp.get_json()["status"] == "reconciled"
    assert "Korrektur" in handler_umgebung["gesendet"][0]
    assert main.get_eodhd_sma_state("IUSQ.XETRA", 255, env="paper") == "neutral"


def test_unbekannte_rolle_wirft(monkeypatch, handler_umgebung):
    _setze_live(monkeypatch, 100.0)
    with pytest.raises(main.EodhdDataError, match="run_role"):
        main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "quatsch", "paper")


# ── XETRA-Feiertage vs. echte Datenprobleme ────────────────────────────────

def test_xetra_kalender_kennt_handelstage():
    assert main._xetra_trading_day("2026-09-08") is True     # Dienstag
    assert main._xetra_trading_day("2026-09-05") is False    # Samstag
    assert main._xetra_trading_day("2026-01-01") is False    # Neujahr


def test_decisive_am_feiertag_schweigt(monkeypatch, handler_umgebung):
    """Veralteter Kurs an einem Nicht-Handelstag: stiller, korrekter Nichtlauf."""
    monkeypatch.setattr(main, "_xetra_trading_day", lambda d: False)
    _setze_live(monkeypatch, 100.0, tag="2026-09-04")
    with main.app.app_context():
        resp, code = main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "decisive", "paper")
    assert code == 200
    assert resp.get_json()["status"] == "no_trading_day"
    assert handler_umgebung["gesendet"] == []
    assert main.get_eodhd_sma_state("IUSQ.XETRA", 255, env="paper") is None


def test_decisive_am_handelstag_wirft_bei_altem_kurs(monkeypatch, handler_umgebung):
    """Derselbe veraltete Kurs an einem Handelstag: echtes Datenproblem."""
    monkeypatch.setattr(main, "_xetra_trading_day", lambda d: True)
    _setze_live(monkeypatch, 100.0, tag="2026-09-04")
    with pytest.raises(main.EodhdDataError, match="XETRA-Handelstag"):
        main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "decisive", "paper")


def test_advisory_am_feiertag_schweigt(monkeypatch, handler_umgebung):
    monkeypatch.setattr(main, "_xetra_trading_day", lambda d: False)
    _setze_live(monkeypatch, 100.4, tag="2026-09-04")   # laege im Band
    with main.app.app_context():
        resp, _ = main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "advisory", "paper")
    assert resp.get_json()["status"] == "no_trading_day"
    assert handler_umgebung["gesendet"] == []


def test_reconcile_am_feiertag_schweigt(monkeypatch, handler_umgebung):
    monkeypatch.setattr(main, "_xetra_trading_day", lambda d: False)
    with main.app.app_context():
        resp, _ = main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "reconcile", "paper")
    assert resp.get_json()["status"] == "no_trading_day"
    assert handler_umgebung["gesendet"] == []


def test_reconcile_am_handelstag_wirft_ohne_schluss(monkeypatch, handler_umgebung):
    monkeypatch.setattr(main, "_xetra_trading_day", lambda d: True)
    with pytest.raises(main.EodhdDataError, match="XETRA-Handelstag"):
        main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "reconcile", "paper")


def test_kaputter_kalender_nimmt_handelstag_an(monkeypatch, handler_umgebung):
    """Der Kalender darf nie ein echtes Signal verschlucken: faellt er aus,
    wird ein Datenproblem laut statt still."""
    def kaputt(*a, **k):
        raise RuntimeError("Kalender weg")
    monkeypatch.setattr(main.mcal, "get_calendar", kaputt)
    assert main._xetra_trading_day("2026-09-08") is True
