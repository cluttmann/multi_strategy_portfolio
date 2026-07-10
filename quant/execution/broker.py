"""Alpaca paper-trading wrapper for the quant system.

Every order carries client_order_id = QNT-{SLEEVE}-{YYYYMMDD}-{seq} so fills
on the shared paper account are attributable. Never call close_all here.
"""

import datetime as dt

import requests

from quant.config import (ALPACA_KEY_PAPER, ALPACA_PAPER_BASE,
                          ALPACA_SECRET_PAPER, ORDER_TAG_PREFIX)

H = {"APCA-API-KEY-ID": ALPACA_KEY_PAPER,
     "APCA-API-SECRET-KEY": ALPACA_SECRET_PAPER}


def account() -> dict:
    r = requests.get(f"{ALPACA_PAPER_BASE}/v2/account", headers=H, timeout=30)
    r.raise_for_status()
    return r.json()


def positions() -> dict[str, float]:
    r = requests.get(f"{ALPACA_PAPER_BASE}/v2/positions", headers=H, timeout=30)
    r.raise_for_status()
    return {p["symbol"]: float(p["qty"]) for p in r.json()}


def latest_prices(symbols: list[str]) -> dict[str, float]:
    """REST batch snapshot (IEX) — quotes for sizing, not for pegging."""
    out = {}
    for i in range(0, len(symbols), 100):
        batch = ",".join(symbols[i:i + 100])
        r = requests.get("https://data.alpaca.markets/v2/stocks/trades/latest",
                         params={"symbols": batch, "feed": "iex"},
                         headers=H, timeout=30)
        r.raise_for_status()
        for s, t in (r.json().get("trades") or {}).items():
            out[s] = float(t["p"])
    return out


def submit_order(symbol: str, qty: int, side: str, tif: str, sleeve: str,
                 seq: int, order_type: str = "market") -> dict:
    coid = (f"{ORDER_TAG_PREFIX}-{sleeve.upper()}-"
            f"{dt.date.today():%Y%m%d}-{seq:03d}")
    r = requests.post(f"{ALPACA_PAPER_BASE}/v2/orders", headers=H, json={
        "symbol": symbol, "qty": str(int(qty)), "side": side,
        "type": order_type, "time_in_force": tif, "client_order_id": coid,
    }, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"order {symbol} {side} x{qty} rejected: "
                           f"HTTP {r.status_code} {r.text[:150]}")
    return r.json()


def orders_today(sleeve: str) -> list[dict]:
    r = requests.get(f"{ALPACA_PAPER_BASE}/v2/orders",
                     params={"status": "all", "limit": 500,
                             "after": f"{dt.date.today()}T00:00:00Z"},
                     headers=H, timeout=30)
    r.raise_for_status()
    tag = f"{ORDER_TAG_PREFIX}-{sleeve.upper()}-"
    return [o for o in r.json()
            if (o.get("client_order_id") or "").startswith(tag)]
