"""
Polymarket CLOB Trading Bot - entry point (asyncio multi-strategy).

Wires together: config -> CLOB client -> notifier manager -> RuntimeManager
(multiple StrategyRuntime tasks) -> interactive Telegram command bot.
Runs an asyncio event loop until interrupted (Ctrl-C / SIGTERM).
"""

from __future__ import annotations

import asyncio
import signal
import sys

import trading
from bot import TelegramCommandBot
from config import Config, ConfigGuard
from market_setup import run_interactive_setup
from notifications import NotifierManager
from runtime_manager import RuntimeManager


async def async_main() -> int:
    print("Starting Polymarket Trading Bot (Python asyncio multi-strategy)...")

    config = Config.load()

    if not config.private_key:
        print("Error: PRIVATE_KEY not found in .env")
        return 1

    if not config.trading.strategies:
        print("Warning: no strategy configured. Use the interactive setup below")
        print("         or the /strategy add command once the bot is running.")

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

    # 交互式市场配置（启动时选择市场与参数，回车跳过使用 .env 现有配置）-------
    if not run_interactive_setup(config, guard):
        print("[main] 未完成市场配置，退出")
        return 1

    # RuntimeManager (manages multiple StrategyRuntime asyncio tasks) --------
    manager = RuntimeManager(client, guard, notifier)

    # Interactive Telegram command bot -------------------------------------
    command_bot: TelegramCommandBot | None = None
    if config.has_interactive_bot():
        command_bot = TelegramCommandBot(
            token=config.telegram_interactive_token,
            config_guard=guard,
            runtime_manager=manager,
            allowed_user_ids=config.telegram_allowed_user_ids,
        )
        await command_bot.start()
        print("[main] interactive Telegram bot started")
    else:
        print("[main] interactive Telegram bot disabled (no TELEGRAM_INTERACTIVE_TOKEN)")

    # Start all strategy runtimes ------------------------------------------
    await manager.start_all()
    print(f"[main] runtime started (poll interval {config.poll_interval}s)")

    # Graceful shutdown via asyncio Event ----------------------------------
    stop_event = asyncio.Event()

    def _handle_signal(signum, _frame):
        print(f"\n[main] received signal {signum}; shutting down...")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _handle_signal, sig, None)
        except (NotImplementedError, RuntimeError):
            # Windows doesn't support add_signal_handler; fallback to signal.signal
            signal.signal(sig, _handle_signal)

    print("[main] bot is running. Press Ctrl-C to stop.")

    # Wait for stop signal
    await stop_event.wait()

    # Shutdown: stop telegram, stop all runtimes ----------------------------
    if command_bot:
        await command_bot.stop()
    await manager.stop_all()
    print("[main] stopped.")
    return 0


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\n[main] interrupted.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
