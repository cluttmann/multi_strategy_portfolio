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


def tradable_symbols(symbols: list[str]) -> set[str]:
    """Welche der Symbole sind bei Alpaca aktuell aktiv/handelbar?

    MERGARB-Deals lösen sich manchmal auf, bevor unser Delisting-Proxy
    (Terminaldatum per Preishistorie, kein 8-K-Terminierungsscan) es merkt —
    der Broker weiß es zuerst. Gefunden 2026-08-06: erster Live-Order-Versuch
    für FORA (bereits delisted) schlug mit "asset FORA is not active" fehl.
    """
    out = set()
    for s in symbols:
        try:
            r = requests.get(f"{ALPACA_PAPER_BASE}/v2/assets/{s}",
                             headers=H, timeout=15)
            if r.status_code == 200:
                a = r.json()
                if a.get("tradable") and a.get("status") == "active":
                    out.add(s)
        except Exception:  # noqa: BLE001
            continue
    return out


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
    # Laufkennung (HHMMSS) im client_order_id: ohne sie kollidiert jeder
    # Wiederholungslauf mit HTTP 422 / 40010001 "client_order_id must be
    # unique", weil `seq` nur der Schleifenindex ist. Genau das passierte am
    # 2026-07-24, nachdem der erste Lauf an einem HTB-Titel abgebrochen war.
    # Das Präfix QNT-<SLEEVE>- bleibt unverändert — orders_today() filtert
    # darauf.
    coid = (f"{ORDER_TAG_PREFIX}-{sleeve.upper()}-"
            f"{dt.date.today():%Y%m%d}-{dt.datetime.now():%H%M%S}-{seq:03d}")
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

def sleeve_fills_today(sleeve: str) -> dict[str, int]:
    """Signierte Fill-Mengen des Sleeves von heute.

    Alpaca markiert gefüllte Auktionsorders (opg/cls) häufig als "expired" —
    verifiziert 2026-07-25: 7 von 10 echten Fills hatten status=expired mit
    filled_qty>0. Es wird daher NIE auf status gefiltert, sondern auf
    filled_qty. Ohne diesen Fix bleibt das Ledger leer, während Positionen
    offen sind (führte zu Verdopplungs-Risiko beim nächsten Rebalance).
    """
    out: dict[str, int] = {}
    for o in orders_today(sleeve):
        q = int(float(o.get("filled_qty") or 0))
        if q <= 0:
            continue
        s = o["symbol"]
        out[s] = out.get(s, 0) + (q if o["side"] == "buy" else -q)
    return out
