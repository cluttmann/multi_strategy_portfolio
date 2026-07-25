"""Pre-trade risk gates — fail-closed, per DESIGN.md §3.6.

check_order() must pass for every order the quant system submits. Any doubt
(missing state, stale data, manual halt) blocks the trade; matching the
margin-gate philosophy of the ETF bot: silence never means safe.
"""

from quant.config import BOT_TICKERS
from quant.execution import broker, ledger

MAX_PER_NAME_FRAC = 0.10       # of account equity
MAX_SLEEVE_GROSS = {"onx": 0.60}   # sleeve gross cap as fraction of equity
DD_HALVE = -0.08               # halve gross below this drawdown from HWM
DD_FLAT = -0.12                # flat + halt below this

# ── Konto-Gross-Deckel ───────────────────────────────────────────────────────
# Reg-T erlaubt 2.0x über Nacht; 1.60x lässt Puffer für Bewertungssprünge
# zwischen Ordereingabe und Auktionsfüllung. DIESER Deckel ist der eigentliche
# Engpass, nicht die Summe der Sleeve-Allokationen — deshalb DARF diese Summe
# 1.0 überschreiten. Genau das ist der Punkt: EOMT ist nur ~5 Tage/Monat im
# Markt, VOLC nur bei Contango > 3 %, DTRD schichtet monatlich. Ihre nominalen
# Allokationen liegen die meiste Zeit brach. Sie überlappen zu lassen bedeutet,
# dass derselbe Margin-Dollar zu verschiedenen Zeiten von mehreren Sleeves
# verdient — "Rendite pro Reg-T-Notional-Dollar" statt "Rendite pro Sleeve".
# Gemessen 2026-07-25: Konto-Gross 0.05x bei 1.9x verfügbar = 2.6 % Ausnutzung.
MAX_ACCOUNT_GROSS = 1.60

_approved_notional = 0.0       # in diesem Prozesslauf freigegeben
_gross_cache: float | None = None


def account_gross(refresh: bool = False) -> float:
    """Aktuelles Brutto-Exposure beim Broker (Long + |Short|), gecacht."""
    global _gross_cache
    if _gross_cache is None or refresh:
        a = broker.account()
        _gross_cache = (abs(float(a.get("long_market_value") or 0.0))
                        + abs(float(a.get("short_market_value") or 0.0)))
    return _gross_cache


def reset_run_state():
    """Vor jedem Handelslauf aufrufen — verwirft Cache und Freigabezähler."""
    global _approved_notional, _gross_cache
    _approved_notional, _gross_cache = 0.0, None


def drawdown_scale(equity: float) -> float:
    """0.0 (halted), 0.5 (halved) or 1.0 — also maintains the HWM."""
    st = ledger.risk_state()
    if st.get("manual_halt"):
        return 0.0
    hwm = max(float(st.get("hwm") or 0.0), equity)
    if hwm != st.get("hwm"):
        ledger.update_risk_state(hwm=hwm)
    dd = equity / hwm - 1
    if dd <= DD_FLAT:
        ledger.update_risk_state(dd_halt=True, last_dd=dd)
        return 0.0
    if dd <= DD_HALVE:
        return 0.5
    return 1.0


def check_order(symbol: str, notional: float, sleeve: str,
                equity: float, sleeve_gross_after: float,
                reduces_exposure: bool = False) -> tuple[bool, str]:
    """Fail-closed Pre-Trade-Gate. `reduces_exposure=True` für Orders, die eine
    Position verkleinern oder schließen — die dürfen den Konto-Deckel NICHT
    treffen, sonst blockiert der Schutz genau die risikosenkenden Trades."""
    global _approved_notional
    if symbol in BOT_TICKERS:
        return False, f"{symbol} belongs to the ETF bot — excluded"
    if notional > MAX_PER_NAME_FRAC * equity:
        return False, (f"{symbol} notional {notional:.0f} exceeds "
                       f"{MAX_PER_NAME_FRAC:.0%} of equity")
    cap = MAX_SLEEVE_GROSS.get(sleeve, 0.25)
    if sleeve_gross_after > cap * equity:
        return False, f"sleeve {sleeve} gross would exceed {cap:.0%} of equity"
    if not reduces_exposure:
        try:
            total = account_gross() + _approved_notional + notional
        except Exception as e:  # noqa: BLE001 — fail-closed
            return False, f"Konto-Gross nicht lesbar ({e}) — blockiert"
        if total > MAX_ACCOUNT_GROSS * equity:
            return False, (f"Konto-Gross {total/equity:.2f}x würde den Deckel "
                           f"{MAX_ACCOUNT_GROSS:.2f}x überschreiten "
                           f"(Reg-T-Schutz)")
        _approved_notional += notional
    return True, "ok"
