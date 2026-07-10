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
                equity: float, sleeve_gross_after: float) -> tuple[bool, str]:
    if symbol in BOT_TICKERS:
        return False, f"{symbol} belongs to the ETF bot — excluded"
    if notional > MAX_PER_NAME_FRAC * equity:
        return False, (f"{symbol} notional {notional:.0f} exceeds "
                       f"{MAX_PER_NAME_FRAC:.0%} of equity")
    cap = MAX_SLEEVE_GROSS.get(sleeve, 0.25)
    if sleeve_gross_after > cap * equity:
        return False, f"sleeve {sleeve} gross would exceed {cap:.0%} of equity"
    return True, "ok"
