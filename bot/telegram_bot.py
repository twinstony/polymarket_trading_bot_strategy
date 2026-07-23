"""
Interactive Telegram bot for runtime control (asyncio + multi-strategy).

Listens for commands via long-polling ``getUpdates`` and replies with
``sendMessage``. All command handlers are async; the HTTP calls to the
Telegram API are wrapped with ``asyncio.to_thread`` so they never block the
event loop.

Commands manage multiple strategy instances via RuntimeManager:
* /status          - show all strategies' status & positions
* /strategy list   - list all configured strategies
* /strategy add <token_id> <entry> <exit> <amount> [label]
* /strategy remove <strategy_id>
* /strategy enable <strategy_id>
* /strategy disable <strategy_id>
* /price <strategy_id> <0-1>
* /exit_price <strategy_id> <0-1>
* /amount <strategy_id> <n>
* /enter <strategy_id>   - force BUY on one strategy
* /close <strategy_id>   - place SELL to close one strategy's position
* /enter_all  /close_all
* /triggermode on|off
* /takeprofit <pct|off>  /stoploss <pct|off>

Access can be restricted to a whitelist of Telegram user ids.
"""

from __future__ import annotations

import asyncio
from typing import Any

import requests

TELEGRAM_API = "https://api.telegram.org"


