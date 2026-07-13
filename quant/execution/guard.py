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


def guard_or_exit(sleeve: str) -> float:
    """Bricht ab, wenn kein Handelstag; sonst gibt den Burn-in-Faktor zurück."""
    import sys
    if not is_trading_day():
        print(f"{sleeve}: kein NYSE-Handelstag — übersprungen")
        sys.exit(0)
    return burn_in_scale()
