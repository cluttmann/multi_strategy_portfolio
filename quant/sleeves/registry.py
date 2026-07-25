"""Deklaratives Sleeve-Register — Forschung → Live ohne neuen Executor-Code.

Jeder validierte Sleeve wird hier als SPEC eingetragen: Universum, Signal-
Funktion (liefert Zielgewichte), Rebalance-Frequenz, Allokation, plus die
validierten Kennzahlen für den Health-Monitor. Der generische Executor
(`quant.execution.generic_sleeve`) handelt jede registrierte Spec.

WARUM: In dieser Session verdienten VOLC und EOMT wochenlang nichts, weil
ihre Executors nie gebaut wurden — obwohl sie validiert waren. Mit dem
Register ist "validiert" gleichbedeutend mit "handelbar": eine neue Spec
eintragen genügt, der Scheduler greift sie automatisch auf.
"""

import datetime as dt
import os
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

try:
    import yaml
except ImportError:      # pragma: no cover
    # Der HANDELSPFAD darf nicht an der Beförderungs-Mechanik hängen. Beim
    # ersten Deployment der Discovery-Pipeline fehlte PyYAML im Cloud-Image;
    # ein Import auf Modulebene hätte etf-rebalance komplett abgebrochen,
    # statt nur die befördertern Sleeves auszulassen.
    yaml = None


@dataclass
class SleeveSpec:
    name: str
    beschreibung: str
    signal: Callable[[], tuple[dict[str, float], str]]
    alloc: float                      # Anteil der Equity
    freq: str                         # "daily" | "monthly_window"
    tif: str = "cls"                  # Auktionsorder
    sharpe_full: float = 0.0
    sharpe_now: float = 0.0
    ann: int = 252                    # Beobachtungsfrequenz für den Monitor
    long_only: bool = True
    notes: str = ""
    tags: list[str] = field(default_factory=list)


# ── Signalfunktionen (nur Zielgewichte, keine Order-Logik) ────────────────────
def _volc_signal() -> tuple[dict[str, float], str]:
    from quant.execution.etf_sleeves import _fred_contango, VOLC_CONTANGO_MIN
    c = _fred_contango()
    if c is None:
        return {}, "kein VIX-Signal (fail-closed → flat)"
    if c > VOLC_CONTANGO_MIN:
        return {"SVXY": 1.0}, f"Contango {c:+.1%} > 3% → long SVXY"
    return {}, f"Contango {c:+.1%} ≤ 3% → flat"


def _eomt_signal() -> tuple[dict[str, float], str]:
    from quant.execution.etf_sleeves import _eomt_in_window, EOMT_SYMBOLS
    inw, pos = _eomt_in_window()
    if not inw:
        return {}, f"Handelstag {pos} von hinten → außerhalb Fenster, flat"
    w = 1.0 / len(EOMT_SYMBOLS)
    return {s: w for s in EOMT_SYMBOLS}, f"T-{pos-1} vor Monatsende → EW"


def _dtrd_signal() -> tuple[dict[str, float], str]:
    """Cross-Asset-TSMOM: 126d-Momentum > 0, inverse-vol auf 10 % Vol-Target,
    Gross auf 1.0 begrenzt. Umschichtung nur am ersten Handelstag des Monats
    (die Frequenz wird vom Executor über `freq` erzwungen)."""
    from quant.data.bq import query
    from quant.research.dtrd_study import ALL, VOL_TARGET
    q = ", ".join(repr(s) for s in ALL)
    df = query(f"""
      SELECT date, symbol, adjusted_close AS ac, close * volume AS dvol
      FROM `trading-436516.quant.eod_bars`
      WHERE symbol IN ({q}) AND adjusted_close > 0
        AND date >= DATE_SUB(CURRENT_DATE(), INTERVAL 400 DAY)""")
    if df.empty:
        return {}, "keine Kursdaten (fail-closed)"
    df["date"] = pd.to_datetime(df["date"])
    px = df.pivot(index="date", columns="symbol", values="ac").sort_index()
    dv = df.pivot(index="date", columns="symbol", values="dvol").sort_index()
    if len(px) < 130:
        return {}, f"nur {len(px)} Tage Historie (fail-closed)"
    adv = dv.rolling(20).mean().iloc[-1]
    ret = px.pct_change()
    mom = px.iloc[-1] / px.iloc[-127] - 1
    vol = (ret.tail(63).std() * np.sqrt(252))
    ok = (mom > 0) & (adv >= 5e6) & vol.notna() & (vol > 0)
    w = (ok.astype(float) * (VOL_TARGET / vol.clip(lower=0.03))).fillna(0.0)
    if w.sum() <= 0:
        return {}, "kein Asset im Aufwärtstrend → flat"
    w = w / max(w.sum(), 1.0)
    w = w[w > 0.005]
    return w.to_dict(), (f"{len(w)} Assets im Trend "
                         f"(gross {w.sum():.0%}, 126d-Momentum)")


