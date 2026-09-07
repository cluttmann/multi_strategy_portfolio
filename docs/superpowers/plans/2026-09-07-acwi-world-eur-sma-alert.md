# EUR-SMA-Alert für ACWI und World — Implementierungsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ein SMA-Crossing-Alert auf MSCI ACWI (255d) und MSCI World (250d), gerechnet auf EUR-notierten XETRA-Schlusskursen aus EODHD, der `urth_255sma_alert` ersetzt.

**Architecture:** Neuer `source="eodhd"`-Zweig in `check_unified_index_alert`, ausgelagert in eine eigene Handler-Funktion. Der bestehende Alpaca-Pfad bleibt Zeile für Zeile unangetastet. Signal und Ausführung liegen auf derselben Zeitscheibe (17:30 XETRA), es wird nichts geschätzt. Drei Lauf-Rollen (`advisory` / `decisive` / `reconcile`) ersetzen die US-Handelszeiten-Logik des Alpaca-Pfads.

**Tech Stack:** Python 3.10, `requests`, Flask, Firestore, Google Cloud Functions, Cloud Scheduler, EODHD REST API. Tests mit pytest (nur lokal, siehe Constraints).

**Spec:** [2026-09-07-acwi-world-eur-sma-alert-design.md](../specs/2026-09-07-acwi-world-eur-sma-alert-design.md)

## Global Constraints

- **Keine neue Runtime-Abhängigkeit.** Nur `requests`, steht bereits in `requirements.txt`.
- **pytest gehört NICHT in `requirements.txt`.** `cloudbuild.yaml` kopiert `requirements.txt` in jedes Function-Image (`fn-light`). Jede Zeile dort landet in 15 Images. Test-Abhängigkeiten kommen in ein neues `requirements-dev.txt`, das nie deployt wird.
- **Der Alpaca-Pfad darf sich nicht verhalten anders.** `sp500_drop_alert`, `msci_drop_alert`, `spy_200sma_alert` laufen weiter über `source="alpaca"` (Default) und müssen bitgleich funktionieren.
- **Fenster:** `IUSQ.XETRA` = 255 Tage, `EUNL.XETRA` = 250 Tage. Nicht vertauschen.
- **Band:** `noise_threshold = 1.0` (Prozent). Entspricht `BAND = 0.01` in `research/sma_sweep_acwi.py:37`.
- **Zeitzone aller neuen Scheduler:** `Europe/Berlin`. Die bestehenden vier laufen in `America/New_York` — das bleibt so.
- **Kein `git push` auf `main`.** Ein Push auf `main` triggert den Cloud Build für alle Functions. Gearbeitet wird auf `feature/acwi-world-eur-sma-alert`.
- **Commit-Messages ohne AI-Co-Author-Trailer** (globale Vorgabe in `~/.claude/CLAUDE.md`), deutschsprachig im Stil des Repos.
- **Fehler sind laut.** Kein Fallback, kein Default, keine zweite Quelle. Jeder Datenfehler → Telegram mit `❗` + HTTP 500 + kein State-Schreiben.

---

## File Structure

| Datei | Verantwortung | Aktion |
|---|---|---|
| `main.py` | EODHD-Fetcher, reine Signalfunktionen, State-Helper, Handler, CLI | Modify |
| `tests/test_eodhd_index_alert.py` | Unit-Tests der reinen Funktionen + Handler mit gefälschten Daten | Create |
| `requirements-dev.txt` | pytest — **nie** deployt | Create |
| `cloudbuild.yaml` | 6 neue Scheduler in Wave D, `urth_255sma_alert` retiren | Modify |

