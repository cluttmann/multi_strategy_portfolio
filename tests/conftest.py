"""Kein Test darf nach draussen: keine Telegram-Nachricht, keine Order, kein
Kurs- oder Positionsabruf.

Lokal liegen TELEGRAM_KEY und die LIVE-Alpaca-Schluessel in der .env. Am
22.09.2026 schickten zwei F4-Tests pro Lauf echte "F4 retirement stopped"-
Nachrichten; am 23.09. machten Tagesjob-Tests echte EODHD-Abrufe, weil sie
eine umbenannte Funktion mockten. Haette einer von ihnen submit_order nicht
gemockt, waere eine echte Order ans Live-Konto gegangen.

Die Fixture haengt vor jedem Test und ersetzt jeden Aussenkontakt durch einen
Recorder (Telegram) bzw. einen lauten Fehler (alles andere). Ein Test, der
einen davon braucht, patcht darueber - monkeypatch im Test gewinnt.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main

import requests

_BLOCKED = ["submit_order", "get_firestore_client"]


@pytest.fixture(autouse=True)
def _no_outside_world(monkeypatch):
    sent = []
    monkeypatch.setattr(
        main, "send_telegram_message",
        lambda message, chat_id_secret="TELEGRAM_CHAT_ID": sent.append((chat_id_secret, message)))
    for name in _BLOCKED:
        def _blocked(*args, _name=name, **kwargs):
            raise RuntimeError(f"Test ruft main.{_name} ungemockt auf - Aussenkontakt gesperrt")
        monkeypatch.setattr(main, name, _blocked)

    # Netzwerkschicht: jeder HTTP-Aufruf (Alpaca, EODHD, FRED, Telegram) laeuft
    # durch Session.request. Tests, die requests.get selbst mocken, kommen hier
    # nie an - genau so sollen sie es tun.
    def _no_http(self, method, url, *args, **kwargs):
        raise RuntimeError(f"Test macht echten HTTP-Aufruf: {method} {str(url).split('?')[0]}")
    monkeypatch.setattr(requests.Session, "request", _no_http)
    yield sent
