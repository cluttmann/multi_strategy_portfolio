"""Telegram notifications — same channel the ETF bot uses, QNT-prefixed."""

import os

import requests
from dotenv import load_dotenv

load_dotenv()


def notify(text: str):
    token = os.environ.get("TELEGRAM_KEY")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
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
