"""
StrategyRuntime: a single-strategy asyncio runtime for one outcome token.

Each StrategyRuntime instance manages exactly one token_id with its own
independent state (positions, order IDs, baselines). Multiple instances run
concurrently as asyncio tasks. A "dual-sided bet" is simply two
StrategyRuntime instances (one per outcome token).

All CLOB SDK calls are wrapped with asyncio.to_thread to avoid blocking the
event loop. Order placement is serialized via a shared global asyncio.Lock
to prevent signature races in the CLOB SDK.
"""

from __future__ import annotations

import asyncio
from typing import Any

import trading
from config import ConfigGuard, Strategy
from strategy import PositionData, should_enter, should_exit


class StrategyRuntime:
    """Async runtime for a single trading strategy (one token)."""

    def __init__(
        self,
        strategy: Strategy,
        client,
        config_guard: ConfigGuard,
        notifier,
        order_lock: asyncio.Lock,
    ):
        self._strategy_id = strategy.strategy_id
        self._token_id = strategy.token_id
        self._label = strategy.display()
        self._client = client
        self._guard = config_guard
        self._notifier = notifier
        self._order_lock = order_lock  # global lock for serializing order placement

        # Independent per-instance state (no sharing between strategies)
        self._position: dict[str, Any] = {"size": 0.0, "avg_price": 0.0, "label": self._label}
        self._seen_trade_ids: set[str] = set()
        self._entry_attempted: bool = False
        self._entry_order_ids: set[str] = set()
        self._exit_order_ids: set[str] = set()
        self._entry_position_baseline: tuple[float, float] | None = None
        self._exit_order_baseline: float | None = None
        self._failed_exit_attempts: int = 0
        self._exit_backoff_threshold = 2
        self._open_orders_sig: str = ""
        self._cycle = 0

        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    @property
    def token_id(self) -> str:
        return self._token_id

    async def run(self) -> None:
        """Main loop: cycle every poll_interval seconds until stopped."""
        print(f"[runtime:{self._strategy_id}] started for {self._label}")
        while not self._stop_event.is_set():
            try:
                await self._cycle_once()
            except Exception as exc:  # noqa: BLE001
                print(f"[runtime:{self._strategy_id}] cycle error: {exc}")
            # Read poll_interval fresh each cycle (supports hot-update)
            interval = max(1, self._guard.config.poll_interval)
            status_every = max(1, self._guard.config.status_every_cycles)
            self._cycle += 1
            if self._cycle % status_every == 0:
                self._log_heartbeat()
                await self._push_status()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass  # interval elapsed, continue loop
        print(f"[runtime:{self._strategy_id}] stopped")

    def _log_heartbeat(self) -> None:
        """周期性终端心跳日志，展示策略详情与当前状态（不依赖 notifier）。

        状态生命周期：
          待入场 → 已挂买单(待成交) → 持仓中(已挂保护卖单) / 持仓中(待挂卖单)
                  → 已平仓(卖单成交) → 待入场（下一轮可重新挂买单）
        """
        pos = self._position
        strat = self._guard.snapshot().get_strategy(self._strategy_id)
        if strat is None:
            print(f"[runtime:{self._strategy_id}] cycle={self._cycle} — strategy removed")
            return

        label = strat.display()
        size = pos.get("size", 0.0)
        avg = pos.get("avg_price", 0.0)
        tp = f"{strat.take_profit_pct:.0%}" if strat.take_profit_pct is not None else "关闭"
        sl = f"{strat.stop_loss_pct:.0%}" if strat.stop_loss_pct is not None else "关闭"
        has_pos = size > 0
        has_exit_order = bool(self._exit_order_ids)

        if has_pos:
            # 买单已成交，持仓中
            pos_str = f"{size}@{avg:.3f}"
            if has_exit_order:
                status = f"持仓中(已挂卖单保护) {pos_str}"
            else:
                status = f"持仓中(待挂卖单) {pos_str}"
            tp_trigger = f"tp触发价≈{avg * (1 + (strat.take_profit_pct or 0)):.3f}" if strat.take_profit_pct else ""
            sl_trigger = f"sl触发价≈{avg * (1 - (strat.stop_loss_pct or 0)):.3f}" if strat.stop_loss_pct else ""
            triggers = " ".join(x for x in (tp_trigger, sl_trigger) if x)
            print(
                f"[{label}] cycle={self._cycle} {status} | "
                f"entry={strat.entry_price} exit={strat.exit_price} tp={tp} sl={sl} {triggers}"
            )
        else:
            # 无持仓：待入场 / 已挂买单 / 已平仓
            if self._entry_attempted and self._exit_order_ids:
                # 买过且卖过，卖单已成交
                status = "已平仓(卖单已成交)"
            elif self._entry_attempted:
                # 买过但当前无持仓，卖单未成交或未挂
                status = "已挂买单(待成交)"
            else:
                status = "待入场"
            print(
                f"[{label}] cycle={self._cycle} {status} | "
                f"entry={strat.entry_price} exit={strat.exit_price} tp={tp} sl={sl} "
                f"size={strat.share_amount}"
            )

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------ #
    # Single cycle
    # ------------------------------------------------------------------ #
    async def _cycle_once(self) -> None:
        if not self._guard.config.trading_enabled:
            return

        t = self._guard.snapshot()
        strat = t.get_strategy(self._strategy_id)
        if strat is None or not strat.enabled:
            return

        # 1. Detect fills
        if not await self._detect_fills():
            return

        # 2. Reconcile against portfolio
        await self._reconcile_position_from_portfolio(strat)

        # 3. If holding, check tp/sl then ensure protective SELL
        position = self._get_position()
        if position.has_position:
            market_data = await trading.get_market_data_async(self._client, self._token_id)
            if self._check_tp_sl_exit(position, market_data, strat):
                await self._emergency_exit(strat, market_data)
                return
            await self._ensure_exit_order(strat)
            return

        # 4. Entry check (only when not holding)
        if strat.exit_price is None:
            return
        has_entry = await self._has_matching_open_order(
            "BUY", price=strat.entry_price, size=strat.share_amount
        )
        if has_entry is None:
            print(f"[runtime:{self._strategy_id}] entry paused — could not confirm open orders")
        elif has_entry:
            pass  # matching BUY already resting
        elif self._entry_attempted:
            pass  # already attempted
        else:
            market_data = await trading.get_market_data_async(self._client, self._token_id)
            if t.conditional_entry:
                if should_enter(market_data, strat.entry_price):
                    await self._do_entry(strat)
            else:
                await self._do_entry(strat)

        # 5. Open-orders change notification
        await self._maybe_notify_open_orders()

    async def _do_entry(self, strat: Strategy) -> None:
        """Place a BUY order and record state."""
        if not await self._remember_layer_baseline(strat):
            print(f"[runtime:{self._strategy_id}] entry paused — baseline failed")
            return
        self._entry_attempted = True
        async with self._order_lock:
            resp = await trading.enter_position_async(
                self._client, self._token_id, "BUY", strat.share_amount, strat.entry_price
            )
        self._remember_order_id(resp, self._entry_order_ids)

    # ------------------------------------------------------------------ #
    # Fill detection
    # ------------------------------------------------------------------ #
    async def _detect_fills(self) -> bool:
        try:
            trades = await trading.get_trades_async(self._client, self._token_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[runtime:{self._strategy_id}] get_trades failed: {exc}")
            return False
        if not trades:
            return True

        first_run = not self._seen_trade_ids
        new_trades: list[dict[str, Any]] = []
        for tr in trades:
            tid = str(tr.get("id") or tr.get("trade_id") or tr.get("order_id") or "")
            if not tid:
                continue
            if tid in self._seen_trade_ids:
                continue
            self._seen_trade_ids.add(tid)
            if first_run:
                await self._apply_fill(tr)
            else:
                new_trades.append(tr)

        for tr in new_trades:
            await self._apply_fill(tr)
            if self._notifier and self._notifier.enabled:
                # notifier.notify_fill expects markets list; pass empty for now
                self._notifier.notify_fill(tr, [])
        return True

    async def _apply_fill(self, trade: dict[str, Any]) -> None:
        token_id = str(
            trade.get("asset_id") or trade.get("token_id") or trade.get("market") or ""
        )
        if not token_id or token_id != self._token_id:
            return
        side = str(trade.get("side", "")).upper()
        trade_size = _to_float(trade.get("size") or trade.get("matched_amount")) or 0.0
        price = _to_float(trade.get("price")) or 0.0
        display = self._label

        bot_fills = self._extract_bot_fills(trade)
        if bot_fills:
            for fill_side, fill_size, fill_price in bot_fills:
                await self._update_position(fill_side, fill_size, fill_price)
                print(
                    f"[runtime:{self._strategy_id}] applied bot fill "
                    f"({display} side={fill_side} size={fill_size} price={fill_price})"
                )
            return

        # order_id 精确匹配失败时，fallback 到 heuristic（参照 master 分支）
        if not await self._trade_matches_current_intent(trade, side, trade_size, price):
            print(
                f"[runtime:{self._strategy_id}] ignored fill (heuristic mismatch) "
                f"({display} side={side} size={trade_size} price={price})"
            )
            return

        await self._update_position(side, trade_size, price)

    def _extract_bot_fills(self, trade: dict[str, Any]) -> list[tuple[str, float, float]]:
        """Extract bot-specific fills from a trade by matching order IDs."""
        fills: list[tuple[str, float, float]] = []
        trade_price = _to_float(trade.get("price")) or 0.0

        taker_id = str(trade.get("taker_order_id") or "")
        if taker_id:
            taker_size = _to_float(
                trade.get("size") or trade.get("matched_amount")
            ) or 0.0
            if taker_id in self._entry_order_ids:
                fills.append(("BUY", taker_size, trade_price))
            elif taker_id in self._exit_order_ids:
                fills.append(("SELL", taker_size, trade_price))

        maker_orders = trade.get("maker_orders") or []
        if isinstance(maker_orders, list):
            for mo in maker_orders:
                if not isinstance(mo, dict):
                    continue
                mid = str(mo.get("order_id") or mo.get("orderID") or "")
                if not mid:
                    continue
                mo_size = _to_float(mo.get("size") or mo.get("matched_size")) or 0.0
                mo_price = _to_float(mo.get("price")) or trade_price
                if mid in self._entry_order_ids:
                    fills.append(("BUY", mo_size, mo_price))
                elif mid in self._exit_order_ids:
                    fills.append(("SELL", mo_size, mo_price))

        return fills

    # ------------------------------------------------------------------ #
    # Position management
    # ------------------------------------------------------------------ #
    def _get_position(self) -> PositionData:
        with _sync_lock_guard(self._lock):
            pos = self._position
            return PositionData(
                token_id=self._token_id,
                size=pos.get("size", 0.0),
                avg_price=pos.get("avg_price", 0.0),
            )

    async def _update_position(self, side: str, size: float, price: float) -> None:
        async with self._lock:
            pos = self._position
            if side.startswith("B"):
                old_size = pos["size"]
                old_avg = pos["avg_price"]
                new_size = old_size + size
                pos["avg_price"] = (
                    (old_avg * old_size + price * size) / new_size
                    if new_size > 0
                    else price
                )
                pos["size"] = new_size
            elif side.startswith("S"):
                pos["size"] = max(0.0, pos["size"] - size)

    async def _reconcile_position_from_portfolio(self, strat: Strategy) -> None:
        portfolio_position = await self._get_portfolio_position()
        if portfolio_position is None:
            return
        size, avg_price = portfolio_position

        async with self._lock:
            current_size = self._position.get("size", 0.0)

        if size < current_size - 0.05:
            async with self._lock:
                if size <= 0:
                    self._position = {"size": 0.0, "avg_price": 0.0, "label": self._label}
                else:
                    self._position = {"size": size, "avg_price": avg_price, "label": self._label}
            print(
                f"[runtime:{self._strategy_id}] portfolio reconciliation corrected "
                f"downward (local={current_size} -> portfolio={size})"
            )
            return

        if size <= 0:
            return

        managed_size = size
        managed_avg = avg_price
        if self._entry_position_baseline and self._entry_attempted:
            baseline_size, baseline_avg = self._entry_position_baseline
            if size <= baseline_size + 0.01:
                return
            managed_size = size - baseline_size
            managed_cost = max(0.0, size * avg_price - baseline_size * baseline_avg)
            managed_avg = managed_cost / managed_size if managed_size > 0 else strat.entry_price
        elif not self._portfolio_position_matches_intent(size, avg_price, strat):
            return

        async with self._lock:
            current_size = self._position.get("size", 0.0)
            if current_size >= managed_size:
                return
            self._position = {"size": managed_size, "avg_price": managed_avg, "label": self._label}
        print(
            f"[runtime:{self._strategy_id}] portfolio reconciliation detected "
            f"managed position ({managed_size} @ {managed_avg})"
        )

    def _portfolio_position_matches_intent(
        self, size: float, avg_price: float, strat: Strategy
    ) -> bool:
        if self._entry_attempted:
            return avg_price <= strat.entry_price + 0.02 and size >= 0.01
        return (
            avg_price <= strat.entry_price + 0.02
            and _close_enough(size, strat.share_amount, min_abs=0.05, rel=0.02)
        )

    async def _get_portfolio_position(self) -> tuple[float, float] | None:
        """Fetch position size and avg_price from Polymarket portfolio API."""
        try:
            positions = await asyncio.to_thread(
                self._client.get_balances, allow_negative=False
            )
        except Exception:
            return None
        if not isinstance(positions, list):
            return None
        for p in positions:
            if not isinstance(p, dict):
                continue
            if str(p.get("asset") or p.get("token_id") or "") != self._token_id:
                continue
            size = _to_float(p.get("size") or p.get("balance")) or 0.0
            avg_price = _to_float(p.get("avg_price") or p.get("average_price")) or 0.0
            return size, avg_price
        return None

    # ------------------------------------------------------------------ #
    # Take-profit / Stop-loss exit
    # ------------------------------------------------------------------ #
    def _check_tp_sl_exit(self, position: PositionData, market_data, strat: Strategy) -> bool:
        """检查止盈止损是否触发。仅当配置了 tp/sl 时才检查。"""
        if strat.take_profit_pct is None and strat.stop_loss_pct is None:
            return False
        return should_exit(
            position=position,
            market_data=market_data,
            exit_price=None,  # exit_price 由 _ensure_exit_order 处理，不在此检查
            take_profit_pct=strat.take_profit_pct,
            stop_loss_pct=strat.stop_loss_pct,
        )

    async def _emergency_exit(self, strat: Strategy, market_data) -> None:
        """tp/sl 触发时的紧急退出：取消现有 SELL 单，以当前 best_bid 挂 SELL。"""
        bid = market_data.best_bid
        if bid is None:
            print(f"[runtime:{self._strategy_id}] tp/sl triggered but no bid — cannot exit")
            return
        position = self._get_position()
        if not position.has_position:
            return
        # 取消该 token 的所有 SELL 单（exit_price 保护单）
        cancelled = await trading.cancel_orders_for_token_async(
            self._client, self._token_id, side="SELL"
        )
        if cancelled > 0:
            print(
                f"[runtime:{self._strategy_id}] cancelled {cancelled} SELL order(s) "
                f"for tp/sl exit"
            )
        # 以当前 best_bid 挂 SELL（应能立即成交）
        async with self._order_lock:
            resp = await trading.exit_position_async(
                self._client, self._token_id, "SELL", position.size, bid
            )
        if resp:
            self._remember_order_id(resp, self._exit_order_ids)
            print(
                f"[runtime:{self._strategy_id}] tp/sl exit: "
                f"SELL {position.size} @ {bid} placed"
            )
        else:
            print(f"[runtime:{self._strategy_id}] tp/sl exit: SELL FAILED")

    # ------------------------------------------------------------------ #
    # Exit protection
    # ------------------------------------------------------------------ #
    async def _ensure_exit_order(self, strat: Strategy) -> None:
        if strat.exit_price is None:
            print(
                f"[runtime:{self._strategy_id}] CRITICAL — position exists but "
                f"EXIT_PRICE not configured; manual action required"
            )
            return

        has_exit = await self._has_matching_open_order(
            "SELL", price=strat.exit_price, size=self._get_position().size
        )
        exit_remaining = await self._matching_open_order_remaining("SELL", strat.exit_price)
        if has_exit is None or exit_remaining is None:
            return

        baseline_remaining = self._exit_order_baseline or 0.0
        protected_size = max(0.0, exit_remaining - baseline_remaining)
        position_size = self._get_position().size
        if protected_size + 0.05 >= position_size:
            self._failed_exit_attempts = 0
            return

        if self._failed_exit_attempts >= self._exit_backoff_threshold:
            print(
                f"[runtime:{self._strategy_id}] exit backed off — "
                f"{self._failed_exit_attempts} consecutive failures"
            )
            return

        missing_size = max(0.0, position_size - protected_size)
        async with self._order_lock:
            resp = await trading.exit_position_async(
                self._client, self._token_id, "SELL", missing_size, strat.exit_price
            )
        if resp is None:
            self._failed_exit_attempts += 1
        else:
            self._failed_exit_attempts = 0
            self._remember_order_id(resp, self._exit_order_ids)

    # ------------------------------------------------------------------ #
    # Baseline recording
    # ------------------------------------------------------------------ #
    async def _remember_layer_baseline(self, strat: Strategy) -> bool:
        position = await self._get_portfolio_position()
        if position is None:
            print(
                f"[runtime:{self._strategy_id}] baseline not recorded — "
                "portfolio API unavailable, proceeding without baseline"
            )
            return True
        self._entry_position_baseline = position
        exit_remaining = await self._matching_open_order_remaining("SELL", strat.exit_price)
        if exit_remaining is None:
            print(
                f"[runtime:{self._strategy_id}] exit baseline not recorded — "
                "open-orders check failed, proceeding without exit baseline"
            )
            return True
        self._exit_order_baseline = exit_remaining
        return True

    # ------------------------------------------------------------------ #
    # Open orders helpers
    # ------------------------------------------------------------------ #
    async def _has_matching_open_order(
        self, side: str, price: float, size: float
    ) -> bool | None:
        orders = await trading.get_open_orders_async(self._client)
        for o in orders:
            o_side = str(o.get("side", "")).upper()
            o_price = _to_float(o.get("price"))
            o_size = _to_float(o.get("original_size") or o.get("size"))
            o_token = str(o.get("asset_id") or o.get("token_id") or "")
            if o_token != self._token_id:
                continue
            if o_side != side.upper():
                continue
            if o_price is not None and abs(o_price - price) < 0.001:
                if o_size is not None and o_size >= size - 0.05:
                    return True
        return False

    async def _matching_open_order_remaining(
        self, side: str, price: float
    ) -> float | None:
        orders = await trading.get_open_orders_async(self._client)
        total = 0.0
        found_any = False
        for o in orders:
            o_side = str(o.get("side", "")).upper()
            o_price = _to_float(o.get("price"))
            o_size = _to_float(o.get("size") or o.get("original_size"))
            o_token = str(o.get("asset_id") or o.get("token_id") or "")
            if o_token != self._token_id:
                continue
            if o_side != side.upper():
                continue
            if o_price is not None and abs(o_price - price) < 0.001:
                if o_size is not None:
                    total += o_size
                    found_any = True
        return total if found_any else 0.0

    def _remember_order_id(self, resp: dict[str, Any] | None, bucket: set[str]) -> None:
        if not resp:
            return
        oid = str(resp.get("orderID") or resp.get("order_id") or "")
        if oid:
            bucket.add(oid)

    async def _trade_matches_current_intent(
        self, trade: dict[str, Any], side: str, size: float, price: float
    ) -> bool:
        """判断 trade 是否匹配当前 bot 意图（参照 master 分支）。

        优先用 order_id 交集精确匹配；不匹配或无 order_id 时
        fallback 到价格/数量/notional heuristic。
        """
        strat = self._guard.snapshot().get_strategy(self._strategy_id)
        if strat is None:
            return False

        # 1. order_id 交集精确匹配（仅当 trade 和 bot 都有 order_id 时）
        trade_order_ids = set(_extract_trade_order_ids(trade))
        if side.startswith("B") and trade_order_ids and self._entry_order_ids:
            return bool(trade_order_ids & self._entry_order_ids)
        if side.startswith("S") and trade_order_ids and self._exit_order_ids:
            return bool(trade_order_ids & self._exit_order_ids)

        # 2. heuristic fallback
        if side.startswith("B"):
            target_notional = strat.share_amount * strat.entry_price
            trade_notional = size * price
            return (
                price <= strat.entry_price + 0.01
                and _close_enough(size, strat.share_amount, min_abs=0.05, rel=0.02)
                and _close_enough(trade_notional, target_notional, min_abs=1.0, rel=0.10)
            )
        if side.startswith("S"):
            pos = self._get_position()
            if not pos.has_position:
                return False
            expected_price = strat.exit_price
            if expected_price is None:
                return size <= pos.size + 0.05
            return pos.has_position and _close_enough(
                price, expected_price, min_abs=0.005, rel=0.02
            )
        return False

    # ------------------------------------------------------------------ #
    # Notifications
    # ------------------------------------------------------------------ #
    async def _maybe_notify_open_orders(self) -> None:
        if not self._notifier or not self._notifier.enabled:
            return
        try:
            orders = await trading.get_open_orders_async(self._client)
        except Exception:
            return
        my_orders = [
            o for o in orders
            if str(o.get("asset_id") or o.get("token_id") or "") == self._token_id
        ]
        sig = "|".join(
            f"{o.get('side','')}{_to_float(o.get('price'))}{_to_float(o.get('size'))}"
            for o in my_orders
        )
        if sig != self._open_orders_sig:
            self._open_orders_sig = sig
            # Notify open orders change (best-effort)
            try:
                self._notifier.notify_open_orders(my_orders)
            except Exception:
                pass

    async def _push_status(self) -> None:
        if not self._notifier or not self._notifier.enabled:
            return
        pos = self._get_position()
        strat = self._guard.snapshot().get_strategy(self._strategy_id)
        label = strat.display() if strat else self._label
        try:
            self._notifier.notify_status(
                f"[{label}] position={pos.size}@{pos.avg_price} "
                f"entry_attempted={self._entry_attempted}"
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Public actions (called by Telegram via RuntimeManager)
    # ------------------------------------------------------------------ #
    async def strategy_enter(self) -> str:
        """Force immediate BUY (ignores conditional_entry)."""
        strat = self._guard.snapshot().get_strategy(self._strategy_id)
        if strat is None:
            return f"[{self._strategy_id}] strategy not found"
        if self._entry_attempted:
            return f"[{self._label}] already attempted"
        if strat.exit_price is None:
            return f"[{self._label}] EXIT_PRICE missing"
        await self._do_entry(strat)
        return f"[{self._label}] BUY {strat.share_amount} @ {strat.entry_price} placed"

    async def strategy_close(self) -> str:
        """Place SELL to close position."""
        strat = self._guard.snapshot().get_strategy(self._strategy_id)
        if strat is None:
            return f"[{self._strategy_id}] strategy not found"
        pos = self._get_position()
        if not pos.has_position:
            return f"[{self._label}] no position to close"
        if strat.exit_price is None:
            return f"[{self._label}] EXIT_PRICE missing"
        async with self._order_lock:
            resp = await trading.exit_position_async(
                self._client, self._token_id, "SELL", pos.size, strat.exit_price
            )
        if resp is None:
            return f"[{self._label}] SELL failed"
        self._remember_order_id(resp, self._exit_order_ids)
        return f"[{self._label}] SELL {pos.size} @ {strat.exit_price} placed"

    def status_snapshot(self) -> dict[str, Any]:
        """Return a status dict for Telegram queries (sync, lock-protected)."""
        pos = self._position
        strat = self._guard.snapshot().get_strategy(self._strategy_id)
        return {
            "strategy_id": self._strategy_id,
            "token_id": self._token_id,
            "label": strat.display() if strat else self._label,
            "outcome_name": strat.outcome_name if strat else "",
            "entry_price": strat.entry_price if strat else None,
            "exit_price": strat.exit_price if strat else None,
            "share_amount": strat.share_amount if strat else None,
            "enabled": strat.enabled if strat else False,
            "position_size": pos.get("size", 0.0),
            "position_avg_price": pos.get("avg_price", 0.0),
            "entry_attempted": self._entry_attempted,
            "entry_order_ids": set(self._entry_order_ids),
            "exit_order_ids": set(self._exit_order_ids),
            "cycle": self._cycle,
        }


# --------------------------------------------------------------------------- #
# Utility functions (migrated from runtime.py module level)
# --------------------------------------------------------------------------- #
def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _close_enough(a: float, b: float, min_abs: float = 0.01, rel: float = 0.01) -> bool:
    if abs(a - b) <= min_abs:
        return True
    return abs(a - b) / max(abs(b), 1e-9) <= rel


def _extract_trade_order_ids(trade: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    taker_id = str(trade.get("taker_order_id") or "")
    if taker_id:
        ids.append(taker_id)
    maker_orders = trade.get("maker_orders") or []
    if isinstance(maker_orders, list):
        for mo in maker_orders:
            if isinstance(mo, dict):
                mid = str(mo.get("order_id") or mo.get("orderID") or "")
                if mid:
                    ids.append(mid)
    return ids


class _sync_lock_guard:
    """Helper to read asyncio.Lock-protected state synchronously.

    Since asyncio.Lock cannot be acquired synchronously, this provides a
    best-effort non-locking read for status_snapshot. In practice the
    asyncio event loop is single-threaded so concurrent mutation is not
    a concern within the same thread.
    """

    def __init__(self, lock: asyncio.Lock):
        self._lock = lock

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass
