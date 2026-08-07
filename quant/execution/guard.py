"""Handelstag-Guard + Burn-in-Skalierung für die geplanten Executors.

Verhindert, dass launchd-Jobs an Feiertagen/Wochenenden Orders platzieren,
und skaliert die Positionsgrößen im Burn-in (DESIGN.md G10: erste 20
Handelstage bei 25 %). Der Burn-in-Faktor liegt in Firestore
(qnt-risk/state.burn_in_scale) und lässt sich ohne Deploy anheben.
"""

import datetime as dt

import pandas_market_calendars as mcal

_NYSE = mcal.get_calendar("XNYS")


def is_trading_day(day: dt.date | None = None) -> bool:
    day = day or dt.date.today()
    sched = _NYSE.schedule(start_date=day.isoformat(), end_date=day.isoformat())
    return len(sched) > 0


def burn_in_scale() -> float:
    """0.25 im Burn-in (Default), 1.0 nach Freigabe. Aus Firestore lesbar."""
    try:
        from quant.execution import ledger
        v = ledger.risk_state().get("burn_in_scale")
        return float(v) if v is not None else 0.25
    except Exception:  # noqa: BLE001 — fail-safe: klein bleiben
        return 0.25


def sleeve_paused(sleeve: str) -> str | None:
    """Grund, falls dieser Sleeve pausiert ist — sonst None.

    Firestore: qnt-risk/state.paused_sleeves = {"onx": "Grund", ...}. Ein
    einzelner Sleeve muss ohne Deploy und ohne Scheduler-Eingriff stillgelegt
    werden können: wenn der Live-Kostenmonitor zeigt, dass ein Sleeve unter
    seinen Break-even gerutscht ist, darf die Reaktion nicht an einem
    Container-Build hängen. Fail-OPEN (bei Lesefehler wird gehandelt), weil das
    Gegenteil — ein Firestore-Ausfall legt alles still — schlechter ist als ein
    Tag zu viel Handel; die Risikogrenzen in risk.py greifen unabhängig davon.
    """
    try:
        from quant.execution import ledger
        p = ledger.risk_state().get("paused_sleeves") or {}
        v = p.get(sleeve.lower())
        return str(v) if v else None
    except Exception:  # noqa: BLE001
        return None


def guard_or_exit(sleeve: str) -> float:
    """Bricht ab, wenn kein Handelstag oder der Sleeve pausiert ist; sonst
    gibt den Burn-in-Faktor zurück."""
    import sys
    if not is_trading_day():
        print(f"{sleeve}: kein NYSE-Handelstag — übersprungen")
        sys.exit(0)
    why = sleeve_paused(sleeve)
    if why:
        print(f"{sleeve}: PAUSIERT — {why}")
        sys.exit(0)
    return burn_in_scale()
