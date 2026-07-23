"""
RuntimeManager: manages the lifecycle of multiple StrategyRuntime coroutines.

Responsibilities:
* Start one StrategyRuntime task per enabled strategy from config.
* Stop all tasks on shutdown.
* Dynamically add/remove strategies at runtime (via Telegram commands).
* Aggregate status snapshots from all instances.
* Provide public action methods (enter/close/enable/disable) for Telegram.
"""

from __future__ import annotations

import asyncio
from typing import Any

from config import ConfigGuard, Strategy
from strategy_runtime import StrategyRuntime


class RuntimeManager:
    """Manages multiple StrategyRuntime asyncio tasks."""

    def __init__(self, client, config_guard: ConfigGuard, notifier):
        self._client = client
        self._guard = config_guard
        self._notifier = notifier
        # Global lock for serializing order placement across all strategies
        self._order_lock = asyncio.Lock()
        # strategy_id -> StrategyRuntime
        self._runtimes: dict[str, StrategyRuntime] = {}
        # strategy_id -> asyncio.Task
        self._tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start_all(self) -> None:
        """Create and start a StrategyRuntime for every enabled strategy."""
        strategies = self._guard.list_enabled()
        for strat in strategies:
            await self._start_one(strat)
        print(f"[manager] started {len(self._runtimes)} strategy runtime(s)")

    async def stop_all(self) -> None:
        """Stop all runtime tasks gracefully."""
        for rt in self._runtimes.values():
            rt.stop()
        # Cancel tasks that don't stop within timeout
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runtimes.clear()
        self._tasks.clear()
        print("[manager] all strategy runtimes stopped")

    async def _start_one(self, strat: Strategy) -> StrategyRuntime | None:
        """Create and start a single StrategyRuntime. Returns it or None on error."""
        if not strat.token_id or not strat.strategy_id:
            return None
        if strat.strategy_id in self._runtimes:
            return self._runtimes[strat.strategy_id]
        rt = StrategyRuntime(
            strategy=strat,
            client=self._client,
            config_guard=self._guard,
            notifier=self._notifier,
            order_lock=self._order_lock,
        )
        self._runtimes[strat.strategy_id] = rt
        self._tasks[strat.strategy_id] = asyncio.create_task(
            rt.run(), name=f"strategy:{strat.strategy_id}"
        )
        return rt

    async def _stop_one(self, strategy_id: str) -> bool:
        """Stop a single runtime by strategy_id. Returns True if found."""
        rt = self._runtimes.pop(strategy_id, None)
        task = self._tasks.pop(strategy_id, None)
        if rt:
            rt.stop()
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            return True
        return False

    # ------------------------------------------------------------------ #
    # Dynamic add / remove
    # ------------------------------------------------------------------ #
    async def add_strategy(self, strat: Strategy) -> str:
        """Add a strategy to config and start its runtime."""
        self._guard.add_strategy(strat)
        rt = await self._start_one(strat)
        if rt:
            return f"Strategy added and started: {strat.display()} (id={strat.strategy_id})"
        return f"Strategy added to config but runtime not started: {strat.display()}"

    async def remove_strategy(self, strategy_id: str) -> str:
        """Stop a runtime and remove its strategy from config."""
        found = await self._stop_one(strategy_id)
        removed = self._guard.remove_strategy(strategy_id)
        if found or removed:
            return f"Strategy removed: {strategy_id}"
        return f"Strategy not found: {strategy_id}"

    async def enable_strategy(self, strategy_id: str, enabled: bool) -> str:
        """Enable/disable a strategy. If enabling and not running, start it."""
        strat = self._guard.update_strategy(strategy_id, enabled=enabled)
        if strat is None:
            return f"Strategy not found: {strategy_id}"
        if enabled:
            if strategy_id not in self._runtimes:
                await self._start_one(strat)
            state = "enabled"
        else:
            # Stop the runtime when disabling
            await self._stop_one(strategy_id)
            state = "disabled"
        return f"Strategy {strat.display()} {state}"

    # ------------------------------------------------------------------ #
    # Public actions (called by Telegram)
    # ------------------------------------------------------------------ #
    async def strategy_enter(self, strategy_id: str) -> str:
        rt = self._runtimes.get(strategy_id)
        if rt is None:
            return f"Strategy not running: {strategy_id}"
        return await rt.strategy_enter()

    async def strategy_close(self, strategy_id: str) -> str:
        rt = self._runtimes.get(strategy_id)
        if rt is None:
            return f"Strategy not running: {strategy_id}"
        return await rt.strategy_close()

    async def strategy_enter_all(self) -> str:
        """Force entry on all running strategies."""
        results = []
        for sid, rt in self._runtimes.items():
            results.append(await rt.strategy_enter())
        return "\n".join(results) if results else "No strategies running"

    async def strategy_close_all(self) -> str:
        """Close positions on all running strategies."""
        results = []
        for sid, rt in self._runtimes.items():
            results.append(await rt.strategy_close())
        return "\n".join(results) if results else "No strategies running"

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #
    def status_snapshot_all(self) -> list[dict[str, Any]]:
        """Aggregate status from all running runtimes (sync, for Telegram)."""
        return [rt.status_snapshot() for rt in self._runtimes.values()]

    def list_strategies(self) -> list[dict[str, Any]]:
        """List all configured strategies with their runtime status."""
        strategies = self._guard.list_strategies()
        result = []
        for s in strategies:
            running = s.strategy_id in self._runtimes
            result.append({
                "strategy_id": s.strategy_id,
                "token_id": s.token_id,
                "label": s.display(),
                "outcome_name": s.outcome_name,
                "entry_price": s.entry_price,
                "exit_price": s.exit_price,
                "share_amount": s.share_amount,
                "enabled": s.enabled,
                "running": running,
            })
        return result
