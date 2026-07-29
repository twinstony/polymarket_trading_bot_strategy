"""
Polymarket CLOB Trading Bot — asyncio entry point.

Flow:
  config (.env) → CLOB client → notifier → interactive CLI setup
  → RuntimeManager (one StrategyRuntime per strategy, concurrent)
  → graceful shutdown on SIGINT / SIGTERM.

Signal handling is Windows-compatible: ``loop.add_signal_handler`` is
preferred (Unix); on Windows it falls back to ``signal.signal`` with
``loop.call_soon_threadsafe`` to safely wake the event loop.
"""

from __future__ import annotations

import asyncio
import signal
import sys

import trading
from bridge import create_bridge_app
from config import Config, StrategyConfig
from market_setup import run_interactive_setup
from notifications import NotifierManager
from runtime import RuntimeManager
from telegram_bot import TelegramCommandBot
from uvicorn import Config as UvicornConfig, Server


def main() -> int:
    print("Starting Polymarket Trading Bot (asyncio, multi-strategy)...")

    config = Config.load()

    if not config.private_key:
        print("Error: PRIVATE_KEY not found in .env")
        return 1

    # CLOB client -----------------------------------------------------------
    try:
        client = trading.init_client(config)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: failed to initialise CLOB client: {exc}")
        return 1

    # Notifications ---------------------------------------------------------
    notifier = NotifierManager(config)
    print(
        f"[main] notifications: telegram={notifier.telegram_targets} bot(s), "
        f"webhook={notifier.webhook_targets} url(s)"
    )

    # Interactive CLI setup (blocking, before the event loop) ---------------
    strategies = run_interactive_setup(config)
    if not strategies:
        if not config.bridge_enabled:
            print("[main] 未配置任何策略，退出")
            return 1
        print("[main] 未配置策略，但 bridge 已启用，以空 RuntimeManager 启动（仅供扩展连接测试）")
    else:
        print(f"\n[main] 已配置 {len(strategies)} 个策略:")
        for i, sc in enumerate(strategies, 1):
            print(f"  [{i}] {sc.label}  entry={sc.entry_price} exit={sc.exit_price} size={sc.share_amount}")

    # Run the asyncio event loop -------------------------------------------
    try:
        asyncio.run(_async_main(config, client, notifier, strategies))
    except KeyboardInterrupt:
        print("\n[main] interrupted by user")
    return 0


async def _async_main(
    config: Config,
    client,
    notifier: NotifierManager,
    strategies: list[StrategyConfig],
) -> None:
    manager = RuntimeManager(client, notifier, config)
    for sc in strategies:
        manager.add_strategy(sc)

    await manager.start_all()

    # Graceful shutdown via signal -----------------------------------------
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        stop.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows: add_signal_handler not implemented — fall back to
            # signal.signal, dispatching back into the loop thread-safely.
            signal.signal(sig, lambda _s, _f: loop.call_soon_threadsafe(_request_stop))

    print(
        f"[main] runtime started (poll={config.poll_interval}s, "
        f"conditional_entry={config.conditional_entry})"
    )
    print("[main] bot is running. Press Ctrl-C to stop.")

    # Optional FastAPI bridge (localhost only, embeds in this event loop) --
    if config.bridge_enabled:
        app = create_bridge_app(manager, config, client)
        uconfig = UvicornConfig(
            app, host=config.bridge_host, port=config.bridge_port, log_level="info"
        )
        server = Server(uconfig)
        # 启动 bridge 与 stop.wait() 并行
        bridge_task = asyncio.create_task(server.serve())
        print(f"[bridge] serving on http://{config.bridge_host}:{config.bridge_port}")
    else:
        bridge_task = None
        print("[main] bridge disabled (BRIDGE_ENABLED not set)")

    # Optional Telegram interactive command bot (asyncio long-poll) ---------
    tg_bot: TelegramCommandBot | None = None
    tg_task: asyncio.Task | None = None
    if config.telegram_interactive_token:
        tg_bot = TelegramCommandBot(
            token=config.telegram_interactive_token,
            manager=manager,
            client=client,
            config=config,
            allowed_user_ids=config.telegram_allowed_user_ids,
        )
        tg_task = asyncio.create_task(tg_bot.run(), name="telegram-command-bot")
        print(
            f"[telegram_bot] interactive bot started "
            f"(allowed_users={len(config.telegram_allowed_user_ids) or 'any'})"
        )
    else:
        print("[main] telegram interactive bot disabled (TELEGRAM_INTERACTIVE_TOKEN not set)")

    await stop.wait()

    print("\n[main] shutting down...")
    if tg_bot is not None:
        tg_bot.stop()
    await manager.stop_all()
    if tg_task is not None:
        await tg_task
        print("[telegram_bot] stopped")
    if bridge_task is not None:
        print("[bridge] stopping...")
        server.should_exit = True
        await bridge_task
        print("[bridge] stopped")
    print("[main] stopped.")


if __name__ == "__main__":
    sys.exit(main())
