"""
Notifier manager + message formatting.

Formats the three notification categories required by the project:

* ``open_orders``  - the markets the bot currently has resting orders in.
* ``fill``         - buy / sell order fill details.
* ``status``       - runtime status: monitored markets + current positions.

Then dispatches the formatted text to every Telegram bot and every webhook.
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
        # Telegram uses HTML; webhooks get plain text + metadata.
        self.telegram.send(text)
        self.webhook.send(text, event=event)

    # ------------------------------------------------------------------ #
    # Event formatters
    # ------------------------------------------------------------------ #
    def notify_open_orders(self, open_orders: list[dict[str, Any]], markets: list) -> None:
        """Push the current set of resting orders grouped by market."""
        if not open_orders:
            body = "📋 <b>Open Orders</b>\nNo resting orders."
        else:
            lines = [f"📋 <b>Open Orders</b> ({len(open_orders)})"]
            market_labels = {m.token_id: m.display() for m in markets}
            for i, o in enumerate(open_orders, 1):
                token = o.get("asset_id") or o.get("token_id") or o.get("market") or "?"
                label = market_labels.get(token, token)
                side = o.get("side", "?")
                size = _fmt_price(o.get("original_size") or o.get("size"))
                price = _fmt_price(o.get("price"))
                status = o.get("status", "?")
                lines.append(
                    f"{i}. {_html_escape(str(label))}\n"
                    f"   {side} {size} @ {price}  [{status}]\n"
                    f"   token: <code>{_html_escape(str(token))[:24]}…</code>"
                )
            body = "\n".join(lines)
        self._dispatch(body, "open_orders")

    def notify_fill(self, trade: dict[str, Any], markets: list) -> None:
        """Push details of a single buy/sell fill."""
        token = trade.get("asset_id") or trade.get("token_id") or trade.get("market") or "?"
        label = next((m.display() for m in markets if m.token_id == token), token)
        side = str(trade.get("side", "?")).upper()
        size = _fmt_price(trade.get("size") or trade.get("matched_amount"))
        price = _fmt_price(trade.get("price") or trade.get("fee_rate_bps"))
        oid = trade.get("id") or trade.get("order_id") or "?"
        ts = trade.get("timestamp") or trade.get("created_at") or ""
        verb = "Bought" if side.startswith("B") else "Sold"
        body = (
            f"✅ <b>Fill: {verb}</b>\n"
            f"Market: {_html_escape(str(label))}\n"
            f"Side: {side}\n"
            f"Size: {size} shares\n"
            f"Price: {price}\n"
            f"Order: <code>{_html_escape(str(oid))[:24]}</code>"
        )
        if ts:
            body += f"\nTime: {ts}"
        self._dispatch(body, "fill")

    def notify_status(
        self,
        markets: list,
        active_market,
        positions: list[dict[str, Any]],
        trading_params,
        cycle: int,
    ) -> None:
        """Push a periodic runtime status summary."""
        lines = ["📊 <b>Bot Status</b>"]
        lines.append(f"Cycle: {cycle}")
        lines.append(f"Active market: {_html_escape(active_market.display() if active_market else 'none')}")
        lines.append("Monitored markets:")
        if not markets:
            lines.append("  (none)")
        else:
            for i, m in enumerate(markets):
                mark = " *" if active_market and m.token_id == active_market.token_id else ""
                lines.append(f"  [{i}]{mark} {_html_escape(m.display())}")

        lines.append("Trading parameters:")
        lines.append(f"  share amount: {trading_params.share_amount}")
        lines.append(f"  entry price: {_fmt_price(trading_params.entry_price)}")
        lines.append(f"  exit price: {_fmt_price(trading_params.exit_price)}")
        lines.append(
            f"  take-profit: {_fmt_pct(trading_params.take_profit_pct)}  "
            f"stop-loss: {_fmt_pct(trading_params.stop_loss_pct)}"
        )

        lines.append("Positions:")
        if not positions:
            lines.append("  (no open positions)")
        else:
            for p in positions:
                lines.append(
                    f"  {_html_escape(str(p.get('label', p.get('token_id'))))}: "
                    f"{_fmt_price(p.get('size'))} @ avg {_fmt_price(p.get('avg_price'))}"
                )
        self._dispatch("\n".join(lines), "status")

    def notify_started(self, active_market) -> None:
        body = (
            "🟢 <b>Bot started</b>\n"
            f"Active market: {_html_escape(active_market.display() if active_market else 'none')}"
        )
        self._dispatch(body, "started")

    def notify_stopped(self) -> None:
        self._dispatch("🔴 <b>Bot stopped</b>", "stopped")

    def notify_config_change(self, summary: str) -> None:
        body = f"⚙️ <b>Config updated</b>\n{_html_escape(summary)}"
        self._dispatch(body, "config_change")
