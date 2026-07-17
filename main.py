"""
Polymarket CLOB Trading Bot - entry point.

Wires together: config -> CLOB client -> notifier manager -> runtime loop ->
interactive Telegram command bot. Runs until interrupted (Ctrl-C / SIGTERM).

Mirrors the original ``node index.js`` entry point but with a real polling
loop instead of a single mock cycle.
"""

from __future__ import annotations

import signal
import sys

import trading
from bot import TelegramCommandBot
from config import Config, ConfigGuard
from notifications import NotifierManager
from runtime import BotRuntime


def main() -> int:
    print("Starting Polymarket Trading Bot (Python)...")

    config = Config.load()

    if not config.private_key:
        # The original JS bot exits here; we do the same for trading mode.
        # (Notifications / interactive bot could still run read-only, but a
        # trading bot without a key is not useful.)
        print("Error: PRIVATE_KEY not found in .env")
        return 1

    if not config.trading.markets:
        print("Warning: no market configured. Set TOKEN_ID / MARKETS in .env or")
        print("         use the /market add <token_id> command once the bot is running.")

    # CLOB client -----------------------------------------------------------
    try:
        client = trading.init_client(config)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: failed to initialise CLOB client: {exc}")
        return 1

    # Config guard + notifications -----------------------------------------
    guard = ConfigGuard(config)
    notifier = NotifierManager(config)
    print(
        f"[main] notifications: telegram={notifier.telegram_targets} bot(s), "
        f"webhook={notifier.webhook_targets} url(s)"
    )

    # Runtime loop ----------------------------------------------------------
    runtime = BotRuntime(client, guard, notifier)

    # Interactive Telegram command bot -------------------------------------
    command_bot: TelegramCommandBot | None = None
    if config.has_interactive_bot():
        command_bot = TelegramCommandBot(
            token=config.telegram_interactive_token,
            config_guard=guard,
            status_provider=runtime.status_snapshot,
            allowed_user_ids=config.telegram_allowed_user_ids,
        )
        command_bot.notifier = notifier  # enables config-change push notifications
        command_bot.start()
        print("[main] interactive Telegram bot started")
    else:
        print("[main] interactive Telegram bot disabled (no TELEGRAM_INTERACTIVE_TOKEN)")

    # Start the runtime loop in a background thread ------------------------
    runtime.start()
    print(f"[main] runtime loop started (poll interval {config.poll_interval}s)")

    # Graceful shutdown ----------------------------------------------------
    stop = False

    def _handle_signal(signum, _frame):
        nonlocal stop
        print(f"\n[main] received signal {signum}; shutting down...")
        stop = True
        runtime.stop()
        if command_bot:
            command_bot.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    print("[main] bot is running. Press Ctrl-C to stop.")
    import time

    while not stop:
        # Short sleeps so signal handlers can flip `stop` promptly on any OS.
        time.sleep(0.5)

    print("[main] stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
