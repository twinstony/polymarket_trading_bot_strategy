"""
Interactive Telegram bot for runtime control.

Listens for commands via long-polling ``getUpdates`` and replies with
``sendMessage``. Supports (and only supports) the operations required by the
project spec:

* set / switch the active trading market
* configure the buy (entry) price
* configure the exit strategy (exit price, take-profit, stop-loss, size)
* query current status & positions

Access can be restricted to a whitelist of Telegram user ids.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

import requests

TELEGRAM_API = "https://api.telegram.org"

# A callable returning the live status snapshot consumed by ``/status``.
StatusProvider = Callable[[], dict[str, Any]]


class TelegramCommandBot:
    def __init__(
        self,
        token: str,
        config_guard,
        status_provider: StatusProvider,
        allowed_user_ids: set[int] | None = None,
        timeout: float = 35.0,
    ):
        self._token = token
        self._config_guard = config_guard
        self._status_provider = status_provider
        self._allowed = allowed_user_ids or set()
        self._timeout = timeout
        self._offset = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        me = self._call("getMe", {})
        if me and me.get("ok"):
            bot_user = me["result"].get("username", "?")
            print(f"[bot] Telegram command bot online as @{bot_user}")
        self._thread = threading.Thread(target=self._poll_loop, name="tg-cmd", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------ #
    # Polling
    # ------------------------------------------------------------------ #
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._call("getUpdates", {"offset": self._offset, "timeout": 30})
            except requests.RequestException as exc:
                print(f"[bot] getUpdates error: {exc}; retrying")
                self._stop.wait(5)
                continue
            if not data or not data.get("ok"):
                self._stop.wait(2)
                continue
            for update in data.get("result", []):
                self._offset = update.get("update_id", self._offset) + 1
                self._handle_update(update)

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not message:
            return
        chat_id = message.get("chat", {}).get("id")
        if chat_id is None:
            return
        user = message.get("from", {})
        user_id = user.get("id")
        if self._allowed and user_id not in self._allowed:
            self._send(chat_id, "⛔ You are not authorised to control this bot.")
            return
        text = (message.get("text") or "").strip()
        if not text:
            return
        reply = self._dispatch_command(text)
        if reply:
            self._send(chat_id, reply)

    # ------------------------------------------------------------------ #
    # Command dispatch
    # ------------------------------------------------------------------ #
    def _dispatch_command(self, text: str) -> str:
        parts = text.split()
        if not parts:
            return ""
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("/start", "/help"):
            return self._help()
        if cmd == "/status":
            return self._status()
        if cmd == "/market":
            return self._market(args)
        if cmd == "/price":
            return self._set_price("entry_price", args, "Buy (entry) price")
        if cmd == "/exit_price":
            return self._set_price("exit_price", args, "Exit price")
        if cmd == "/takeprofit":
            return self._set_pct("take_profit_pct", args, "Take-profit")
        if cmd == "/stoploss":
            return self._set_pct("stop_loss_pct", args, "Stop-loss")
        if cmd == "/amount":
            return self._set_amount(args)
        if cmd == "/triggermode":
            return self._set_conditional_entry(args)
        return (
            f"Unknown command: {cmd}\n\n{self._help()}"
        )

    def _help(self) -> str:
        return (
            "<b>Polymarket Bot commands</b>\n"
            "/status - show current status &amp; positions\n"
            "/market - list monitored markets\n"
            "/market set &lt;index&gt; - switch active market\n"
            "/market add &lt;token_id&gt; [label] - add &amp; switch to a market\n"
            "/price &lt;0-1&gt; - set buy (entry) price\n"
            "/exit_price &lt;0-1&gt; - set exit price\n"
            "/takeprofit &lt;pct|off&gt; - set take-profit (e.g. 10 = 10%)\n"
            "/stoploss &lt;pct|off&gt; - set stop-loss (e.g. 10 = 10%)\n"
            "/amount &lt;n&gt; - set order size (shares)\n"
            "/conditional_entry on|off - toggle entry mode (on=wait for ask ≤ price, off=place order immediately)"
        )

    def _status(self) -> str:
        snap = self._status_provider()
        markets = snap.get("markets", [])
        active = snap.get("active_market")
        positions = snap.get("positions", [])
        t = snap.get("trading_params")
        cycle = snap.get("cycle", "?")
        open_orders = snap.get("open_orders", [])

        lines = ["<b>Status</b>", f"Cycle: {cycle}"]
        lines.append(f"Active market: {_esc(active.display() if active else 'none')}")
        lines.append("Markets:")
        if not markets:
            lines.append("  (none)")
        else:
            for i, m in enumerate(markets):
                mark = " *" if active and m.token_id == active.token_id else ""
                lines.append(f"  [{i}]{mark} {_esc(m.display())}")
        if t is not None:
            lines.append(
                f"Buy price: {_p(t.entry_price)} | Exit price: {_p(t.exit_price)} | "
                f"Size: {t.share_amount}"
            )
            lines.append(
                f"Take-profit: {_pc(t.take_profit_pct)} | Stop-loss: {_pc(t.stop_loss_pct)}"
            )
            lines.append(
                f"Conditional entry: {'ON' if t.conditional_entry else 'OFF'}"
            )
        lines.append(f"Open orders: {len(open_orders)}")
        lines.append("Positions:")
        if not positions:
            lines.append("  (none)")
        else:
            for p in positions:
                lines.append(
                    f"  {_esc(str(p.get('label', p.get('token_id'))))}: "
                    f"{_p(p.get('size'))} @ avg {_p(p.get('avg_price'))}"
                )
        return "\n".join(lines)

    def _market(self, args: list[str]) -> str:
        if not args:
            snap = self._status_provider()
            markets = snap.get("markets", [])
            active = snap.get("active_market")
            if not markets:
                return "No monitored markets. Use: /market add <token_id> [label]"
            lines = ["<b>Monitored markets</b>"]
            for i, m in enumerate(markets):
                mark = " *" if active and m.token_id == active.token_id else ""
                lines.append(f"[{i}]{mark} {_esc(m.display())}  <code>{_esc(m.token_id[:20])}…</code>")
            lines.append("\n* = active. Use /market set <index> to switch.")
            return "\n".join(lines)

        sub = args[0].lower()
        if sub == "set":
            if len(args) < 2:
                return "Usage: /market set <index>"
            try:
                idx = int(args[1])
            except ValueError:
                return "Index must be a number."
            market = self._config_guard.set_active_market(idx)
            if market is None:
                return f"Invalid index. Use /market to list markets."
            self._notify_change(f"Active market switched to: {market.display()}")
            return f"✅ Active market: {_esc(market.display())}"
        if sub == "add":
            if len(args) < 2:
                return "Usage: /market add <token_id> [label]"
            token_id = args[1]
            label = " ".join(args[2:]) if len(args) > 2 else ""
            market = self._config_guard.add_or_switch_market(token_id, label)
            self._notify_change(
                f"Market added/switched: {market.display()} ({token_id})"
            )
            return f"✅ Active market: {_esc(market.display())}\ntoken: <code>{_esc(token_id)}</code>"
        return f"Unknown /market subcommand: {sub}\nUse /market set or /market add."

    def _set_price(self, field: str, args: list[str], label: str) -> str:
        if not args:
            return f"Usage: /{('price' if field=='entry_price' else 'exit_price')} <0-1>"
        value = _parse_price(args[0])
        if value is None:
            return "Price must be a number between 0 and 1."
        self._config_guard.update(**{field: value})
        self._notify_change(f"{label} set to {value}")
        return f"✅ {label} = {value}"

    def _set_pct(self, field: str, args: list[str], label: str) -> str:
        if not args:
            return f"Usage: /{('takeprofit' if field=='take_profit_pct' else 'stoploss')} <pct|off>"
        raw = args[0].lower()
        if raw in ("off", "none", "0"):
            clear = "clear_tp" if field == "take_profit_pct" else "clear_sl"
            self._config_guard.update(**{clear: True})
            self._notify_change(f"{label} disabled")
            return f"✅ {label} disabled"
        frac = _parse_pct(args[0])
        if frac is None:
            return "Value must be a number (e.g. 10 = 10%)."
        self._config_guard.update(**{field: frac})
        self._notify_change(f"{label} set to {frac:.2%}")
        return f"✅ {label} = {frac:.2%}"

    def _set_amount(self, args: list[str]) -> str:
        if not args:
            return "Usage: /amount <n>"
        try:
            value = float(args[0])
        except ValueError:
            return "Amount must be a number."
        if value <= 0:
            return "Amount must be positive."
        self._config_guard.update(share_amount=value)
        self._notify_change(f"Order size set to {value}")
        return f"✅ Order size = {value} shares"

    def _set_conditional_entry(self, args: list[str]) -> str:
        if not args:
            snap = self._status_provider()
            t = snap.get("trading_params")
            current = t.conditional_entry if t else True
            return f"Conditional entry is currently: {'ON' if current else 'OFF'}\nUse /conditional_entry on|off to toggle."
        raw = args[0].lower()
        if raw in ("on", "true", "1", "yes"):
            new_mode = True
        elif raw in ("off", "false", "0", "no"):
            new_mode = False
        else:
            return "Usage: /conditional_entry on|off"
        self._config_guard.update(conditional_entry=new_mode)
        label = "ON (wait for best_ask ≤ entry_price)" if new_mode else "OFF (place order immediately)"
        self._notify_change(f"Conditional entry set to {label}")
        return f"✅ Conditional entry = {label}"

    # ------------------------------------------------------------------ #
    # Telegram HTTP helpers
    # ------------------------------------------------------------------ #
    def _notify_change(self, summary: str) -> None:
        """Push a config-change notification through the notifier manager.

        The notifier is attached by the runtime after construction.
        """
        notifier = getattr(self, "notifier", None)
        if notifier is not None:
            try:
                notifier.notify_config_change(summary)
            except Exception as exc:  # noqa: BLE001
                print(f"[bot] config-change notification failed: {exc}")

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        url = f"{TELEGRAM_API}/bot{self._token}/{method}"
        try:
            resp = requests.post(url, json=payload, timeout=self._timeout)
            if resp.status_code != 200:
                print(f"[bot] {method} failed ({resp.status_code}): {resp.text[:200]}")
                return None
            return resp.json()
        except requests.RequestException as exc:
            print(f"[bot] {method} error: {exc}")
            return None

    def _send(self, chat_id: int | str, text: str) -> None:
        self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _parse_price(raw: str) -> float | None:
    try:
        v = float(raw)
    except ValueError:
        return None
    if v < 0 or v > 1:
        return None
    return v


def _parse_pct(raw: str) -> float | None:
    try:
        v = float(raw)
    except ValueError:
        return None
    if v <= 0:
        return None
    # 10 -> 0.10 ; 0.10 -> 0.10
    return v / 100.0 if v >= 1 else v


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _p(v) -> str:
    try:
        return f"{float(v):.4f}"
    except (TypeError, ValueError):
        return str(v) if v is not None else "n/a"


def _pc(v) -> str:
    if v is None:
        return "off"
    try:
        return f"{float(v):.2%}"
    except (TypeError, ValueError):
        return str(v)