class TelegramCommandBot:
    def __init__(
        self,
        token: str,
        config_guard,
        runtime_manager,
        allowed_user_ids: set[int] | None = None,
        timeout: float = 35.0,
    ):
        self._token = token
        self._config_guard = config_guard
        self._runtime_manager = runtime_manager
        self._allowed = allowed_user_ids or set()
        self._timeout = timeout
        self._offset = 0
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        me = await self._call_async("getMe", {})
        if me and me.get("ok"):
            bot_user = me["result"].get("username", "?")
            print(f"[bot] Telegram command bot online as @{bot_user}")
        self._task = asyncio.create_task(self._poll_loop(), name="tg-cmd")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------ #
    # Polling (async)
    # ------------------------------------------------------------------ #
    async def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                data = await self._call_async(
                    "getUpdates", {"offset": self._offset, "timeout": 30}
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[bot] getUpdates error: {exc}; retrying")
                await asyncio.sleep(5)
                continue
            if not data or not data.get("ok"):
                await asyncio.sleep(2)
                continue
            for update in data.get("result", []):
                self._offset = update.get("update_id", self._offset) + 1
                await self._handle_update(update)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not message:
            return
        chat_id = message.get("chat", {}).get("id")
        if chat_id is None:
            return
        user = message.get("from", {})
        user_id = user.get("id")
        if self._allowed and user_id not in self._allowed:
            await self._send(chat_id, "⛔ You are not authorised to control this bot.")
            return
        text = (message.get("text") or "").strip()
        if not text:
            return
        reply = await self._dispatch_command(text)
        if reply:
            await self._send(chat_id, reply)

    # ------------------------------------------------------------------ #
    # Command dispatch (async)
    # ------------------------------------------------------------------ #
    async def _dispatch_command(self, text: str) -> str:
        parts = text.split()
        if not parts:
            return ""
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("/start", "/help"):
            return self._help()
        if cmd == "/status":
            return self._status()
        if cmd == "/strategy":
            return await self._strategy(args)
        if cmd == "/price":
            return await self._set_strategy_price("entry_price", args, "Buy (entry) price")
        if cmd == "/exit_price":
            return await self._set_strategy_price("exit_price", args, "Exit price")
        if cmd == "/amount":
            return await self._set_strategy_amount(args)
        if cmd == "/enter":
            return await self._enter(args)
        if cmd == "/close":
            return await self._close(args)
        if cmd == "/enter_all":
            return await self._enter_all()
        if cmd == "/close_all":
            return await self._close_all()
        if cmd == "/triggermode":
            return self._set_conditional_entry(args)
        if cmd == "/takeprofit":
            return self._set_pct("take_profit_pct", args, "Take-profit")
        if cmd == "/stoploss":
            return self._set_pct("stop_loss_pct", args, "Stop-loss")
        return f"Unknown command: {cmd}\n\n{self._help()}"

    def _help(self) -> str:
        return (
            "<b>Polymarket Bot commands (multi-strategy)</b>\n"
            "\n<b>Status</b>\n"
            "/status - show all strategies' status &amp; positions\n"
            "\n<b>Strategy management</b>\n"
            "/strategy list - list all strategies\n"
            "/strategy add &lt;token_id&gt; &lt;entry&gt; &lt;exit&gt; &lt;amount&gt; [label]\n"
            "/strategy remove &lt;strategy_id&gt;\n"
            "/strategy enable &lt;strategy_id&gt;\n"
            "/strategy disable &lt;strategy_id&gt;\n"
            "\n<b>Per-strategy actions</b>\n"
            "/price &lt;strategy_id&gt; &lt;0-1&gt; - set entry price\n"
            "/exit_price &lt;strategy_id&gt; &lt;0-1&gt; - set exit price\n"
            "/amount &lt;strategy_id&gt; &lt;n&gt; - set order size\n"
            "/enter &lt;strategy_id&gt; - force BUY on one strategy\n"
            "/close &lt;strategy_id&gt; - place SELL to close position\n"
            "/enter_all - force BUY on all strategies\n"
            "/close_all - close positions on all strategies\n"
            "\n<b>Global settings</b>\n"
            "/triggermode on|off - conditional entry (on=wait for ask ≤ price)\n"
            "/takeprofit &lt;pct|off&gt; - global take-profit\n"
            "/stoploss &lt;pct|off&gt; - global stop-loss"
        )

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #
    def _status(self) -> str:
        snapshots = self._runtime_manager.status_snapshot_all()
        t = self._config_guard.snapshot()
        lines = [
            "<b>Strategy Status</b>",
            f"Strategies running: {len(snapshots)}",
            f"Conditional entry: {'ON' if t.conditional_entry else 'OFF'}",
        ]
        if not snapshots:
            lines.append("  (no strategies running)")
        else:
            for snap in snapshots:
                label = _esc(snap.get("label", "?"))
                size = _p(snap.get("position_size"))
                avg = _p(snap.get("position_avg_price"))
                entry = _p(snap.get("entry_price"))
                exit_p = _p(snap.get("exit_price"))
                lines.append(
                    f"  [{snap['strategy_id']}] {label}\n"
                    f"    pos={size}@{avg} entry={entry} exit={exit_p} "
                    f"attempted={'yes' if snap.get('entry_attempted') else 'no'}"
                )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Strategy management
    # ------------------------------------------------------------------ #
    async def _strategy(self, args: list[str]) -> str:
        if not args:
            return self._strategy_list()

        sub = args[0].lower()
        if sub == "list":
            return self._strategy_list()
        if sub == "add":
            return await self._strategy_add(args[1:])
        if sub == "remove":
            return await self._strategy_remove(args[1:])
        if sub == "enable":
            return await self._strategy_enable(args[1:], True)
        if sub == "disable":
            return await self._strategy_enable(args[1:], False)
        return f"Unknown /strategy subcommand: {sub}\nUse list, add, remove, enable, disable."

    def _strategy_list(self) -> str:
        strategies = self._runtime_manager.list_strategies()
        if not strategies:
            return "No strategies configured."
        lines = ["<b>Configured strategies</b>"]
        for s in strategies:
            running = "running" if s["running"] else "stopped"
            enabled = "enabled" if s["enabled"] else "disabled"
            lines.append(
                f"  [{s['strategy_id']}] {_esc(s['label'])}\n"
                f"    token=...{s['token_id'][-8:]} entry={_p(s['entry_price'])} "
                f"exit={_p(s['exit_price'])} size={_p(s['share_amount'])} "
                f"{enabled}/{running}"
            )
        return "\n".join(lines)

    async def _strategy_add(self, args: list[str]) -> str:
        if len(args) < 4:
            return "Usage: /strategy add <token_id> <entry> <exit> <amount> [label]"
        token_id = args[0]
        entry = _parse_price(args[1])
        exit_p = _parse_price(args[2])
        if entry is None or exit_p is None:
            return "Entry and exit prices must be numbers between 0 and 1."
        try:
            amount = float(args[3])
        except ValueError:
            return "Amount must be a number."
        if amount <= 0:
            return "Amount must be positive."
        label = " ".join(args[4:]) if len(args) > 4 else ""
        from config import Strategy
        strat = Strategy(
            token_id=token_id,
            label=label,
            entry_price=entry,
            exit_price=exit_p,
            share_amount=amount,
            enabled=True,
        )
        result = await self._runtime_manager.add_strategy(strat)
        return f"✅ {result}"

    async def _strategy_remove(self, args: list[str]) -> str:
        if not args:
            return "Usage: /strategy remove <strategy_id>"
        result = await self._runtime_manager.remove_strategy(args[0])
        return f"✅ {result}"

    async def _strategy_enable(self, args: list[str], enabled: bool) -> str:
        if not args:
            return "Usage: /strategy enable|disable <strategy_id>"
        result = await self._runtime_manager.enable_strategy(args[0], enabled)
        return f"✅ {result}"

    # ------------------------------------------------------------------ #
    # Per-strategy price/amount
    # ------------------------------------------------------------------ #
    async def _set_strategy_price(self, field: str, args: list[str], label: str) -> str:
        cmd_name = "price" if field == "entry_price" else "exit_price"
        if len(args) < 2:
            return f"Usage: /{cmd_name} <strategy_id> <0-1>"
        strategy_id = args[0]
        value = _parse_price(args[1])
        if value is None:
            return "Price must be a number between 0 and 1."
        result = self._config_guard.update_strategy(strategy_id, **{field: value})
        if result is None:
            return f"Strategy not found: {strategy_id}"
        return f"✅ {label} for {result.display()} = {value}"

    async def _set_strategy_amount(self, args: list[str]) -> str:
        if len(args) < 2:
            return "Usage: /amount <strategy_id> <n>"
        strategy_id = args[0]
        try:
            value = float(args[1])
        except ValueError:
            return "Amount must be a number."
        if value <= 0:
            return "Amount must be positive."
        result = self._config_guard.update_strategy(strategy_id, share_amount=value)
        if result is None:
            return f"Strategy not found: {strategy_id}"
        return f"✅ Order size for {result.display()} = {value} shares"

    # ------------------------------------------------------------------ #
    # Enter / close
    # ------------------------------------------------------------------ #
    async def _enter(self, args: list[str]) -> str:
        if not args:
            return "Usage: /enter <strategy_id>"
        result = await self._runtime_manager.strategy_enter(args[0])
        return f"✅ {result}"

    async def _close(self, args: list[str]) -> str:
        if not args:
            return "Usage: /close <strategy_id>"
        result = await self._runtime_manager.strategy_close(args[0])
        return f"✅ {result}"

    async def _enter_all(self) -> str:
        result = await self._runtime_manager.strategy_enter_all()
        return f"✅ {result}"

    async def _close_all(self) -> str:
        result = await self._runtime_manager.strategy_close_all()
        return f"✅ {result}"

    # ------------------------------------------------------------------ #
    # Global settings
    # ------------------------------------------------------------------ #
    def _set_conditional_entry(self, args: list[str]) -> str:
        if not args:
            t = self._config_guard.snapshot()
            current = t.conditional_entry
            return f"Conditional entry is currently: {'ON' if current else 'OFF'}\nUse /triggermode on|off to toggle."
        raw = args[0].lower()
        if raw in ("on", "true", "1", "yes"):
            new_mode = True
        elif raw in ("off", "false", "0", "no"):
            new_mode = False
        else:
            return "Usage: /triggermode on|off"
        self._config_guard.update(conditional_entry=new_mode)
        label = "ON (wait for best_ask ≤ entry_price)" if new_mode else "OFF (place order immediately)"
        return f"✅ Conditional entry = {label}"

    def _set_pct(self, field: str, args: list[str], label: str) -> str:
        if not args:
            cmd = "takeprofit" if field == "take_profit_pct" else "stoploss"
            return f"Usage: /{cmd} <pct|off>"
        raw = args[0].lower()
        if raw in ("off", "none", "0"):
            clear = "clear_tp" if field == "take_profit_pct" else "clear_sl"
            self._config_guard.update(**{clear: True})
            return f"✅ {label} disabled"
        frac = _parse_pct(args[0])
        if frac is None:
            return "Value must be a number (e.g. 10 = 10%)."
        self._config_guard.update(**{field: frac})
        return f"✅ {label} = {frac:.2%}"

    # ------------------------------------------------------------------ #
    # Telegram HTTP helpers (async via to_thread)
    # ------------------------------------------------------------------ #
    async def _call_async(self, method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._call_sync, method, payload)

    def _call_sync(self, method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
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

    async def _send(self, chat_id: int | str, text: str) -> None:
        await self._call_async(
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
    return v / 100.0 if v >= 1 else v


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _p(v) -> str:
    try:
        return f"{float(v):.4f}"
    except (TypeError, ValueError):
        return str(v) if v is not None else "n/a"
