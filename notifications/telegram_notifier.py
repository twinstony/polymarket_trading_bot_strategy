"""
Telegram Bot API notifier.

Pushes the same notification text to every configured bot/chat. Uses raw HTTP
calls against the Bot API (``sendMessage``) so a single process can drive an
arbitrary number of bots without extra dependencies.
"""

from __future__ import annotations

import threading
from typing import Iterable

import requests

TELEGRAM_API = "https://api.telegram.org"


class TelegramNotifier:
    """Delivers messages to a list of (token, chat_id) destinations."""

    def __init__(self, bots: Iterable, timeout: float = 10.0):
        # ``bots`` are config.TelegramBot dataclass instances.
        self._bots = [b for b in bots if b.token and b.chat_id and b.enabled]
        self._timeout = timeout
        self._lock = threading.Lock()

    @property
    def targets(self) -> int:
        return len(self._bots)

    def send(self, text: str) -> None:
        if not text or not self._bots:
            return
        for bot in list(self._bots):
            self._send_one(bot.token, bot.chat_id, text)

    def _send_one(self, token: str, chat_id: str, text: str) -> None:
        url = f"{TELEGRAM_API}/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=self._timeout)
            if resp.status_code != 200:
                print(
                    f"[telegram] sendMessage failed ({resp.status_code}): "
                    f"{resp.text[:200]}"
                )
        except requests.RequestException as exc:
            print(f"[telegram] sendMessage error to {chat_id}: {exc}")
