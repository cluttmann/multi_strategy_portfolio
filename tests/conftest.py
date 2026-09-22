"""Kein Test darf eine echte Telegram-Nachricht senden.

Lokal liegt TELEGRAM_KEY in der .env, send_telegram_message sendet also
wirklich -- in den privaten Alerts-Kanal. Zwei F4-Tests pruefen den Fehlerpfad
des Retirement-Skripts, der genau so eine Meldung absetzt, und hatten Telegram
nie abgeklemmt: am 22.09.2026 kam pro Testlauf ein Paar "F4 retirement
stopped"-Nachrichten an. Diese Fixture haengt vor jedem Test. Wer Nachrichten
pruefen will, patcht darueber und bekommt seinen eigenen Recorder.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    sent = []
    monkeypatch.setattr(
        main,
        "send_telegram_message",
        lambda message, chat_id_secret="TELEGRAM_CHAT_ID": sent.append((chat_id_secret, message)),
    )
    yield sent
