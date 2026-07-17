"""
Webhook notifier.

POSTs a JSON payload ``{"event", "text", "timestamp"}`` to every configured
webhook URL. Delivery is best-effort and non-blocking to the trading loop.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Iterable

import requests


class WebhookNotifier:
    def __init__(self, webhooks: Iterable, timeout: float = 10.0):
        self._webhooks = [w for w in webhooks if w.url and w.enabled]
        self._timeout = timeout
        self._lock = threading.Lock()

    @property
    def targets(self) -> int:
        return len(self._webhooks)

    def send(self, text: str, event: str = "notification") -> None:
        if not text or not self._webhooks:
            return
        payload = {
            "event": event,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "unix_ts": int(time.time()),
        }
        for hook in list(self._webhooks):
            self._post_one(hook.url, payload)

    def _post_one(self, url: str, payload: dict) -> None:
        try:
            resp = requests.post(url, json=payload, timeout=self._timeout)
            if resp.status_code >= 400:
                print(
                    f"[webhook] POST {url} failed ({resp.status_code}): "
                    f"{resp.text[:200]}"
                )
        except requests.RequestException as exc:
            print(f"[webhook] POST {url} error: {exc}")