REGISTRY: dict[str, SleeveSpec] = {
    "volc": SleeveSpec(
        name="volc", beschreibung="Short-Vol via SVXY, Gate VIX3M/VIX-Contango>3%",
        signal=_volc_signal, alloc=0.15, freq="daily",
        sharpe_full=0.64, sharpe_now=0.46, tags=["vol"]),
    "eomt": SleeveSpec(
        name="eomt", beschreibung="Monatsend-Duration-Ernte Treasury-ETFs",
        signal=_eomt_signal, alloc=0.20, freq="daily",
        sharpe_full=0.87, sharpe_now=0.65, ann=12, tags=["rates", "flow"],
        notes="nur ~5 Tage/Monat im Markt → 80% der Tage kein Reg-T-Notional"),
    "dtrd": SleeveSpec(
        name="dtrd", beschreibung="Cross-Asset-TSMOM über 30 ETFs (ohne US-Aktien/Krypto)",
        signal=_dtrd_signal, alloc=0.15, freq="monthly_first",
        sharpe_full=0.73, sharpe_now=0.43, tags=["trend", "crossasset"],
        notes="ρ(XSR)=-0.001; monatlich → kostenimmun"),
}

PROMOTED_PATH = os.path.join(os.path.dirname(__file__), "promoted.yaml")


def _load_promoted() -> dict[str, SleeveSpec]:
    """Von der Discovery-Pipeline beförderte Sleeves nachladen.

    Beförderung ist damit eine Daten-Änderung: wer die Gates passiert, landet
    in `promoted.yaml` und wird beim nächsten Scheduler-Lauf gehandelt — ohne
    dass jemand einen Executor schreiben muss.
    """
    import importlib
    if yaml is None:
        print("[registry] PyYAML fehlt — befördertes Register übersprungen, "
              "die fest eingetragenen Sleeves handeln normal weiter")
        return {}
    if not os.path.exists(PROMOTED_PATH):
        return {}
    with open(PROMOTED_PATH) as f:
        doc = yaml.safe_load(f) or {}
    out: dict[str, SleeveSpec] = {}
    for e in doc.get("befoerdert") or []:
        try:
            mod_name, fn_name = e["live_signal"].rsplit(".", 1)
            fn = getattr(importlib.import_module(mod_name), fn_name)
        except Exception as ex:  # noqa: BLE001
            # Fail-closed: eine unimportierbare Signalfunktion darf den
            # gesamten Handelslauf nicht mitreißen, aber sie muss auffallen.
            print(f"[registry] Sleeve '{e.get('name')}' übersprungen — "
                  f"live_signal nicht ladbar: {ex}")
            continue
        m = e.get("metriken") or {}
        out[e["name"]] = SleeveSpec(
            name=e["name"], beschreibung=e.get("beschreibung", ""),
            signal=fn, alloc=float(e["alloc"]), freq=e.get("freq", "daily"),
            tif=e.get("tif", "cls"),
            sharpe_full=float(m.get("sharpe_full", 0.0)),
            sharpe_now=float(m.get("sharpe_now", 0.0)),
            ann=int(e.get("ann", 252)),
            long_only=bool(e.get("long_only", True)),
            notes=e.get("notes", ""), tags=list(e.get("tags") or []))
    return out


REGISTRY.update(_load_promoted())


def rebalance_today(spec: SleeveSpec, today: dt.date | None = None) -> bool:
    """Soll dieser Sleeve heute umschichten?"""
    today = today or dt.date.today()
    if spec.freq == "daily":
        return True
    if spec.freq == "monthly_first":
        import pandas_market_calendars as mcal
        cal = mcal.get_calendar("XNYS")
        first = today.replace(day=1)
        sched = cal.schedule(start_date=first.isoformat(),
                             end_date=today.isoformat())
        days = [d.date() for d in sched.index]
        return bool(days) and days[0] == today
    return False