Alles in `main.py` folgt der Konvention des Repos (eine Datei, keine lokalen Modul-Importe — `fn-light` kopiert nur `main.py` + `requirements.txt`). Die neuen Funktionen werden direkt **vor** `check_unified_index_alert` (aktuell [main.py:3239](../../../main.py#L3239)) eingefügt.

---

### Task 1: Reine Signalfunktionen und Plausibilitätswächter

Keine Netzwerkzugriffe, keine Firestore-Zugriffe — deshalb vollständig unit-testbar.

**Files:**
- Create: `requirements-dev.txt`
- Create: `tests/test_eodhd_index_alert.py`
- Modify: `main.py` (Einfügen direkt vor `def check_unified_index_alert`, aktuell Zeile 3239)

**Interfaces:**
- Consumes: nichts
- Produces:
  - `class EodhdDataError(Exception)`
  - `_eodhd_sma_series(hist, live_price, period, today_iso) -> list[float]`
  - `_eodhd_close_series(hist, period, today_iso) -> list[float]`
  - `_sma_state_from_diff(diff_percent, noise_threshold) -> str`
  - Konstanten `MAX_EOD_GAP_DAYS = 10`, `MAX_LIVE_JUMP = 0.08`
  - `hist` hat überall das Format `list[tuple[str, float]]`, aufsteigend nach ISO-Datum.

- [ ] **Step 1: `requirements-dev.txt` anlegen**

```
# Nur lokal. NIEMALS nach requirements.txt uebernehmen - cloudbuild.yaml
# kopiert requirements.txt in jedes fn-light-Image (15 Functions).
pytest>=8.0
```

- [ ] **Step 2: Den fehlschlagenden Test schreiben**

Datei `tests/test_eodhd_index_alert.py`:

```python
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
    series = main._eodhd_sma_series(hist, 175.0, 255, TODAY)
    assert len(series) == 255
    assert series[-1] == 175.0


def test_sma_series_verwirft_heutigen_bar():
    """Ein heute schon geschlossener Bar darf den Tag nicht doppelt einbringen."""
    hist = _hist(300, include_today=True)
    series = main._eodhd_sma_series(hist, 175.0, 255, TODAY)
    assert len(series) == 255
    assert series[-1] == 175.0
    assert series[-2] != hist[-1][1]   # der heutige Bar ist nicht drin


def test_sma_series_wirft_bei_zu_wenig_historie():
    with pytest.raises(main.EodhdDataError, match="historische Schlusskurse"):
        main._eodhd_sma_series(_hist(50), 100.0, 255, TODAY)


def test_sma_series_wirft_bei_veralteter_historie():
    hist = _hist(300, end_date="2026-08-01")
    with pytest.raises(main.EodhdDataError, match="Kalendertage alt"):
        main._eodhd_sma_series(hist, 130.0, 255, TODAY)


def test_sma_series_wirft_bei_implausiblem_sprung():
    hist = _hist(300)
    letzter = hist[-1][1]
    with pytest.raises(main.EodhdDataError, match="weicht"):
        main._eodhd_sma_series(hist, letzter * 1.5, 255, TODAY)


def test_sma_series_akzeptiert_sprung_knapp_unter_der_grenze():
    hist = _hist(300)
    letzter = hist[-1][1]
    series = main._eodhd_sma_series(hist, letzter * 1.07, 255, TODAY)
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
```

- [ ] **Step 3: Tests laufen lassen, Fehlschlag bestätigen**

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests/test_eodhd_index_alert.py -v
```

Erwartet: alle FAIL mit `AttributeError: module 'main' has no attribute 'EodhdDataError'`.

- [ ] **Step 4: Implementieren**

In `main.py` direkt vor `def check_unified_index_alert(request, env=None):` einfügen:

```python
# ─────────────────────────────────────────────────────────────────────────────
# EODHD-Zweig des Index-Alerts (EUR-notierte XETRA-Tracker)
#
# Warum eine eigene Quelle statt Alpaca: der Alert entscheidet ueber einen Trade,
# der um 17:30 CET an der XETRA in EUR ausgefuehrt wird. Alpaca fuehrt keine
# EUR-Variante des ACWI, liefert Bars nur mit adjustment="split" (also ohne
# Dividenden, ~1,8 %/Jahr Versatz gegen eine Net-TR-Studie) und quotet diese
# Ticker im IEX-Feed unbrauchbar breit. Details: docs/superpowers/specs/
# 2026-09-07-acwi-world-eur-sma-alert-design.md
# ─────────────────────────────────────────────────────────────────────────────

EODHD_BASE_URL = "https://eodhd.com/api"
MAX_EOD_GAP_DAYS = 10      # beobachtete Maximalluecke bei IUSQ.XETRA: 6 (Weihnachten)
MAX_LIVE_JUMP = 0.08       # groesserer Sprung => Tickerwechsel, Split oder Fehlprint


class EodhdDataError(Exception):
    """EODHD-Daten fehlen, sind veraltet oder unplausibel.

    Wird bewusst NIE in einen Default abgefangen. Ein Alert, der bei
    Datenausfall stillschweigend 'alles in Ordnung' meldet, ist schlimmer als
    keiner - dieselbe Regel wie fuer die Margin-Gates in CLAUDE.md.
    """


def _sma_state_from_diff(diff_percent, noise_threshold):
    """Zustand aus dem prozentualen Abstand zur SMA, mit Totband."""
    if diff_percent > noise_threshold:
        return "above"
    if diff_percent < -noise_threshold:
        return "below"
    return "neutral"


def _eodhd_sma_series(hist, live_price, period, today_iso):
    """SMA-Fenster fuer advisory/decisive: (period-1) Schlusskurse + Live-Kurs.

    `hist` ist [(datum_iso, close)] aufsteigend und darf den heutigen Bar
    enthalten - der wird verworfen, damit der Tag nicht doppelt im Fenster
    steht (einmal als Schluss, einmal als Live-Kurs).
    """
    past = [(d, c) for d, c in hist if d < today_iso]
    if len(past) < period - 1:
        raise EodhdDataError(
            f"nur {len(past)} historische Schlusskurse vor {today_iso}, "
            f"benoetigt {period - 1}")

    newest_date, newest_close = past[-1]
    gap = (datetime.date.fromisoformat(today_iso)
           - datetime.date.fromisoformat(newest_date)).days
    if gap > MAX_EOD_GAP_DAYS:
        raise EodhdDataError(
            f"neuester Schlusskurs ist {gap} Kalendertage alt ({newest_date})")

    if abs(live_price / newest_close - 1) > MAX_LIVE_JUMP:
        raise EodhdDataError(
            f"Live-Kurs {live_price:.4f} weicht "
            f"{(live_price / newest_close - 1) * 100:+.2f} % vom letzten "
            f"Schluss {newest_close:.4f} ({newest_date}) ab")

    series = [c for _, c in past[-(period - 1):]] + [live_price]
    if len(series) != period:
        raise EodhdDataError(f"Fenster hat {len(series)} statt {period} Werte")
    return series


def _eodhd_close_series(hist, period, today_iso):
    """SMA-Fenster fuer reconcile: `period` echte Schlusskurse inkl. heute."""
    if not hist or hist[-1][0] != today_iso:
        neuester = hist[-1][0] if hist else "keiner"
        raise EodhdDataError(
            f"kein Schlusskurs fuer {today_iso} vorhanden (neuester: {neuester})")
    if len(hist) < period:
        raise EodhdDataError(
            f"nur {len(hist)} Schlusskurse, benoetigt {period}")
    return [c for _, c in hist[-period:]]
```

- [ ] **Step 5: Tests laufen lassen, Erfolg bestätigen**

```bash
python3 -m pytest tests/test_eodhd_index_alert.py -v
```

Erwartet: alle PASS (13 Tests).

- [ ] **Step 6: Committen**

```bash
git add requirements-dev.txt tests/test_eodhd_index_alert.py main.py
git commit -m "alert: reine Signalfunktionen und Plausibilitaetswaechter fuer den EODHD-Zweig

Vier Waechter, weil ein stiller falscher Kurs der gefaehrlichste Fehlermodus
ist - er erzeugt ein Signal, das echt aussieht: zu kurze Historie, veraltete
Historie, implausibler Sprung gegen den letzten Schluss, falsche Fensterlaenge.
Alle werfen EodhdDataError statt einen Default zu liefern.

pytest bewusst in requirements-dev.txt statt requirements.txt - letztere wird
von cloudbuild.yaml in jedes fn-light-Image kopiert."
```

---

### Task 2: EODHD-Fetcher

**Files:**
- Modify: `main.py` (direkt hinter `_eodhd_close_series` aus Task 1)
- Modify: `tests/test_eodhd_index_alert.py` (Tests anhängen)

**Interfaces:**
- Consumes: `EodhdDataError` aus Task 1; `get_secret_or_env` aus [main.py:592](../../../main.py#L592)
- Produces:
  - `fetch_eodhd_eod_series(symbol, calendar_days=600) -> list[tuple[str, float]]`
  - `fetch_eodhd_realtime(symbol) -> tuple[float, datetime.datetime]`

- [ ] **Step 1: Den fehlschlagenden Test schreiben**

An `tests/test_eodhd_index_alert.py` anhängen:

```python
# ── Fetcher (mit gefaelschten HTTP-Antworten) ──────────────────────────────

class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if self._payload is _KEIN_JSON:
            raise ValueError("no json")
        return self._payload


_KEIN_JSON = object()


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(main, "get_secret_or_env", lambda *a, **k: "testtoken")


def test_eod_series_parst_und_sortiert(monkeypatch, token):
    payload = [
        {"date": "2026-09-02", "close": 106.0, "adjusted_close": 106.0, "volume": 1},
        {"date": "2026-09-01", "close": 105.0, "adjusted_close": 105.0, "volume": 1},
    ]
    monkeypatch.setattr(main.requests, "get", lambda *a, **k: _FakeResponse(payload))
    out = main.fetch_eodhd_eod_series("IUSQ.XETRA")
    assert out == [("2026-09-01", 105.0), ("2026-09-02", 106.0)]


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
```

- [ ] **Step 2: Tests laufen lassen, Fehlschlag bestätigen**

```bash
python3 -m pytest tests/test_eodhd_index_alert.py -v -k "eod_series or realtime"
```

Erwartet: FAIL mit `AttributeError: module 'main' has no attribute 'fetch_eodhd_eod_series'`.

- [ ] **Step 3: Implementieren**

Direkt hinter `_eodhd_close_series` in `main.py`:

```python
def _eodhd_token():
    token = get_secret_or_env("EODHD_TOKEN")
    if not token:
        raise EodhdDataError(
            "EODHD_TOKEN nicht gefunden (weder Secret Manager noch .env)")
    return token


def fetch_eodhd_eod_series(symbol, calendar_days=600):
    """Taegliche Schlusskurse von EODHD als [(datum_iso, close)] aufsteigend.

    Nutzt `adjusted_close`; die verwendeten Tracker sind thesaurierend, die
    Reihe ist also Total Return per Konstruktion.
    """
    start = (datetime.date.today()
             - datetime.timedelta(days=calendar_days)).isoformat()
    response = requests.get(
        f"{EODHD_BASE_URL}/eod/{symbol}",
        params={"api_token": _eodhd_token(), "fmt": "json",
                "period": "d", "from": start},
        timeout=30)

    if response.status_code != 200:
        raise EodhdDataError(
            f"EOD-Abruf fuer {symbol}: HTTP {response.status_code}")
    try:
        rows = response.json()
    except ValueError:
        raise EodhdDataError(f"EOD-Abruf fuer {symbol}: Antwort ist kein JSON")
    if not isinstance(rows, list) or not rows:
        raise EodhdDataError(f"EOD-Abruf fuer {symbol}: keine Bars geliefert")

    series = []
    for row in rows:
        close = row.get("adjusted_close", row.get("close"))
        if close in (None, "NA") or not row.get("date"):
            continue
        series.append((row["date"], float(close)))
    if not series:
        raise EodhdDataError(
            f"EOD-Abruf fuer {symbol}: keine verwertbaren Schlusskurse")
    return sorted(series)


def fetch_eodhd_realtime(symbol):
    """Aktueller Kurs von EODHD als (close, zeitstempel_utc)."""
    response = requests.get(
        f"{EODHD_BASE_URL}/real-time/{symbol}",
        params={"api_token": _eodhd_token(), "fmt": "json"},
        timeout=20)

    if response.status_code != 200:
        raise EodhdDataError(
            f"Live-Abruf fuer {symbol}: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise EodhdDataError(f"Live-Abruf fuer {symbol}: Antwort ist kein JSON")

    close, timestamp = data.get("close"), data.get("timestamp")
    if close in (None, "NA") or timestamp in (None, "NA"):
        raise EodhdDataError(
            f"Live-Abruf fuer {symbol}: EODHD meldet 'NA' - Ticker unbekannt "
            f"oder umbenannt?")
    return float(close), datetime.datetime.utcfromtimestamp(int(timestamp))
```

- [ ] **Step 4: Tests laufen lassen, Erfolg bestätigen**

```bash
python3 -m pytest tests/test_eodhd_index_alert.py -v
```

Erwartet: alle PASS (19 Tests).

- [ ] **Step 5: Echten Abruf gegen EODHD prüfen**

```bash
python3 -c "
import main
h = main.fetch_eodhd_eod_series('IUSQ.XETRA')
k, t = main.fetch_eodhd_realtime('IUSQ.XETRA')
print(len(h), h[0], h[-1]); print(k, t)
"
```

Erwartet: >600 Bars, ältester ~2024, Live-Kurs ~107 EUR. Wirft der Aufruf `EodhdDataError: EODHD_TOKEN`, dann fehlt das Token in `.env`.

- [ ] **Step 6: Committen**

```bash
git add main.py tests/test_eodhd_index_alert.py
git commit -m "alert: EODHD-Fetcher fuer Historie und Live-Kurs

EODHD antwortet fuer unbekannte Ticker mit HTTP 200 und 'NA' statt mit einem
Fehlerstatus - geprueft an SWRD.XETRA und IWDA.XETRA. Ohne die explizite
NA-Pruefung wuerde float('NA') als ValueError durchschlagen und der Alert
haette eine unverstaendliche Fehlermeldung statt einer nennenden."
```

---

### Task 3: Eigene State-Persistenz für den EODHD-Pfad

Der kritische Task. Ohne ihn wäre der Alert **stillschweigend komplett funktionslos**.

`save_index_sma_state` ([main.py:3075](../../../main.py#L3075)) schreibt in die Sammlung `market-data-{env}` — dieselbe, die der 5-Minuten-Preis-Cache von Alpaca nutzt — und steigt vorher aus, wenn das Dokument noch nicht existiert:

```python
if not doc.exists:
    print(f"Warning: No market data exists for {index_symbol}. Call update_market_data() first.")
    return
```

`update_market_data()` ist Alpaca-basiert und wird für `IUSQ.XETRA` nie laufen. Der State würde also nie geschrieben, `previous_state` wäre bei jedem Lauf `None`, und der Crossover-Zweig (`if previous_state and previous_state != current_state`) würde **nie** feuern. Ein Alert, der aussieht, als liefe er, und nie auslöst.

**Files:**
- Modify: `main.py` (hinter `fetch_eodhd_realtime`)
- Modify: `tests/test_eodhd_index_alert.py`

**Interfaces:**
- Consumes: `get_firestore_client`, `normalize_symbol` ([main.py:283](../../../main.py#L283))
- Produces:
  - `get_eodhd_sma_state(index_symbol, sma_period, env="live") -> str | None`
  - `save_eodhd_sma_state(index_symbol, sma_period, state, price, sma_value, env="live") -> None`
  - Sammlung `index-alert-state-{env}`, Dokument-ID `normalize_symbol(symbol)` (`IUSQ.XETRA` → `IUSQ_XETRA`), Feld `sma{period}_state`

- [ ] **Step 1: Den fehlschlagenden Test schreiben**

An `tests/test_eodhd_index_alert.py` anhängen:

```python
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
    """Der eigentliche Grund fuer diese Helper: save_index_sma_state steigt
    aus, wenn das Dokument nicht existiert - fuer EODHD-Symbole legt
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
```

- [ ] **Step 2: Tests laufen lassen, Fehlschlag bestätigen**

```bash
python3 -m pytest tests/test_eodhd_index_alert.py -v -k state
```

Erwartet: FAIL mit `AttributeError: module 'main' has no attribute 'get_eodhd_sma_state'`.

- [ ] **Step 3: Implementieren**

Hinter `fetch_eodhd_realtime` in `main.py`:

```python
def get_eodhd_sma_state(index_symbol, sma_period, env="live"):
    """Letzter bekannter Crossing-Zustand eines EODHD-Index, oder None.

    Bewusst NICHT get_index_sma_state(): das legt den Zustand als Feld im
    Alpaca-Preis-Cache (`market-data-{env}`) ab und schreibt gar nicht, wenn
    das Dokument fehlt. Fuer EODHD-Symbole legt update_market_data() nie eines
    an - der Zustand wuerde also nie persistieren, previous_state waere immer
    None und es gaebe nie einen Crossover-Alert.
    """
    try:
        doc = (get_firestore_client()
               .collection(f"index-alert-state-{env}")
               .document(normalize_symbol(index_symbol))
               .get())
        if not doc.exists:
            return None
        return doc.to_dict().get(f"sma{sma_period}_state")
    except Exception as e:
        print(f"Warning: could not load EODHD SMA state for {index_symbol}: {e}")
        return None


def save_eodhd_sma_state(index_symbol, sma_period, state, price, sma_value,
                         env="live"):
    """Crossing-Zustand schreiben. Legt das Dokument an, falls noetig.

    Faengt bewusst keine Exception ab: schlaegt das Schreiben fehl, sieht der
    naechste Lauf noch den alten Zustand und wuerde denselben Alert erneut
    senden. Das soll auffallen, nicht in einem Log versickern.
    """
    (get_firestore_client()
     .collection(f"index-alert-state-{env}")
     .document(normalize_symbol(index_symbol))
     .set({f"sma{sma_period}_state": state,
           f"sma{sma_period}_value": sma_value,
           "price": price,
           "timestamp": datetime.datetime.utcnow()}, merge=True))
```

- [ ] **Step 4: Tests laufen lassen, Erfolg bestätigen**

```bash
python3 -m pytest tests/test_eodhd_index_alert.py -v
```

Erwartet: alle PASS (25 Tests).

- [ ] **Step 5: Committen**

```bash
git add main.py tests/test_eodhd_index_alert.py
git commit -m "alert: eigene State-Persistenz fuer den EODHD-Pfad

save_index_sma_state schreibt in market-data-{env} und steigt aus, wenn das
Dokument nicht existiert - angelegt wird es nur von update_market_data(), das
Alpaca-basiert ist und IUSQ.XETRA nie anfassen wird. Der Zustand waere also
nie persistiert worden, previous_state bei jedem Lauf None, und der
Crossover-Zweig haette nie gefeuert. Ein Alert, der laeuft und nie ausloest.

Eigene Sammlung index-alert-state-{env} statt eines Fixes am alten Helper:
haelt den Alpaca-Pfad unveraendert und verschmutzt den Preis-Cache nicht mit
Symbolen, die update_market_data() nicht auffrischen kann."
```

---

### Task 4: Handler für den EODHD-Zweig

**Files:**
- Modify: `main.py` (neue Funktion vor `check_unified_index_alert`; Parameter + Weiche + Fehlerzweig in `check_unified_index_alert`)
- Modify: `tests/test_eodhd_index_alert.py`

**Interfaces:**
- Consumes: alles aus Task 1–3, `send_telegram_message` ([main.py:2978](../../../main.py#L2978))
- Produces: `_handle_eodhd_sma_crossing(index_symbol, index_name, sma_period, noise_threshold, currency_symbol, run_role, env) -> (flask.Response, int)`
- Neue Request-Parameter von `check_unified_index_alert`: `source` (`"alpaca"` default), `currency_symbol` (`"$"` default), `run_role` (`"decisive"` default)

- [ ] **Step 1: Den fehlschlagenden Test schreiben**

An `tests/test_eodhd_index_alert.py` anhängen:

```python
# ── Handler ────────────────────────────────────────────────────────────────

@pytest.fixture
def handler_umgebung(monkeypatch, firestore):
    """Historie mit konstant 100.0, damit die SMA exakt vorhersagbar ist."""
    gesendet = []
    hist = [((datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=300 - i)).isoformat(),
             100.0) for i in range(300)]
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
    _setze_live(monkeypatch, 90.0)          # -10 % => below
    with main.app.app_context():
        resp, code = main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "decisive", "paper")
    assert resp.get_json()["status"] == "crossover_below"
    assert "€" in handler_umgebung["gesendet"][0]
    assert "$" not in handler_umgebung["gesendet"][0]
    assert main.get_eodhd_sma_state("IUSQ.XETRA", 255, env="paper") == "below"


def test_decisive_schweigt_ohne_zustandswechsel(monkeypatch, handler_umgebung):
    main.save_eodhd_sma_state("IUSQ.XETRA", 255, "above", 110.0, 100.0, env="paper")
    _setze_live(monkeypatch, 110.0)         # bleibt above
    with main.app.app_context():
        resp, code = main._handle_eodhd_sma_crossing(
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
    _setze_live(monkeypatch, 90.0)          # wuerde below ergeben
    with main.app.app_context():
        main._handle_eodhd_sma_crossing(
            "IUSQ.XETRA", "ACWI", 255, 1.0, "€", "advisory", "paper")
    assert main.get_eodhd_sma_state("IUSQ.XETRA", 255, env="paper") == "above"
    assert len(handler_umgebung["gesendet"]) == 1


def test_advisory_schweigt_weit_ausserhalb_des_bands(monkeypatch, handler_umgebung):
    main.save_eodhd_sma_state("IUSQ.XETRA", 255, "above", 110.0, 100.0, env="paper")
    _setze_live(monkeypatch, 110.0)
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
```

- [ ] **Step 2: Tests laufen lassen, Fehlschlag bestätigen**

```bash
python3 -m pytest tests/test_eodhd_index_alert.py -v -k "decisive or advisory or reconcile or rolle"
```

Erwartet: FAIL mit `AttributeError: module 'main' has no attribute '_handle_eodhd_sma_crossing'`.

- [ ] **Step 3: Den Handler implementieren**

Hinter `save_eodhd_sma_state` in `main.py`:

```python
def _heute_iso():
    """Heutiges Datum als ISO-String. Eigene Funktion, damit Tests sie ersetzen koennen."""
    return datetime.date.today().isoformat()


def _handle_eodhd_sma_crossing(index_symbol, index_name, sma_period,
                               noise_threshold, currency_symbol, run_role, env):
    """SMA-Crossing-Alert auf einem EUR-notierten XETRA-Tracker.

    Drei Rollen statt der US-Handelszeiten-Logik des Alpaca-Pfads:
      advisory  - Vorwarnung 15:00-17:00, schreibt bewusst KEINEN State
      decisive  - Handelsalarm 17:20, schreibt den State
      reconcile - Abgleich 17:45 auf den echten Schluss, korrigiert den State
    """
    today_iso = _heute_iso()
    hist = fetch_eodhd_eod_series(index_symbol)

    if run_role == "reconcile":
        series = _eodhd_close_series(hist, sma_period, today_iso)
        current_price = series[-1]
        price_source = f"XETRA-Schluss {today_iso}"
    elif run_role in ("advisory", "decisive"):
        current_price, live_ts = fetch_eodhd_realtime(index_symbol)
        if run_role == "decisive" and live_ts.date().isoformat() != today_iso:
            raise EodhdDataError(
                f"Live-Kurs stammt vom {live_ts.date()}, nicht von heute "
                f"({today_iso}) - Feiertag oder haengender Feed?")
        series = _eodhd_sma_series(hist, current_price, sma_period, today_iso)
        price_source = f"live {live_ts:%H:%M} UTC"
    else:
        raise EodhdDataError(
            f"Unbekannte run_role '{run_role}' - erlaubt: advisory, decisive, reconcile")

    sma_value = sum(series) / len(series)
    diff_percent = (current_price / sma_value - 1) * 100
    current_state = _sma_state_from_diff(diff_percent, noise_threshold)
    previous_state = get_eodhd_sma_state(index_symbol, sma_period, env=env)
    trigger = sma_value * (1 - noise_threshold / 100)

    def body(headline):
        return (f"{headline}\n"
                f"Kurs: {current_price:.2f} {currency_symbol} ({price_source})\n"
                f"SMA{sma_period}: {sma_value:.2f} {currency_symbol} "
                f"({diff_percent:+.2f} %)\n"
                f"Ausstiegslinie (SMA -{noise_threshold:.1f} %): "
                f"{trigger:.2f} {currency_symbol} "
                f"({(trigger / current_price - 1) * 100:+.2f} % vom Kurs)")

    message, status = None, f"{current_state}_no_change"

    if run_role == "advisory":
        # Meldet nur, wenn es eng wird oder das Vorzeichen gegen den letzten
        # Tagesschluss kippt. Schreibt bewusst keinen State: ein am Band
        # zitternder Intraday-Kurs wuerde sonst Whipsaw-Alerts erzeugen, die
        # der Backtest nie hatte - der schaut nur auf Schlusskurse.
        if current_state == "neutral" or (previous_state
                                          and current_state != previous_state):
            message = body(f"⚠️ {index_name} Vorwarnung (SMA{sma_period}) - "
                           f"Handelsschluss XETRA 17:30 CET")
            status = "advisory"

    elif run_role == "decisive":
        if previous_state is None:
            message = body(f"🆕 {index_name}: Alert initialisiert "
                           f"(SMA{sma_period}) - Zustand {current_state.upper()}")
            status = "initialised"
        elif previous_state != current_state:
            emoji = {"above": "🚀", "below": "📉", "neutral": "📊"}[current_state]
            message = body(f"{emoji} {index_name}: SMA{sma_period} "
                           f"{previous_state.upper()} → {current_state.upper()} "
                           f"- noch bis 17:30 CET handelbar")
            status = f"crossover_{current_state}"
        save_eodhd_sma_state(index_symbol, sma_period, current_state,
                             current_price, sma_value, env=env)

    else:  # reconcile
        if previous_state != current_state:
            message = body(f"🔁 {index_name}: Korrektur nach Schlusskurs "
                           f"(SMA{sma_period}) - {previous_state} → {current_state}")
            status = "reconciled"
        save_eodhd_sma_state(index_symbol, sma_period, current_state,
                             current_price, sma_value, env=env)

    if message:
        send_telegram_message(message)

    return jsonify({
        "message": message or f"{index_name} ist {current_state} SMA{sma_period}",
        "status": status,
        "run_role": run_role,
        "current_price": current_price,
        "sma_value": sma_value,
        "price_diff_percent": diff_percent,
        "trigger_price": trigger,
        "previous_state": previous_state,
        "current_state": current_state,
    }), 200
```

- [ ] **Step 4: Weiche und Fehlerzweig in `check_unified_index_alert` einbauen**

Bei den Parameter-Extraktionen (aktuell um [main.py:3262](../../../main.py#L3262)) ergänzen:

```python
    source = request_json.get("source", "alpaca")            # "alpaca" | "eodhd"
    currency_symbol = request_json.get("currency_symbol", "$")
    run_role = request_json.get("run_role", "decisive")      # nur fuer source="eodhd"
```

Ganz am Anfang des `elif alert_type == "sma_crossing":`-Zweigs, **vor** dem bestehenden `market_data = get_all_market_data(...)`:

```python
        elif alert_type == "sma_crossing":
            if source == "eodhd":
                return _handle_eodhd_sma_crossing(
                    index_symbol, index_name, sma_period, noise_threshold,
                    currency_symbol, run_role, env)

            # ── Alpaca-Pfad, unveraendert ──────────────────────────────────
            # Handle SMA crossing alerts with crossover detection
```

Und den Fehlerzweig am Ende der Funktion (aktuell [main.py:3420](../../../main.py#L3420)) um einen vorgelagerten `except` erweitern:

```python
    except EodhdDataError as e:
        error_message = f"❗ {index_name} ({index_symbol}, {run_role}): {e}"
        print(error_message)
        send_telegram_message(error_message)
        return jsonify({"error": str(e), "status": "data_error"}), 500
    except Exception as e:
        error_message = f"Error checking {index_name} alert: {str(e)}"
        print(error_message)
        send_telegram_message(error_message)
        return jsonify({"error": error_message}), 500
```

- [ ] **Step 5: Tests laufen lassen, Erfolg bestätigen**

```bash
python3 -m pytest tests/test_eodhd_index_alert.py -v
```

Erwartet: alle PASS (34 Tests).

- [ ] **Step 6: Committen**

```bash
git add main.py tests/test_eodhd_index_alert.py
git commit -m "alert: EODHD-Handler mit drei Lauf-Rollen

advisory / decisive / reconcile ersetzen die is_last_trading_hour-Logik des
Alpaca-Pfads, die an NYSE-Handelszeiten haengt und fuer eine XETRA-Entscheidung
bedeutungslos ist.

advisory schreibt bewusst keinen State: ein am Band zitternder Intraday-Kurs
wuerde sonst Whipsaw-Alerts erzeugen, die der Backtest nie hatte - der schaut
ausschliesslich auf Schlusskurse.

Der Alpaca-Pfad wird nur um eine vorgelagerte Weiche ergaenzt und bleibt sonst
Zeile fuer Zeile unveraendert."
```

---

### Task 5: CLI reparieren und erweitern

`--action index_alert` ist heute unbenutzbar: `run_local` hat `request="test"` ([main.py:6214](../../../main.py#L6214)), `check_unified_index_alert` greift auf `request.content_type` zu — ein String hat das nicht. Außerdem gibt es keine Argumente für Symbol oder Fenster.

**Files:**
- Modify: `main.py` (`run_local` und der `__main__`-Block)

**Interfaces:**
- Consumes: `check_unified_index_alert` aus Task 4
- Produces: `class _LocalRequest`, erweiterte `run_local(..., alert_payload=None)`

- [ ] **Step 1: Den Fehlschlag reproduzieren**

```bash
python3 main.py --action index_alert --env paper
```

Erwartet: `AttributeError: 'str' object has no attribute 'content_type'`.

- [ ] **Step 2: `_LocalRequest` implementieren**

Direkt vor `def run_local(` in `main.py`:

```python
class _LocalRequest:
    """Minimaler Ersatz fuer ein Flask-Request-Objekt, damit run_local die
    HTTP-Handler direkt aufrufen kann."""

    content_type = "application/json"

    def __init__(self, payload):
        self._payload = payload
        self.data = json.dumps(payload).encode("utf-8")

    def get_json(self, silent=False):
        return self._payload
```

- [ ] **Step 3: `run_local` anpassen**

Signatur ([main.py:6214](../../../main.py#L6214)) um `alert_payload=None` erweitern und den `index_alert`-Zweig ersetzen:

```python
def run_local(action, env="paper", request="test", force_execute=False,
              investment_amount=None, alert_payload=None):
```

```python
    elif action == "index_alert":
        if not alert_payload:
            return ("index_alert braucht mindestens --index_symbol. "
                    "Beispiel: --action index_alert --source eodhd "
                    "--index_symbol IUSQ.XETRA --sma_period 255")
        return check_unified_index_alert(_LocalRequest(alert_payload), env=env)
```

- [ ] **Step 4: argparse erweitern**

Im `__main__`-Block hinter `--investment_amount`:

```python
    parser.add_argument("--index_symbol", default=None,
                        help="index_alert: Symbol, z.B. IUSQ.XETRA oder SPY")
    parser.add_argument("--index_name", default=None,
                        help="index_alert: Klartextname fuer die Telegram-Nachricht")
    parser.add_argument("--alert_type", default="sma_crossing",
                        choices=["sma_crossing", "ath_drop"])
    parser.add_argument("--source", default="alpaca", choices=["alpaca", "eodhd"])
    parser.add_argument("--run_role", default="decisive",
                        choices=["advisory", "decisive", "reconcile"],
                        help="index_alert, nur bei --source eodhd")
    parser.add_argument("--sma_period", type=int, default=200)
    parser.add_argument("--noise_threshold", type=float, default=1.0)
    parser.add_argument("--currency_symbol", default=None,
                        help="Default: EUR-Zeichen bei --source eodhd, sonst $")
```

Und den Aufruf am Dateiende ersetzen:

```python
    args = parser.parse_args()

    alert_payload = None
    if args.index_symbol:
        alert_payload = {
            "index_symbol": args.index_symbol,
            "index_name": args.index_name or args.index_symbol,
            "alert_type": args.alert_type,
            "source": args.source,
            "run_role": args.run_role,
            "sma_period": args.sma_period,
            "noise_threshold": args.noise_threshold,
            "currency_symbol": args.currency_symbol
                               or ("€" if args.source == "eodhd" else "$"),
        }

    result = run_local(action=args.action, env=args.env,
                       force_execute=args.force,
                       investment_amount=args.investment_amount,
                       alert_payload=alert_payload)
    print(f"\nResult: {result}\n")
```

- [ ] **Step 5: Beide Symbole live prüfen**

```bash
python3 main.py --action index_alert --env paper --source eodhd --run_role advisory \
  --index_symbol IUSQ.XETRA --index_name "MSCI ACWI (EUR)" --sma_period 255

python3 main.py --action index_alert --env paper --source eodhd --run_role advisory \
  --index_symbol EUNL.XETRA --index_name "MSCI World (EUR)" --sma_period 250
```

Erwartet, gegen die Referenztabelle des Specs (Stand 2026-09-07): ACWI SMA ≈ 97,27 €, Abstand ≈ +10,5 %; World SMA ≈ 116,16 €, Abstand ≈ +9,6 %. Beide `above`, `status` endet auf `_no_change`, **keine Telegram-Nachricht** (advisory schweigt außerhalb des Bands).

- [ ] **Step 6: Regression des Alpaca-Pfads prüfen**

```bash
python3 main.py --action index_alert --env paper --source alpaca \
  --index_symbol SPY --index_name "S&P 500 (SPY)" --sma_period 200
```

Erwartet: läuft durch den unveränderten Alpaca-Zweig, Preis in `$`, plausibler SPY-Kurs.

- [ ] **Step 7: Wächter prüfen**

```bash
python3 main.py --action index_alert --env paper --source eodhd --run_role advisory \
  --index_symbol SWRD.XETRA --index_name "Unbekannt" --sma_period 250
```

Erwartet: HTTP 500, Telegram-Nachricht mit `❗` und dem Hinweis auf `NA` — **nicht** stillschweigend `neutral`.

- [ ] **Step 8: Committen**

```bash
git add main.py
git commit -m "cli: index_alert lokal ausfuehrbar machen

run_local uebergab den String 'test' als request-Objekt, check_unified_index_alert
greift aber auf request.content_type zu - --action index_alert konnte also nie
laufen, und Symbol und Fenster liessen sich ohnehin nicht uebergeben.

Minimales _LocalRequest plus die noetigen argparse-Argumente. Damit ist der
neue EODHD-Zweig lokal gegen die Referenzwerte aus dem Spec pruefbar."
```

---

### Task 6: Scheduler in `cloudbuild.yaml`, alten Alert retiren

**Files:**
- Modify: `cloudbuild.yaml` (Wave D; der `urth_255sma_alert`-Block steht aktuell bei Zeile 573–599)

**Interfaces:**
- Consumes: die deployte Function `index_alert` (unverändert, kein neuer Deploy-Schritt)
- Produces: 6 Scheduler-Jobs — `acwi_eur_sma_{advisory,decisive,reconcile}`, `world_eur_sma_{advisory,decisive,reconcile}`

- [ ] **Step 1: `urth_255sma_alert`-Block ersetzen**

Den kompletten Block (Zeile 573–599, inklusive `waitFor`) durch einen Retirement-Kommentar im Stil der RSSB/WTIP- und Regime-World-Blöcke ersetzen:

```yaml
  # ─────────────────────────────────────────────────────────────────────────
  # RETIRED 2026-09-07: urth_255sma_alert
  #
  # Ersetzt durch world_eur_sma_* weiter unten. Der alte Alert mass URTH in USD
  # zu New Yorker Zeiten, die Handelsentscheidung faellt aber in EUR an der
  # XETRA um 17:30 CET - zwei verschiedene Zeitscheiben fuer dieselbe
  # Entscheidung.
  #
  # ACHTUNG: cloudbuild kennt nur create/update. Das Entfernen dieses Blocks
  # loescht den Job NICHT. Einmalig manuell noetig:
  #   gcloud scheduler jobs delete urth_255sma_alert --location=europe-west3
  #
  # msci_drop_alert bleibt bestehen - anderer alert_type (ATH-Drop/Kreditsignal).
  # ─────────────────────────────────────────────────────────────────────────
```

- [ ] **Step 2: Die 6 neuen Scheduler-Blöcke anhängen**

Ans Ende von Wave D, im `update … || create …`-Muster der bestehenden Blöcke. Alle sechs teilen dasselbe `waitFor` wie die anderen Wave-D-Schritte:

```yaml
  # ─────────────────────────────────────────────────────────────────────────
  # EUR-SMA-Alerts auf der XETRA-Zeitscheibe (2026-09-07)
  #
  # Zeitzone Europe/Berlin, NICHT America/New_York wie die vier aelteren
  # Alerts: der Anker ist der XETRA-Schluss um 17:30, und die Sommerzeit-
  # umstellungen der beiden Zonen sind nicht deckungsgleich.
  #
  # Drei Rollen pro Symbol:
  #   advisory  0 15-17  Vorwarnung, schreibt keinen State
  #   decisive  20 17    Handelsalarm, 10 Min vor Handelsschluss
  #   reconcile 45 17    Abgleich auf den echten Schluss + taeglicher Lebendtest
  # ─────────────────────────────────────────────────────────────────────────
  - name: 'gcr.io/cloud-builders/gcloud'
    entrypoint: 'bash'
    args:
      - '-c'
      - |
        set -e
        URI='https://europe-west3-trading-436516.cloudfunctions.net/index_alert'
        SA='1098661711782-compute@developer.gserviceaccount.com'

        create_or_update () {
          JOB="$1"; SCHED="$2"; BODY="$3"
          gcloud scheduler jobs update http "$JOB" \
            --schedule="$SCHED" --uri="$URI" --http-method='POST' \
            --time-zone='Europe/Berlin' --location='europe-west3' \
            --oidc-service-account-email="$SA" --message-body="$BODY" \
          || gcloud scheduler jobs create http "$JOB" \
            --schedule="$SCHED" --uri="$URI" --http-method='POST' \
            --time-zone='Europe/Berlin' --location='europe-west3' \
            --oidc-service-account-email="$SA" --message-body="$BODY"
        }

        ACWI_BASE='"index_symbol":"IUSQ.XETRA","index_name":"MSCI ACWI (EUR)","alert_type":"sma_crossing","source":"eodhd","sma_period":255,"noise_threshold":1.0,"currency_symbol":"€"'
        WORLD_BASE='"index_symbol":"EUNL.XETRA","index_name":"MSCI World (EUR)","alert_type":"sma_crossing","source":"eodhd","sma_period":250,"noise_threshold":1.0,"currency_symbol":"€"'

        create_or_update acwi_eur_sma_advisory  '0 15-17 * * 1-5' "{$ACWI_BASE,\"run_role\":\"advisory\"}"
        create_or_update acwi_eur_sma_decisive  '20 17 * * 1-5'   "{$ACWI_BASE,\"run_role\":\"decisive\"}"
        create_or_update acwi_eur_sma_reconcile '45 17 * * 1-5'   "{$ACWI_BASE,\"run_role\":\"reconcile\"}"

        create_or_update world_eur_sma_advisory  '0 15-17 * * 1-5' "{$WORLD_BASE,\"run_role\":\"advisory\"}"
        create_or_update world_eur_sma_decisive  '20 17 * * 1-5'   "{$WORLD_BASE,\"run_role\":\"decisive\"}"
        create_or_update world_eur_sma_reconcile '45 17 * * 1-5'   "{$WORLD_BASE,\"run_role\":\"reconcile\"}"
    waitFor:
      - 'deploy-aaa'
      - 'deploy-f4'
      - 'deploy-quarterly-f4'
      - 'deploy-index-alert'
      - 'deploy-audit'
```

- [ ] **Step 3: YAML-Syntax prüfen**

```bash
python3 -c "import yaml,sys; d=yaml.safe_load(open('cloudbuild.yaml')); print('steps:', len(d['steps']))"
grep -c "urth_255sma_alert" cloudbuild.yaml
```

Erwartet: YAML parst, `urth_255sma_alert` kommt nur noch im Retirement-Kommentar vor (Trefferzahl 1).

- [ ] **Step 4: Prüfen, dass keine Function-Deploys angefasst wurden**

```bash
git diff cloudbuild.yaml | grep -E "^[+-].*entry-point" || echo "keine Entry-Point-Aenderung — gut"
```

Erwartet: `keine Entry-Point-Aenderung — gut`. Wäre hier etwas zu sehen, drohte genau die in CLAUDE.md dokumentierte `MissingTargetException`.

- [ ] **Step 5: Committen**

```bash
git add cloudbuild.yaml
git commit -m "deploy: 6 EUR-SMA-Scheduler, urth_255sma_alert retired

Zeitzone Europe/Berlin statt America/New_York - der Anker ist der
XETRA-Schluss, und die Sommerzeitumstellungen der beiden Zonen sind nicht
deckungsgleich.

Kein neuer Deploy-Schritt und keine neue Cloud Function: index_alert nimmt den
neuen Pfad ueber den source-Parameter. Damit bleibt die Function-Liste
unveraendert und beide in CLAUDE.md dokumentierten Deploy-Fallen sind umgangen.

Der alte Job muss manuell geloescht werden, cloudbuild kennt nur create/update."
```

---

### Task 7: CLAUDE.md nachziehen und Übergabe

**Files:**
- Modify: `CLAUDE.md` (Abschnitt „Cloud Function ↔ Scheduler matrix", „Local development")

**Interfaces:**
- Consumes: alles aus Task 1–6
- Produces: keine Codeartefakte

- [ ] **Step 1: Scheduler-Matrix aktualisieren**

Die Zeile `| `index_alert` | ✓ × 4 | sp500_drop / msci_drop / urth_255sma / spy_200sma alerts |` ersetzen durch:

```markdown
| `index_alert` | ✓ × 9 | sp500_drop / msci_drop / spy_200sma (Alpaca, USD, NY-Zeit) + acwi_eur_sma_* und world_eur_sma_* (EODHD, EUR, XETRA-Zeit, je advisory/decisive/reconcile). `urth_255sma_alert` retired 2026-09-07 |
```

Und die Überschrift `### Cloud Function ↔ Scheduler matrix (16 functions, 11 schedulers as of 2026-05-17)` auf `(16 functions, 16 schedulers as of 2026-09-07)` ändern.

- [ ] **Step 2: Abschnitt zum EUR-Alert ergänzen**

Hinter dem Abschnitt „Non-fractionable tickers" einfügen:

```markdown
## EUR-SMA-Alerts laufen auf der XETRA-Zeitscheibe, nicht auf dem MSCI-Schluss

`acwi_eur_sma_*` und `world_eur_sma_*` gaten die beiden gehebelten Euro-Produkte
(2× ACWI `LU3386643970`, 2× World). Sie rechnen bewusst **nicht** auf dem
offiziellen MSCI-EUR-Net-TR-Schluss, sondern auf dem 17:30-XETRA-Schluss von
`IUSQ.XETRA` (255d) und `EUNL.XETRA` (250d) aus EODHD.

Grund: gehandelt wird bis 17:30 CET. Um 17:15 sind erst 1,75 von 6,5
US-Handelsstunden gelaufen; eine Hochrechnung auf den 22:00-Schluss hätte bei
~60 % US-Gewicht eine Unsicherheit von 0,4–0,5 % — die Hälfte des 1-%-Bands.
Auf der 17:30-Scheibe wird dagegen nichts geschätzt. Details und der gemessene
Preis dieser Wahl: [Spec](docs/superpowers/specs/2026-09-07-acwi-world-eur-sma-alert-design.md).

**Der EODHD-Pfad hat eigene State-Helper** (`get_eodhd_sma_state` /
`save_eodhd_sma_state`, Sammlung `index-alert-state-{env}`). Nicht auf
`save_index_sma_state` umstellen: das schreibt in den Alpaca-Preis-Cache
`market-data-{env}` und steigt aus, wenn das Dokument fehlt — für EODHD-Symbole
legt `update_market_data()` nie eines an, der State würde nie persistieren und
es gäbe **nie** einen Crossover-Alert.

**Drei Lauf-Rollen** über den `run_role`-Parameter: `advisory` (15:00–17:00,
schreibt keinen State), `decisive` (17:20, Handelsalarm), `reconcile` (17:45,
Abgleich auf den echten Schluss). `advisory` schreibt bewusst nicht — sonst
erzeugt ein am Band zitternder Intraday-Kurs Whipsaw-Alerts, die der Backtest
nie hatte.
```

- [ ] **Step 3: Local-development-Beispiele ergänzen**

Im Codeblock unter „Local development" anhängen:

```bash
# 2026-09-07: EUR-SMA-Alerts (EODHD-Quelle, XETRA-Zeitscheibe)
python3 main.py --action index_alert --env paper --source eodhd --run_role advisory \
  --index_symbol IUSQ.XETRA --index_name "MSCI ACWI (EUR)" --sma_period 255
python3 main.py --action index_alert --env paper --source eodhd --run_role advisory \
  --index_symbol EUNL.XETRA --index_name "MSCI World (EUR)" --sma_period 250

# Unit-Tests (pytest steht in requirements-dev.txt, NICHT in requirements.txt)
python3 -m pytest tests/ -v
```

- [ ] **Step 4: Alles noch einmal grün**

```bash
python3 -m pytest tests/ -v
```

Erwartet: 34 PASS.

- [ ] **Step 5: Committen**

```bash
git add CLAUDE.md
git commit -m "docs: EUR-SMA-Alerts in CLAUDE.md dokumentiert

Vor allem die Falle festgehalten, dass der EODHD-Pfad eigene State-Helper
braucht - save_index_sma_state haette den Alert stillschweigend funktionslos
gemacht."
```

- [ ] **Step 6: Übergabe an Carl (keine Automatik)**

Diese drei Schritte macht **Carl**, nicht der Implementierer:

1. Branch mergen und pushen — der Push auf `main` löst den Cloud Build aus (~25–30 min).
2. Nach dem grünen Build den alten Job löschen:
   ```bash
   gcloud scheduler jobs delete urth_255sma_alert --location=europe-west3
   ```
   Unterbleibt das, feuern alte USD-Alerts neben den neuen EUR-Alerts.
3. Die neuen Jobs prüfen und einen scharf testen:
   ```bash
   gcloud scheduler jobs list --location=europe-west3 | grep eur_sma
   gcloud scheduler jobs run acwi_eur_sma_decisive --location=europe-west3
   ```
   Erwartet beim allerersten `decisive`-Lauf: eine Telegram-Nachricht
   „🆕 MSCI ACWI (EUR): Alert initialisiert (SMA255) — Zustand ABOVE".
   Diese Nachricht ist der Beweis, dass die Kette bis Firestore durchläuft —
   danach schweigt der Alert planmäßig, bis der Kurs die Linie erreicht.

---

## Self-Review

**Spec-Abdeckung**

| Spec-Abschnitt | Task |
|---|---|
| Signaldefinition (Fensteraufbau, heutigen Bar verwerfen) | 1 |
| Plausibilitätswächter (5 Stück) | 1 (4) + 4 (Zeitstempel-Wächter für `decisive`) |
| Datenquelle EODHD, beide Legs | 2 |
| Fehlerverhalten (❗, 500, kein State-Schreiben) | 2 (Werfen) + 4 (`except EodhdDataError`) |
| Läufe und Rollen, State-Semantik | 4 |
| `source` / `currency_symbol` / `run_role` | 4 |
| Alpaca-Pfad unverändert | 4 (Weiche) + 5 (Regressionstest) |
| CLI-Fix | 5 |
| 6 Scheduler, Europe/Berlin | 6 |
| `urth_255sma_alert` retiren + manuelles Löschen | 6 + 7 |
| Referenzwerte verifizieren | 5 (Step 5) |

Eine **Abweichung vom Spec**, bewusst: der Spec sagt „State-Machine unverändert, `get_index_sma_state` / `save_index_sma_state` tragen". Das stimmt nicht — beim Schreiben des Plans kam heraus, dass `save_index_sma_state` ohne vorhandenes Dokument stillschweigend aussteigt und für EODHD-Symbole nie eines angelegt würde. Task 3 ersetzt diese Annahme durch eigene Helper. Der Spec wird in Task 7 nicht nachgezogen, weil CLAUDE.md die Falle prominenter dokumentiert; wer den Spec liest, findet über den CLAUDE.md-Abschnitt die korrigierte Fassung.

**Placeholder-Scan:** keine TBD/TODO, jeder Code-Schritt enthält lauffähigen Code, jeder Test-Schritt echte Assertions.

**Typ-Konsistenz geprüft:** `hist` ist überall `list[tuple[str, float]]` aufsteigend (Task 1 definiert, Task 2 liefert, Task 4 konsumiert). `_sma_state_from_diff` gibt überall `"above"|"below"|"neutral"`. `get_eodhd_sma_state` gibt `str | None` — Task 4 prüft explizit auf `None` für den `initialised`-Zweig. `save_eodhd_sma_state` hat in Task 3 und Task 4 dieselbe Signatur (6 Parameter, `env` als Keyword).

**Ein bewusstes Testloch:** `_handle_eodhd_sma_crossing` wird gegen ein gefälschtes Firestore und gefälschte HTTP-Antworten getestet, nicht gegen die echten. Der Realitätsabgleich passiert in Task 5, Steps 5–7 gegen die Referenztabelle des Specs, und in Task 7, Step 6 gegen die echte Cloud-Umgebung.
