"""Telegram notifications — eigener Quant-Channel, QNT-prefixed.

Getrennt vom ETF-Bot (2026-08-07): der Bot-Token ist derselbe, aber das
Quant-Desk postet in die Gruppe aus TELEGRAM_CHAT_ID_QNT. Fallback auf
TELEGRAM_CHAT_ID ist Absicht — fehlt das neue Secret irgendwo (lokaler Run,
vergessenes Job-Update), landen Alerts im alten Chat statt spurlos zu
verschwinden. Eine stille Benachrichtigung ist schlimmer als eine im
falschen Kanal.
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()


def notify(text: str):
    token = os.environ.get("TELEGRAM_KEY")
    chat = os.environ.get("TELEGRAM_CHAT_ID_QNT") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(f"[telegram unavailable] {text}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": f"🧪 QNT | {text}"},
            timeout=15,
        )
    except Exception as e:  # noqa: BLE001 — never let telegram kill trading
        print(f"[telegram failed: {e}] {text}")
