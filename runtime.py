"""
Asyncio multi-strategy runtime for Polymarket CLOB trading.

Architecture:
- One ``StrategyRuntime`` per strategy (one token + independent params).
- All runtimes share a single ``asyncio.Lock`` (``_order_lock``) that
  serialises order placement to avoid CLOB SDK signing races.
- CLOB SDK synchronous calls are wrapped with ``asyncio.to_thread``.
- Bilateral betting = two independent ``StrategyRuntime`` instances.

Each cycle (_cycle_once):
1. Fill detection — pull recent trades for this token, match by order_id.
2. Position reconciliation — query Polymarket data API for actual position.
3. If holding: check TP/SL → emergency exit, else maintain protective SELL.
4. If not holding: check entry (conditional or direct).
5. Heartbeat log every STATUS_EVERY_CYCLES (stdout, not Telegram).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import trading
from config import Config, StrategyConfig
from py_clob_client_v2.clob_types import TradeParams
from strategy import PositionData, should_enter, should_exit, tp_trigger_price, sl_trigger_price

# Funder address for the Polymarket data API position query.
PORTFOLIO_POSITIONS_URL = "https://data-api.polymarket.com/positions"


class StrategyRuntime:
    """One strategy = one outcome token + independent params, asyncio-based."""

    # Heartbeat states
    S_WAITING_ENTRY = "待入场"
    S_BUY_PENDING = "已挂买单（待成交）"
    S_HOLDING_NO_SELL = "持仓中（待挂卖单）"
    S_HOLDING_WITH_SELL = "持仓中（已挂保护卖单）"
    S_CLOSED = "已平仓"

    def __init__(
        self,
        strategy_config: StrategyConfig,
        client,
        notifier,
        order_lock: asyncio.Lock,
        config: Config,
    ):
        self._cfg = strategy_config
        self._client = client
        self._notifier = notifier
        self._order_lock = order_lock
        self._config = config

        # Position / order state
        self._position = PositionData(token_id=strategy_config.token_id)
        self._entry_order_ids: set[str] = set()
        self._exit_order_ids: set[str] = set()
        self._entry_attempted = False
        self._entry_position_baseline: tuple[float, float] | None = None
        self._exit_order_baseline: float | None = None
        self._seen_trade_ids: set[str] = set()
        self._first_fill_check_done = False

        # Exit-order failure backoff
        self._failed_exit_attempts = 0
        self._exit_backoff_threshold = 2

        # Lifecycle
        self._cycle = 0
        self._closed = False
        self._stop = asyncio.Event()

        # Cached open-order flags for heartbeat display
        self._has_open_buy_order = False
        self._has_open_sell_order = False

    @property
    def label(self) -> str:
        return self._cfg.display()

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        interval = max(1, self._config.poll_interval)
        status_every = max(1, self._config.status_every_cycles)

        while not self._stop.is_set() and not self._closed:
            self._cycle += 1
            try:
                await self._cycle_once()
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                print(f"[{self.label}] cycle {self._cycle} error: {exc}")

            if self._cycle % status_every == 0:
                self._heartbeat()

            # Sleep but wake immediately if stop is signalled.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

        if self._closed:
            print(f"[{self.label}] 策略已结束（卖单已成交）")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ #
    # Main cycle
    # ------------------------------------------------------------------ #
    async def _cycle_once(self) -> None:
        if not self._config.trading_enabled:
            return

        token_id = self._cfg.token_id

        # 1. Fill detection
        await self._detect_fills()

        # 2. Position reconciliation (fallback fill detection)
        await self._reconcile_position()

        # 3. If holding: TP/SL check + exit-order maintenance
        if self._position.has_position:
            market_data = await asyncio.to_thread(
                trading.get_market_data, self._client, token_id
            )
            # TP/SL emergency exit (exit_price=None — protective sell handles that)
            if self._check_tp_sl(market_data):
                await self._emergency_exit(market_data)
                return
            await self._ensure_exit_order()
            return

        # 4. If not holding and not closed: check entry
        if not self._closed:
            await self._check_entry()

    # ------------------------------------------------------------------ #
    # Entry logic
    # ------------------------------------------------------------------ #
    async def _check_entry(self) -> None:
        cfg = self._cfg
        token_id = cfg.token_id

        if cfg.exit_price is None:
            print(f"[{self.label}] entry blocked — EXIT_PRICE required")
            return

        # Guard: don't place if a matching buy order is already resting.
        has_entry = await self._has_matching_open_order(
            token_id, "BUY", price=cfg.entry_price, size=cfg.share_amount
        )
        if has_entry is None:
            return  # couldn't confirm open orders; retry next cycle
        if has_entry:
            self._has_open_buy_order = True
            return
        self._has_open_buy_order = False

        if self._entry_attempted:
            return  # already attempted; don't retry

        # Conditional entry: wait for best_ask <= entry_price
        if self._config.conditional_entry:
            market_data = await asyncio.to_thread(
                trading.get_market_data, self._client, token_id
            )
            if not should_enter(market_data, cfg.entry_price):
                return
            print(f"[{self.label}] entry signal (ask <= {cfg.entry_price})")
        else:
            print(f"[{self.label}] direct entry (conditional_entry=off)")

        # Record baseline before placing the order
        await self._remember_baseline(token_id)
        self._entry_attempted = True

        # Place BUY limit order (serialised by global lock)
        async with self._order_lock:
            resp = await asyncio.to_thread(
                trading.enter_position,
                self._client, token_id, "BUY", cfg.share_amount, cfg.entry_price,
            )
        self._remember_order_id(resp, self._entry_order_ids)
        self._has_open_buy_order = True

    # ------------------------------------------------------------------ #
    # Exit-order maintenance
    # ------------------------------------------------------------------ #
    async def _ensure_exit_order(self) -> None:
        cfg = self._cfg
        token_id = cfg.token_id

        if cfg.exit_price is None:
            print(f"[{self.label}] CRITICAL — position exists but EXIT_PRICE not configured")
            return

        has_exit = await self._has_matching_open_order(
            token_id, "SELL", price=cfg.exit_price, size=self._position.size
        )
        exit_remaining = await self._matching_open_order_remaining(
            token_id, "SELL", cfg.exit_price
        )
        if has_exit is None or exit_remaining is None:
            self._has_open_sell_order = False
            return

        baseline_remaining = self._exit_order_baseline or 0.0
        protected_size = max(0.0, exit_remaining - baseline_remaining)

        if protected_size + 0.05 >= self._position.size:
            self._has_open_sell_order = True
            self._failed_exit_attempts = 0
            return

        self._has_open_sell_order = False

        # Backoff: too many consecutive failures
        if self._failed_exit_attempts >= self._exit_backoff_threshold:
            print(
                f"[{self.label}] exit backed off — {self._failed_exit_attempts} "
                f"consecutive failures; manual intervention required"
            )
            return

        missing_size = max(0.0, self._position.size - protected_size)
        print(f"[{self.label}] exit protection — SELL {missing_size} @ {cfg.exit_price}")
        async with self._order_lock:
            resp = await asyncio.to_thread(
                trading.exit_position,
                self._client, token_id, "SELL", missing_size, cfg.exit_price,
            )
        if resp is None:
            self._failed_exit_attempts += 1
            print(
                f"[{self.label}] exit order failed — attempt "
                f"{self._failed_exit_attempts}/{self._exit_backoff_threshold}"
            )
        else:
            self._failed_exit_attempts = 0
            self._remember_order_id(resp, self._exit_order_ids)
            self._has_open_sell_order = True

    # ------------------------------------------------------------------ #
    # TP/SL emergency exit
    # ------------------------------------------------------------------ #
    def _check_tp_sl(self, market_data) -> bool:
        """Return True if TP/SL threshold is breached (exit_price excluded)."""
        return should_exit(
            self._position,
            market_data,
            exit_price=None,  # protective sell handles exit_price
            take_profit_pct=self._cfg.take_profit_pct,
            stop_loss_pct=self._cfg.stop_loss_pct,
        )

    async def _emergency_exit(self, market_data) -> None:
        cfg = self._cfg
        token_id = cfg.token_id
        bid = market_data.best_bid
        if bid is None or bid <= 0:
            print(f"[{self.label}] emergency exit failed — no best_bid available")
            return

        print(f"[{self.label}] TP/SL triggered — emergency exit at best_bid={bid}")

        # Cancel existing protective SELL orders for this token
        await asyncio.to_thread(
            trading.cancel_open_orders_for_token, self._client, token_id, "SELL"
        )
        self._has_open_sell_order = False

        # Place emergency SELL at best_bid
        async with self._order_lock:
            resp = await asyncio.to_thread(
                trading.exit_position,
                self._client, token_id, "SELL", self._position.size, bid,
            )
        if resp is not None:
            self._remember_order_id(resp, self._exit_order_ids)
            self._has_open_sell_order = True
            print(f"[{self.label}] emergency SELL placed: {self._position.size} @ {bid}")
        else:
            print(f"[{self.label}] emergency SELL failed — manual action required")

    # ------------------------------------------------------------------ #
    # Fill detection
    # ------------------------------------------------------------------ #
    async def _detect_fills(self) -> None:
        token_id = self._cfg.token_id
        try:
            if self._client:
                params = TradeParams(asset_id=token_id)
                raw_trades = await asyncio.to_thread(
                    self._client.get_trades, params=params
                )
            else:
                raw_trades = []
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.label}] get_trades failed: {exc}")
            return

        trades = [trading._order_to_dict(tr) for tr in raw_trades] if raw_trades else []
        if not trades:
            return

        # first_run: apply all existing trades to sync state (no notification).
        # Use an independent flag (not _seen_trade_ids emptiness) to avoid the
        # bug where the first call returns empty and the next call is mistaken
        # for first_run again.
        first_run = not self._first_fill_check_done
        self._first_fill_check_done = True

        new_trades: list[dict[str, Any]] = []
        for tr in trades:
            tid = str(tr.get("id") or tr.get("trade_id") or tr.get("order_id") or "")
            if not tid or tid in self._seen_trade_ids:
                continue
            self._seen_trade_ids.add(tid)
            if first_run:
                self._apply_fill(tr)
            else:
                new_trades.append(tr)

        for tr in new_trades:
            self._apply_fill(tr)
            if self._notifier and self._notifier.enabled:
                self._notifier.notify_fill(tr, self._cfg)

    def _extract_bot_fills(self, trade: dict[str, Any]) -> list[tuple[str, float, float]]:
        """Extract this bot's fills from a trade record.

        Returns ``[(direction, size, price), ...]`` where direction is the
        bot's actual direction (BUY/SELL), not the taker-side ``trade.side``.
        """
        fills: list[tuple[str, float, float]] = []
        trade_price = _to_float(trade.get("price")) or 0.0

        # 1. Bot as taker
        taker_id = str(trade.get("taker_order_id") or "")
        if taker_id:
            taker_size = _to_float(trade.get("size") or trade.get("matched_amount")) or 0.0
            if taker_id in self._entry_order_ids:
                fills.append(("BUY", taker_size, trade_price))
            elif taker_id in self._exit_order_ids:
                fills.append(("SELL", taker_size, trade_price))

        # 2. Bot as maker
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

    def _update_position(self, side: str, size: float, price: float) -> None:
        """Update local position from a bot fill. side = BUY/SELL."""
        if side.startswith("B"):
            old_size = self._position.size
            old_avg = self._position.avg_price
            new_size = old_size + size
            self._position.avg_price = (
                (old_avg * old_size + price * size) / new_size
                if new_size > 0
                else price
            )
            self._position.size = new_size
        elif side.startswith("S"):
            self._position.size = max(0.0, self._position.size - size)
            if self._position.size <= 0:
                self._closed = True
        print(
            f"[{self.label}] position update side={side} size={size} price={price} "
            f"-> held={self._position.size} @ {self._position.avg_price:.4f}"
        )

    def _apply_fill(self, trade: dict[str, Any]) -> None:
        token_id = str(
            trade.get("asset_id") or trade.get("token_id") or trade.get("market") or ""
        )
        if not token_id or token_id != self._cfg.token_id:
            if token_id:
                print(
                    f"[{self.label}] trade token mismatch: trade={token_id[:12]}... "
                    f"expected={self._cfg.token_id[:12]}..."
                )
            return

        side = str(trade.get("side", "")).upper()
        trade_size = _to_float(trade.get("size") or trade.get("matched_amount")) or 0.0
        price = _to_float(trade.get("price")) or 0.0

        # 1. order_id precise match
        bot_fills = self._extract_bot_fills(trade)
        if bot_fills:
            for fill_side, fill_size, fill_price in bot_fills:
                self._update_position(fill_side, fill_size, fill_price)
            return

        # 2. trade has order_ids but none match our buckets → someone else's trade
        trade_order_ids = _extract_trade_order_ids(trade)
        has_buckets = bool(self._entry_order_ids or self._exit_order_ids)
        if trade_order_ids and has_buckets:
            print(
                f"[{self.label}] ignored fill (order_id not in bot buckets) "
                f"side={side} size={trade_size} price={price}"
            )
            return

        # 3. Heuristic fallback (no order_id info or buckets empty)
        if not self._trade_matches_intent(side, trade_size, price):
            print(
                f"[{self.label}] ignored fill (heuristic mismatch) "
                f"side={side} size={trade_size} price={price}"
            )
            return

        self._update_position(side, trade_size, price)

    def _trade_matches_intent(
        self, side: str, size: float, price: float
    ) -> bool:
        """Heuristic fallback matching (no order_id or buckets empty)."""
        cfg = self._cfg
        if side.startswith("B"):
            target_notional = cfg.share_amount * cfg.entry_price
            trade_notional = size * price
            return (
                price <= cfg.entry_price + 0.01
                and _close_enough(size, cfg.share_amount, min_abs=0.05, rel=0.02)
                and _close_enough(trade_notional, target_notional, min_abs=1.0, rel=0.10)
            )
        if side.startswith("S"):
            return (
                self._position.size > 0
                and _close_enough(price, cfg.exit_price, min_abs=0.005, rel=0.02)
            )
        return False

    # ------------------------------------------------------------------ #
    # Position reconciliation (data API fallback)
    # ------------------------------------------------------------------ #
    async def _reconcile_position(self) -> None:
        """Use Polymarket portfolio positions as a fallback fill detector.

        - Upward correction: portfolio > local → BUY fill missed locally.
        - Downward correction: portfolio < local → SELL fill missed locally.
        """
        portfolio_position = await self._get_portfolio_position()
        if portfolio_position is None:
            return
        size, avg_price = portfolio_position

        current_size = self._position.size

        # Downward: portfolio < local → SELL fill missed
        if size < current_size - 0.05:
            if size <= 0:
                self._position.size = 0.0
                self._closed = True
            else:
                self._position.size = size
                self._position.avg_price = avg_price
            print(
                f"[{self.label}] portfolio reconciliation corrected position down "
                f"(local={current_size} -> portfolio={size})"
            )
            return

        if size <= 0:
            return

        # Upward: portfolio > local → BUY fill missed
        managed_size = size
        managed_avg = avg_price
        baseline = self._entry_position_baseline
        if baseline and self._entry_attempted:
            baseline_size, baseline_avg = baseline
            if size <= baseline_size + 0.01:
                return
            managed_size = size - baseline_size
            managed_cost = max(0.0, size * avg_price - baseline_size * baseline_avg)
            managed_avg = managed_cost / managed_size if managed_size > 0 else self._cfg.entry_price
        elif not self._portfolio_matches_intent(size, avg_price):
            return

        if current_size >= managed_size:
            return

        self._position.size = managed_size
        self._position.avg_price = managed_avg
        print(
            f"[{self.label}] portfolio reconciliation detected position "
            f"({managed_size} @ {managed_avg:.4f})"
        )

    def _portfolio_matches_intent(self, size: float, avg_price: float) -> bool:
        cfg = self._cfg
        if self._entry_attempted:
            return avg_price <= cfg.entry_price + 0.02 and size >= 0.01
        return (
            avg_price <= cfg.entry_price + 0.02
            and _close_enough(size, cfg.share_amount, min_abs=0.05, rel=0.02)
        )

    async def _get_portfolio_position(self) -> tuple[float, float] | None:
        funder = self._config.funder
        if not funder:
            return (0.0, 0.0)

        last_exc: Exception | None = None
        data: Any = None
        for attempt in range(3):
            try:
                response = await asyncio.to_thread(
                    httpx.get,
                    PORTFOLIO_POSITIONS_URL,
                    params={"user": funder, "limit": 200, "sizeThreshold": 0},
                    timeout=15,
                )
                response.raise_for_status()
                data = response.json()
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep((attempt + 1) * 2)

        if data is None:
            print(f"[{self.label}] portfolio position check failed: {last_exc}")
            return None

        items = data if isinstance(data, list) else []
        for item in items:
            item_token_id = str(
                item.get("asset") or item.get("assetId") or item.get("tokenId") or ""
            )
            if item_token_id == self._cfg.token_id:
                return (
                    _to_float(item.get("size")) or 0.0,
                    _to_float(item.get("avgPrice")) or 0.0,
                )
        return (0.0, 0.0)

    async def _remember_baseline(self, token_id: str) -> None:
        """Record pre-entry baseline (portfolio position + SELL orders)."""
        position = await self._get_portfolio_position()
        if position is None:
            print(
                f"[{self.label}] baseline not recorded — portfolio API unavailable, "
                f"proceeding without baseline"
            )
            return
        self._entry_position_baseline = position

        exit_remaining = await self._matching_open_order_remaining(
            token_id, "SELL", self._cfg.exit_price
        )
        if exit_remaining is not None:
            self._exit_order_baseline = exit_remaining

    # ------------------------------------------------------------------ #
    # Open-order helpers
    # ------------------------------------------------------------------ #
    async def _has_matching_open_order(
        self,
        token_id: str,
        side: str,
        *,
        price: float | None,
        size: float | None,
    ) -> bool | None:
        try:
            orders = await asyncio.to_thread(self._client.get_open_orders)
        except Exception:  # noqa: BLE001
            return None
        for o in orders:
            d = o if isinstance(o, dict) else trading._order_to_dict(o)
            o_token = str(d.get("asset_id") or d.get("token_id") or d.get("market") or "")
            o_side = str(d.get("side", "")).upper()
            if o_token != token_id or o_side != side.upper():
                continue
            o_price = _to_float(d.get("price"))
            o_size = _to_float(d.get("original_size") or d.get("size") or d.get("remaining_size"))
            if _close_enough(o_price, price, min_abs=0.005, rel=0.02) and _close_enough(
                o_size, size, min_abs=0.05, rel=0.02
            ):
                return True
        return False

    async def _matching_open_order_remaining(
        self,
        token_id: str,
        side: str,
        price: float | None,
    ) -> float | None:
        try:
            orders = await asyncio.to_thread(self._client.get_open_orders)
        except Exception:  # noqa: BLE001
            return None
        remaining_total = 0.0
        for o in orders:
            d = o if isinstance(o, dict) else trading._order_to_dict(o)
            o_token = str(d.get("asset_id") or d.get("token_id") or d.get("market") or "")
            o_side = str(d.get("side", "")).upper()
            o_price = _to_float(d.get("price"))
            if o_token != token_id or o_side != side.upper():
                continue
            if not _close_enough(o_price, price, min_abs=0.005, rel=0.02):
                continue
            original_size = _to_float(d.get("original_size") or d.get("size")) or 0.0
            matched_size = _to_float(d.get("size_matched")) or 0.0
            remaining_total += max(0.0, original_size - matched_size)
        return remaining_total

    def _remember_order_id(self, response: Any, bucket: set[str]) -> None:
        if not isinstance(response, dict):
            return
        order_id = str(response.get("orderID") or response.get("order_id") or "")
        if order_id:
            bucket.add(order_id)

    # ------------------------------------------------------------------ #
    # Heartbeat (stdout, not Telegram)
    # ------------------------------------------------------------------ #
    def _current_state(self) -> str:
        if self._closed:
            return self.S_CLOSED
        if self._position.has_position:
            return self.S_HOLDING_WITH_SELL if self._has_open_sell_order else self.S_HOLDING_NO_SELL
        if self._entry_attempted or self._has_open_buy_order:
            return self.S_BUY_PENDING
        return self.S_WAITING_ENTRY

    def _heartbeat(self) -> None:
        cfg = self._cfg
        state = self._current_state()
        tp_str = f"{cfg.take_profit_pct:.0%}" if cfg.take_profit_pct is not None else "off"
        sl_str = f"{cfg.stop_loss_pct:.0%}" if cfg.stop_loss_pct is not None else "off"

        line = (
            f"[{self.label}] cycle={self._cycle} {state} | "
            f"entry={cfg.entry_price} exit={cfg.exit_price} "
            f"tp={tp_str} sl={sl_str} size={cfg.share_amount}"
        )
        if self._position.has_position:
            tp_trig = tp_trigger_price(self._position.avg_price, cfg.take_profit_pct)
            sl_trig = sl_trigger_price(self._position.avg_price, cfg.stop_loss_pct)
            tp_trig_str = f"{tp_trig:.4f}" if tp_trig is not None else "n/a"
            sl_trig_str = f"{sl_trig:.4f}" if sl_trig is not None else "n/a"
            line += (
                f" | 持仓={self._position.size} @ {self._position.avg_price:.4f}"
                f" tp_trigger={tp_trig_str} sl_trigger={sl_trig_str}"
            )
        print(line)

    # ------------------------------------------------------------------ #
    # Status snapshot (for notifications)
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "token_id": self._cfg.token_id,
            "state": self._current_state(),
            "cycle": self._cycle,
            "entry_price": self._cfg.entry_price,
            "exit_price": self._cfg.exit_price,
            "share_amount": self._cfg.share_amount,
            "take_profit_pct": self._cfg.take_profit_pct,
            "stop_loss_pct": self._cfg.stop_loss_pct,
            "position_size": self._position.size,
            "avg_price": self._position.avg_price,
            "closed": self._closed,
        }


class RuntimeManager:
    """Manages all StrategyRuntime instances and the global order lock."""

    def __init__(self, client, notifier, config: Config):
        self._client = client
        self._notifier = notifier
        self._config = config
        self._order_lock = asyncio.Lock()
        self._runtimes: list[StrategyRuntime] = []
        self._tasks: list[asyncio.Task] = []
        self._status_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def add_strategy(self, strategy_config: StrategyConfig) -> StrategyRuntime:
        rt = StrategyRuntime(
            strategy_config, self._client, self._notifier,
            self._order_lock, self._config,
        )
        self._runtimes.append(rt)
        return rt

    @property
    def runtimes(self) -> list[StrategyRuntime]:
        return list(self._runtimes)

    async def start_all(self) -> None:
        if not self._runtimes:
            print("[manager] no strategies to start")
            return

        if self._notifier and self._notifier.enabled:
            self._notifier.notify_started(self._runtimes)

        for rt in self._runtimes:
            task = asyncio.create_task(rt.run(), name=f"strategy:{rt.label}")
            self._tasks.append(task)
            print(f"[manager] started strategy: {rt.label}")

        # Periodic aggregate status push (Telegram / webhook)
        if self._notifier and self._notifier.enabled:
            self._status_task = asyncio.create_task(
                self._status_loop(), name="status-push"
            )

    async def stop_all(self) -> None:
        self._stop.set()
        for rt in self._runtimes:
            rt.stop()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._status_task:
            self._status_task.cancel()
            try:
                await self._status_task
            except asyncio.CancelledError:
                pass
        if self._notifier and self._notifier.enabled:
            self._notifier.notify_stopped()
        print("[manager] all strategies stopped")

    async def _status_loop(self) -> None:
        interval = max(1, self._config.poll_interval) * max(1, self._config.status_every_cycles)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            if self._notifier and self._notifier.enabled:
                self._notifier.notify_status(self._runtimes)

    def status_snapshot(self) -> list[dict[str, Any]]:
        return [rt.snapshot() for rt in self._runtimes]


# --------------------------------------------------------------------------- #
# Module-level helpers
# --------------------------------------------------------------------------- #
def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _close_enough(
    actual: float | None,
    expected: float | None,
    *,
    min_abs: float,
    rel: float,
) -> bool:
    if actual is None or expected is None:
        return False
    return abs(actual - expected) <= max(min_abs, abs(expected) * rel)


def _extract_trade_order_ids(trade: dict[str, Any]) -> set[str]:
    """Collect all order_ids from a CLOB v2 Trade object."""
    ids: set[str] = set()
    taker_id = trade.get("taker_order_id") or trade.get("order_id") or trade.get("orderID")
    if taker_id:
        ids.add(str(taker_id))
    maker_orders = trade.get("maker_orders") or []
    if isinstance(maker_orders, list):
        for mo in maker_orders:
            if isinstance(mo, dict):
                mid = mo.get("order_id") or mo.get("orderID")
                if mid:
                    ids.add(str(mid))
    return ids
