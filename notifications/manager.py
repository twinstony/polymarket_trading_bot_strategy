"""
Notifier manager + message formatting (multi-strategy).

Formats notifications for the multi-strategy asyncio runtime:

* ``fill``         — buy / sell order fill details (per-strategy).
* ``status``       — aggregate runtime status: all strategies + positions.
* ``started`` / ``stopped`` — bot lifecycle.

Dispatches formatted text to every Telegram bot and every webhook.
"""

from __future__ import annotations

import threading
from typing import Any

from notifications.telegram_notifier import TelegramNotifier
from notifications.webhook_notifier import WebhookNotifier


def _fmt_price(v: Any) -> str:
    try:
        return f"{float(v):.4f}"
    except (TypeError, ValueError):
        return str(v) if v is not None else "n/a"


def _fmt_pct(v: Any) -> str:
    if v is None:
        return "off"
    try:
        return f"{float(v):.2%}"
    except (TypeError, ValueError):
        return str(v)


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class NotifierManager:
    """Routes formatted notifications to Telegram bots and webhooks."""

    def __init__(self, config):
        self.telegram = TelegramNotifier(config.telegram_bots)
        self.webhook = WebhookNotifier(config.webhooks)
        self._enabled = self.telegram.targets > 0 or self.webhook.targets > 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def telegram_targets(self) -> int:
        return self.telegram.targets

    @property
    def webhook_targets(self) -> int:
        return self.webhook.targets

    # ------------------------------------------------------------------ #
    # Internal dispatch
    # ------------------------------------------------------------------ #
    def _dispatch(self, text: str, event: str) -> None:
        if not text:
            return
        self.telegram.send(text)
        self.webhook.send(text, event=event)

    # ------------------------------------------------------------------ #
    # Event formatters
    # ------------------------------------------------------------------ #
    def notify_fill(self, trade: dict[str, Any], strategy_config) -> None:
        """Push details of a single buy/sell fill for a strategy."""
        label = strategy_config.display()
        side = str(trade.get("side", "?")).upper()
        size = _fmt_price(trade.get("size") or trade.get("matched_amount"))
        price = _fmt_price(trade.get("price") or trade.get("fee_rate_bps"))
        oid = trade.get("id") or trade.get("order_id") or "?"
        ts = trade.get("timestamp") or trade.get("created_at") or ""
        verb = "Bought" if side.startswith("B") else "Sold"
        body = (
            f"✅ <b>Fill: {verb}</b>\n"
            f"Strategy: {_html_escape(label)}\n"
            f"Side: {side}\n"
            f"Size: {size} shares\n"
            f"Price: {price}\n"
            f"Order: <code>{_html_escape(str(oid))[:24]}</code>"
        )
        if ts:
            body += f"\nTime: {ts}"
        self._dispatch(body, "fill")

    def notify_status(self, runtimes: list) -> None:
        """Push an aggregate runtime status summary across all strategies."""
        lines = ["📊 <b>Bot Status</b>"]
        lines.append(f"Strategies: {len(runtimes)}")

        for i, rt in enumerate(runtimes, 1):
            snap = rt.snapshot()
            pos_str = ""
            if snap.get("position_size", 0) > 0:
                pos_str = (
                    f" | 持仓 {snap['position_size']} @ {_fmt_price(snap['avg_price'])}"
                )
            lines.append(
                f"[{i}] {_html_escape(snap['label'])}\n"
                f"   {snap['state']} cycle={snap['cycle']}"
                f" entry={snap['entry_price']} exit={snap['exit_price']}"
                f" tp={_fmt_pct(snap['take_profit_pct'])}"
                f" sl={_fmt_pct(snap['stop_loss_pct'])}"
                f"{pos_str}"
            )
        self._dispatch("\n".join(lines), "status")

    def notify_started(self, runtimes: list) -> None:
        lines = [f"🟢 <b>Bot started</b> ({len(runtimes)} strategies)"]
        for i, rt in enumerate(runtimes, 1):
            lines.append(f"  [{i}] {_html_escape(rt.label)}")
        self._dispatch("\n".join(lines), "started")

    def notify_stopped(self) -> None:
        self._dispatch("🔴 <b>Bot stopped</b>", "stopped")

    def notify_config_change(self, summary: str) -> None:
        body = f"⚙️ <b>Config updated</b>\n{_html_escape(summary)}"
        self._dispatch(body, "config_change")
