"""
Bot runtime: the strategy / trading / notification main loop.

Responsibilities (one cycle every ``POLL_INTERVAL`` seconds):

1. Read the current trading params (thread-safe snapshot).
2. For the active market, fetch live market data and the local position.
3. ``should_enter`` -> place a BUY limit order (entry).
4. ``should_exit``  -> place a SELL limit order (exit).
5. Detect new fills via ``get_recent_trades`` and update local positions,
   pushing a fill notification for each new buy/sell.
6. Detect open-order set changes and push the current open-orders summary.
7. Periodically push a runtime status summary.
8. Periodically check remote cancellations (v3.0 §5.3).
9. Detect pending settlement markets (v3.0 §5.4).

The runtime is also the source of truth the interactive Telegram bot queries
through ``status_snapshot``.

All trading state is persisted via the ``Persistence`` layer (SQLite + WAL).
BUY and SELL paths are symmetric: both record a PENDING intent before the
API call, classify exceptions (timeout/rate-limit/reject), and reconcile
remote cancellations on the next cycle.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx
import trading
from config import ConfigGuard, TradingParams
from order_state import (
    OrderRateLimitError,
    OrderStatus,
    OrderTimeoutError,
    new_intent_id,
)
from persistence import FillRecord, OrderRecord, PositionRecord
from py_clob_client_v2.clob_types import TradeParams
from strategy import PositionData, should_enter


class BotRuntime:
    def __init__(
        self,
        client,
        config_guard: ConfigGuard,
        notifier,
        persistence=None,
        session_id: str | None = None,
    ):
        self._client = client
        self._guard = config_guard
        self._notifier = notifier
        self._db = persistence
        self._session_id = session_id or "no-session"

        # Memory cache (mirror of DB for hot-path reads). The DB is the
        # source of truth; these caches are best-effort and refreshed from
        # the DB at startup and after each write.
        self._positions_cache: dict[str, dict[str, Any]] = {}
        self._open_orders_sig: str = ""
        self._cycle = 0
        self._exit_backoff_threshold = 2
        # DB unavailable consecutive cycles counter (for Layer 3 protection)
        self._db_unavailable_cycles = 0

        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None

        # Load persisted state into memory cache at startup
        if self._db is not None:
            self._load_state_from_db()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="runtime", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def run_forever(self) -> None:
        """Run the loop in the calling thread until stopped."""
        self._stop.clear()
        self._loop()

    # ------------------------------------------------------------------ #
    # State loading / DB integration
    # ------------------------------------------------------------------ #
    def _load_state_from_db(self) -> None:
        """Load persisted state into memory cache at startup."""
        if self._db is None:
            return
        try:
            for p in self._db.get_all_positions():
                self._positions_cache[p.token_id] = {
                    "size": p.size,
                    "avg_price": p.avg_price,
                    "label": p.label,
                }
            print(f"[runtime] loaded {len(self._positions_cache)} position(s) from DB")
        except Exception as exc:  # noqa: BLE001
            print(f"[runtime] failed to load state from DB: {exc}")

    def _db_write_safe(self, action_name: str, action) -> bool:
        """Execute a DB write with Layer 2/3 protection.

        Returns True on success, False on failure. On persistent failure,
        marks DB unavailable and triggers Telegram alert.
        """
        if self._db is None:
            return False
        try:
            action()
            self._db_unavailable_cycles = 0
            return True
        except Exception as exc:
            print(f"[runtime] DB write failed ({action_name}): {exc}")
            self._db_unavailable_cycles += 1
            if self._db_unavailable_cycles >= 3:
                self._db.mark_unavailable(self._cycle)
                if self._notifier and self._notifier.enabled:
                    self._notifier.notify(
                        f"⚠ DB unavailable (cycle {self._cycle}). "
                        f"Degraded to memory-only mode."
                    )
            return False

    # ------------------------------------------------------------------ #
    # Status snapshot (consumed by the Telegram command bot)
    # ------------------------------------------------------------------ #
    def status_snapshot(self) -> dict[str, Any]:
        with self._lock:
            t = self._guard.snapshot()
            positions = [
                {
                    "token_id": tid,
                    "label": p.get("label", ""),
                    "size": p.get("size", 0.0),
                    "avg_price": p.get("avg_price", 0.0),
                }
                for tid, p in self._positions_cache.items()
                if p.get("size", 0.0) > 0
            ]
        try:
            open_orders = trading.get_open_orders(self._client) if self._client else []
        except Exception:  # noqa: BLE001
            open_orders = []
        return {
            "markets": t.markets,
            "active_market": t.active_market(),
            "trading_params": t,
            "positions": positions,
            "open_orders": open_orders,
            "cycle": self._cycle,
        }

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def _loop(self) -> None:
        interval = max(1, self._guard.config.poll_interval)
        status_every = max(1, self._guard.config.status_every_cycles)
        archive_every = max(0, self._guard.config.archive_every_cycles)
        remote_check_interval = max(1, self._guard.config.remote_check_interval)

        active = self._guard.active_market()
        if self._notifier and self._notifier.enabled:
            self._notifier.notify_started(active)

        while not self._stop.is_set():
            self._cycle += 1
            try:
                # DB Layer 3 protection: if DB unavailable for too long, pause trading
                if self._db is not None and not self._db.is_db_available():
                    if self._db_unavailable_cycles >= 5:
                        print(
                            f"[runtime] DB unavailable for {self._db_unavailable_cycles} cycles; "
                            f"pausing trading, monitoring only"
                        )
                    else:
                        # Periodic recovery attempt
                        if self._cycle % 30 == 0:
                            if self._db.try_recover(self._cycle):
                                self._db_unavailable_cycles = 0
                self._cycle_once()
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                print(f"[runtime] cycle {self._cycle} error: {exc}")

            # Periodic archival
            if (
                self._db is not None
                and archive_every > 0
                and self._cycle % archive_every == 0
            ):
                try:
                    self._db.archive_old_data(
                        order_retention_days=self._guard.config.archive_retention_days,
                        seen_trades_retention_days=self._guard.config.seen_trades_retention_days,
                        recon_retention_days=self._guard.config.recon_retention_days,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[runtime] archive failed: {exc}")

            if self._cycle % status_every == 0 and self._notifier and self._notifier.enabled:
                self._push_status()

            self._stop.wait(interval)

        if self._notifier and self._notifier.enabled:
            self._notifier.notify_stopped()

    def _cycle_once(self) -> None:
        if not self._guard.config.trading_enabled:
            print("[runtime] trading disabled by TRADING_ENABLED=false; skipping cycle")
            return

        t = self._guard.snapshot()
        active = t.active_market()
        if active is None or not active.token_id:
            print("[runtime] no active market configured; skipping cycle")
            return

        token_id = active.token_id

        # 1. Detect fills first so positions are current before decisions.
        if not self._detect_fills(t):
            print(f"[runtime] cycle paused — could not confirm fills for {active.display()}")
            return
        self._reconcile_position_from_portfolio(t)

        # 2. Periodic remote cancellation check (v3.0 §5.3)
        self._check_remote_cancellations(t)

        # 3. Fetch live market data.
        market_data = trading.get_market_data(self._client, token_id)

        # 4. Pending settlement detection (v3.0 §5.4)
        if self._check_pending_settlement(t, market_data):
            # Market in pending settlement: skip entry but keep SELL protection
            position = self._get_position(token_id)
            if not position.has_position:
                self._maybe_notify_open_orders(t)
                return

        # 5. Exit protection comes before any new entry. If the bot sees a
        # managed position, it must keep a matching SELL order working.
        position = self._get_position(token_id)
        if position.has_position:
            if self._ensure_exit_order(t, active, token_id, position):
                return
            print(f"[runtime] holding {active.display()} ({position.size} @ {position.avg_price})")
            self._maybe_notify_open_orders(t)
            return

        # 6. Entry check (only when not already holding this token).
        if not position.has_position:
            if t.exit_price is None:
                print(
                    "[runtime] entry blocked — EXIT_PRICE is required before placing "
                    f"a BUY for {active.display()}"
                )
                return
            # Guard: don't place another entry order if the same intended order
            # is already resting. Token-only checks are too broad because the
            # user may also trade the same outcome manually.
            has_entry = self._has_matching_open_order(
                token_id, "BUY", price=t.entry_price, size=t.share_amount
            )
            if has_entry is None:
                print(f"[runtime] entry paused — could not confirm open orders for {active.display()}")
            elif has_entry:
                print(f"[runtime] entry skipped — matching buy order already exists for {active.display()}")
            elif self._db is not None and self._db.is_entry_attempted(token_id):
                print(f"[runtime] entry skipped — entry already attempted for {active.display()}")
            else:
                if t.conditional_entry:
                    # Conditional entry: wait until market ask is at/below entry price.
                    if should_enter(market_data, t.entry_price):
                        print(f"[runtime] entry signal for {active.display()}")
                        if not self._remember_layer_baseline(t, token_id):
                            print(f"[runtime] entry paused — could not record baseline for {active.display()}")
                            return
                        self._place_buy_order(t, active, token_id)
                    else:
                        print(f"[runtime] no entry signal for {active.display()}")
                else:
                    # Direct mode: immediately place a limit buy at entry price.
                    print(f"[runtime] direct entry for {active.display()} (conditional_entry=off)")
                    if not self._remember_layer_baseline(t, token_id):
                        print(f"[runtime] entry paused — could not record baseline for {active.display()}")
                        return
                    self._place_buy_order(t, active, token_id)

        # 7. Open-orders change notification.
        self._maybe_notify_open_orders(t)

    # ------------------------------------------------------------------ #
    # Remote cancellation & pending settlement detection (v3.0 §5.3 / §5.4)
    # ------------------------------------------------------------------ #
    def _check_remote_cancellations(self, t: TradingParams) -> None:
        """每 remote_check_interval 轮检测远端取消的本地 PLACED 订单。

        对比本地 PLACED/PARTIAL 订单与远端 open_orders：
        - 远端不存在的订单调用 ``_confirm_order_canceled_or_filled`` 判断是成交还是取消。
        - BUY 取消 → set_entry_attempted(True) 阻止重挂。
        - SELL 取消 → 标 CANCELED，下轮 ``_ensure_exit_order`` 自动补挂。
        """
        if self._db is None:
            return
        interval = max(1, self._guard.config.remote_check_interval)
        if self._cycle % interval != 0:
            return
        try:
            remote_open = trading.get_open_orders(self._client) or []
        except Exception as exc:  # noqa: BLE001
            print(f"[runtime] remote cancel check skipped — API error: {exc}")
            return
        remote_ids = {
            str(o.get("id") or o.get("order_id") or "") for o in remote_open
        }
        local_active = self._db.query_unfinished_orders()
        for order in local_active:
            if order.order_id and order.order_id not in remote_ids:
                self._confirm_order_canceled_or_filled(order)

    def _confirm_order_canceled_or_filled(self, order: OrderRecord) -> None:
        """判断订单是成交还是取消，更新 DB 状态。

        调用 ``get_recent_trades`` 检查是否有该 order_id 的成交记录：
        - 有成交 → 标 FILLED/PARTIAL + 更新 filled_size。
        - 无成交 → 标 CANCELED + BUY 阻止重挂 / SELL 告警补挂。
        """
        try:
            trades = trading.get_recent_trades(self._client) or []
        except Exception as exc:  # noqa: BLE001
            print(
                f"[runtime] confirm order {order.intent_id[:8]} failed — API error: {exc}"
            )
            return
        # 检查是否有该 order_id 的成交
        matched_size = 0.0
        for tr in trades:
            trade_order_ids = _extract_trade_order_ids(tr)
            if order.order_id in trade_order_ids:
                matched_size += float(tr.get("size", 0) or 0)
        if matched_size > 0:
            # 部分或全部成交
            new_status = (
                OrderStatus.FILLED.value
                if matched_size >= order.size - 0.01
                else OrderStatus.PARTIAL.value
            )
            self._db_write_safe(
                "order filled (remote check)",
                lambda: self._db.update_order_status(
                    order.intent_id, new_status, filled_size=matched_size
                ),
            )
        else:
            # 取消
            self._db_write_safe(
                "order canceled (remote check)",
                lambda: self._db.update_order_status(
                    order.intent_id,
                    OrderStatus.CANCELED.value,
                    notes="remote cancel detected",
                ),
            )
            if order.side == "BUY":
                self._db_write_safe(
                    "set_entry_attempted (BUY canceled)",
                    lambda: self._db.set_entry_attempted(order.token_id, True),
                )
                if self._notifier and self._notifier.enabled:
                    self._notifier.notify(
                        f"⚠ BUY 单 {order.intent_id[:8]} 被远端取消，已阻止重挂。"
                    )
            else:  # SELL
                if self._notifier and self._notifier.enabled:
                    self._notifier.notify(
                        f"⚠ SELL 单 {order.intent_id[:8]} 被远端取消，下轮将自动补挂。"
                    )

    def _check_pending_settlement(self, t: TradingParams, market_data) -> bool:
        """检测市场是否进入 pending settlement。

        判据：``best_ask is None`` 且 ``best_bid >= 0.999``。
        返回 True 表示跳过入场（SELL 保护仍继续）。
        """
        if market_data is None:
            return False
        if (
            market_data.best_ask is None
            and market_data.best_bid is not None
            and market_data.best_bid >= 0.999
        ):
            active = t.active_market()
            if active and self._db is not None:
                if not self._db.is_entry_attempted(active.token_id):
                    self._db_write_safe(
                        "set_entry_attempted (settlement)",
                        lambda: self._db.set_entry_attempted(active.token_id, True),
                    )
                    if self._notifier and self._notifier.enabled:
                        self._notifier.notify(
                            f"⚠ {active.display()} 进入 pending settlement "
                            f"(bid={market_data.best_bid}, ask=None)。已阻止入场，等待结算。"
                        )
            return True
        return False

    # ------------------------------------------------------------------ #
    # BUY order placement (symmetric with SELL)
    # ------------------------------------------------------------------ #
    def _place_buy_order(self, t: TradingParams, active, token_id: str) -> None:
        """Record PENDING intent, call API, classify exceptions, update DB.

        Symmetric with ``_place_sell_order`` (v3.0 §2.3).
        """
        intent_id = new_intent_id()
        order_record = OrderRecord(
            intent_id=intent_id,
            session_id=self._session_id,
            token_id=token_id,
            side="BUY",
            price=t.entry_price,
            size=t.share_amount,
            status=OrderStatus.PENDING.value,
            label=active.label,
        )
        self._db_write_safe("insert BUY PENDING", lambda: self._db.insert_order(order_record))
        self._db_write_safe(
            "set_entry_attempted",
            lambda: self._db.set_entry_attempted(token_id, True),
        )

        print(f"[runtime] placing BUY {t.share_amount} @ {t.entry_price} for {active.display()}")
        try:
            resp = trading.enter_position(
                self._client, token_id, "BUY", t.share_amount, t.entry_price
            )
        except OrderTimeoutError as exc:
            # Network timeout: order may have reached exchange; mark TIMEOUT_UNCONFIRMED
            self._db_write_safe(
                "BUY TIMEOUT_UNCONFIRMED",
                lambda: self._db.update_order_status(
                    intent_id, OrderStatus.TIMEOUT_UNCONFIRMED.value, notes=str(exc)
                ),
            )
            print(f"[runtime] BUY timeout — will reconcile next cycle: {exc}")
            return
        except OrderRateLimitError as exc:
            # Rate limited: keep PENDING, back off 60s
            self._db_write_safe(
                "BUY rate limited",
                lambda: self._db.update_order_status(
                    intent_id, OrderStatus.PENDING.value, notes=f"rate limited: {exc}"
                ),
            )
            print(f"[runtime] BUY rate limited — backing off 60s: {exc}")
            time.sleep(60)
            return

        if resp is None:
            # 4xx rejected (balance insufficient, invalid price, etc.)
            self._db_write_safe(
                "BUY REJECTED",
                lambda: self._db.update_order_status(
                    intent_id, OrderStatus.REJECTED.value, notes="API rejected"
                ),
            )
            print(f"[runtime] BUY rejected by API for {active.display()}")
        else:
            order_id = str(resp.get("orderID") or resp.get("order_id") or "")
            self._db_write_safe(
                "BUY PLACED",
                lambda: self._db.update_order_status(
                    intent_id, OrderStatus.PLACED.value, order_id=order_id
                ),
            )
            print(f"[runtime] BUY placed for {active.display()} (order_id={order_id})")

    def _ensure_exit_order(
        self,
        t: TradingParams,
        active,
        token_id: str,
        position: PositionData,
    ) -> bool:
        """Return True when exit protection handled this cycle.

        Symmetric with BUY path (v3.0 §2.2): records PENDING intent, calls
        API, classifies exceptions, links to originating BUY via pair_intent_id.
        """
        if t.exit_price is None:
            print(
                "[runtime] CRITICAL — position exists but EXIT_PRICE is not configured; "
                f"manual action required for {active.display()}"
            )
            return True

        # 1. Check if there is already a working SELL intent in DB
        if self._db is not None:
            existing_sells = self._db.query_unfinished_orders(token_id=token_id, side="SELL")
            if existing_sells:
                print(
                    f"[runtime] exit pending — {len(existing_sells)} SELL order(s) working "
                    f"for {active.display()}"
                )
                return False

        # 2. Check remote open orders
        has_exit = self._has_matching_open_order(
            token_id, "SELL", price=t.exit_price, size=position.size
        )
        exit_remaining = self._matching_open_order_remaining(token_id, "SELL", t.exit_price)
        if has_exit is None or exit_remaining is None:
            print(f"[runtime] exit paused — could not confirm open orders for {active.display()}")
            return True

        # Get baseline from DB (or 0 if not set)
        baseline_remaining = 0.0
        if self._db is not None:
            pos_rec = self._db.get_position(token_id)
            if pos_rec:
                baseline_remaining = pos_rec.baseline_exit_remaining
        protected_size = max(0.0, exit_remaining - baseline_remaining)
        if protected_size + 0.05 >= position.size:
            # 已有匹配卖单覆盖持仓 → 重置失败计数
            if self._db is not None:
                self._db_write_safe("reset_failed", lambda: self._db.reset_failed(token_id))
            print(f"[runtime] exit protected — sell order already exists for {active.display()}")
            return False

        # 3. Backoff check (from DB)
        failures = 0
        if self._db is not None:
            failures = self._db.get_failed_count(token_id)
        if failures >= self._exit_backoff_threshold:
            print(
                f"[runtime] exit backed off — {failures} consecutive failures for "
                f"{active.display()}; manual intervention required"
            )
            return True

        # 4. Symmetric: record SELL PENDING intent before API call
        intent_id_sell = new_intent_id()
        buy_intent_id = None
        if self._db is not None:
            buy_intent_id = self._db.get_last_buy_filled_intent(token_id)
        sell_record = OrderRecord(
            intent_id=intent_id_sell,
            session_id=self._session_id,
            token_id=token_id,
            side="SELL",
            price=t.exit_price,
            size=position.size,
            status=OrderStatus.PENDING.value,
            label=active.label,
            pair_intent_id=buy_intent_id,
        )
        if not self._db_write_safe("insert SELL PENDING", lambda: self._db.insert_order(sell_record)):
            return True
        if buy_intent_id:
            self._db_write_safe(
                "update BUY pair_intent_id",
                lambda: self._db.update_order_pair(buy_intent_id, intent_id_sell),
            )

        # 5. Call API
        missing_size = max(0.0, position.size - protected_size)
        print(
            f"[runtime] exit protection — placing SELL for {missing_size} "
            f"{active.display()} at {t.exit_price}"
        )
        try:
            resp = trading.exit_position(
                self._client, token_id, "SELL", missing_size, t.exit_price
            )
        except OrderTimeoutError as exc:
            self._db_write_safe(
                "SELL TIMEOUT_UNCONFIRMED",
                lambda: self._db.update_order_status(
                    intent_id_sell, OrderStatus.TIMEOUT_UNCONFIRMED.value, notes=str(exc)
                ),
            )
            print(f"[runtime] SELL timeout — will reconcile next cycle: {exc}")
            return True
        except OrderRateLimitError as exc:
            self._db_write_safe(
                "SELL rate limited",
                lambda: self._db.update_order_status(
                    intent_id_sell, OrderStatus.PENDING.value, notes=f"rate limited: {exc}"
                ),
            )
            print(f"[runtime] SELL rate limited — backing off 60s: {exc}")
            time.sleep(60)
            return True

        if resp is None:
            # 4xx rejected (balance insufficient, etc.)
            self._db_write_safe(
                "SELL REJECTED",
                lambda: self._db.update_order_status(
                    intent_id_sell, OrderStatus.REJECTED.value, notes="API rejected"
                ),
            )
            self._db_write_safe(
                "incr_failed",
                lambda: self._db.incr_failed(token_id, "SELL API rejected"),
            )
            print(
                f"[runtime] SELL rejected — attempt {failures + 1}/"
                f"{self._exit_backoff_threshold} for {active.display()}"
            )
        else:
            order_id = str(resp.get("orderID") or resp.get("order_id") or "")
            self._db_write_safe(
                "SELL PLACED",
                lambda: self._db.update_order_status(
                    intent_id_sell, OrderStatus.PLACED.value, order_id=order_id
                ),
            )
            self._db_write_safe("reset_failed", lambda: self._db.reset_failed(token_id))
            print(f"[runtime] SELL placed for {active.display()} (order_id={order_id})")
        return True

    # ------------------------------------------------------------------ #
    # Fill detection + position tracking
    # ------------------------------------------------------------------ #
    def _detect_fills(self, t: TradingParams) -> bool:
        """Detect new fills via get_trades and update positions.

        v3.0: removed the ``first_run`` trap — DB persistence makes it
        unnecessary. seen_trade_ids is now queried from the DB.
        """
        active = t.active_market()
        token_id = active.token_id if active else ""
        try:
            if self._client:
                params = TradeParams(asset_id=token_id) if token_id else None
                raw_trades = self._client.get_trades(params=params) if params else self._client.get_trades()
            else:
                raw_trades = []
        except Exception as exc:  # noqa: BLE001
            print(f"[runtime] get_trades failed: {exc}")
            return False
        trades = [trading._order_to_dict(tr) for tr in raw_trades] if raw_trades else []
        if not trades:
            return True

        new_trades: list[dict[str, Any]] = []
        for tr in trades:
            tid = str(tr.get("id") or tr.get("trade_id") or tr.get("order_id") or "")
            if not tid:
                continue
            # Check DB for seen trade (replaces in-memory set)
            if self._db is not None and self._db.is_trade_seen(tid):
                continue
            # Mark as seen in DB
            if self._db is not None:
                self._db_write_safe("mark_trade_seen", lambda tid=tid: self._db.mark_trade_seen(tid))
            new_trades.append(tr)

        if not new_trades:
            return True

        for tr in new_trades:
            self._apply_fill(tr, t)
            if self._notifier and self._notifier.enabled:
                self._notifier.notify_fill(tr, t.markets)
        return True

    def _apply_fill(self, trade: dict[str, Any], t: TradingParams) -> None:
        """Apply a fill: update memory cache + DB (fill record + position).

        v3.0: also inserts a FillRecord and updates the matching OrderRecord.
        """
        token_id = str(
            trade.get("asset_id") or trade.get("token_id") or trade.get("market") or ""
        )
        if not token_id:
            return
        side = str(trade.get("side", "")).upper()
        size = _to_float(trade.get("size") or trade.get("matched_amount")) or 0.0
        price = _to_float(trade.get("price")) or 0.0
        label = next((m.label for m in t.markets if m.token_id == token_id), "")
        active = t.active_market()
        if active is None or token_id != active.token_id:
            return
        if not self._trade_matches_current_intent(trade, t, side, size, price):
            print(
                "[runtime] ignored fill that does not match current bot intent "
                f"({active.display()} side={side} size={size} price={price})"
            )
            return

        # Update memory cache
        with self._lock:
            pos = self._positions_cache.setdefault(
                token_id, {"size": 0.0, "avg_price": 0.0, "label": label}
            )
            if not label:
                pos["label"] = label
            if side.startswith("B"):
                old_size = pos["size"]
                old_avg = pos["avg_price"]
                new_size = old_size + size
                pos["avg_price"] = (
                    (old_avg * old_size + price * size) / new_size if new_size > 0 else price
                )
                pos["size"] = new_size
            elif side.startswith("S"):
                pos["size"] = max(0.0, pos["size"] - size)

        # Persist to DB: fill record + position update + order status
        if self._db is None:
            return

        # Find matching order in DB by order_id
        trade_order_ids = _extract_trade_order_ids(trade)
        matching_order = None
        if trade_order_ids:
            matches = self._db.query_orders_by_order_ids(trade_order_ids)
            if matches:
                matching_order = matches[0]

        # Insert fill record
        if matching_order:
            fill_rec = FillRecord(
                trade_id=str(trade.get("id") or trade.get("trade_id") or ""),
                token_id=token_id,
                side=side,
                size=size,
                price=price,
                order_id=matching_order.order_id,
                intent_id=matching_order.intent_id,
                raw_trade=json.dumps(trade, default=str) if trade else None,
            )
            self._db_write_safe("insert_fill", lambda: self._db.insert_fill(fill_rec))

            # Update order filled_size + status
            new_filled_total = matching_order.filled_size + size
            is_full = new_filled_total >= matching_order.size - 0.05
            self._db_write_safe(
                "update_order_filled",
                lambda: self._db.update_order_filled(
                    matching_order.intent_id, size, new_filled_total, is_full
                ),
            )

        # Update position in DB
        with self._lock:
            current = self._positions_cache.get(token_id, {})
        current_size = current.get("size", 0.0)
        current_avg = current.get("avg_price", 0.0)
        pos_rec = PositionRecord(
            token_id=token_id,
            label=label or active.label,
            size=current_size,
            avg_price=current_avg,
        )
        self._db_write_safe("upsert_position", lambda: self._db.upsert_position(pos_rec))

        # If SELL fully filled, fully reset position state (size/avg/baselines/entry_attempted/failed_attempts)
        if side.startswith("S") and current_size <= 0.01:
            self._db_write_safe("reset_position_state", lambda: self._db.reset_position_state(token_id))
            print(f"[runtime] position state fully reset for {active.display()} (SELL filled — ready for re-entry)")

    def _reconcile_position_from_portfolio(self, t: TradingParams) -> None:
        """Use Polymarket portfolio positions as a fallback fill detector."""
        active = t.active_market()
        if active is None or not active.token_id:
            return
        portfolio_position = self._get_portfolio_position(active.token_id)
        if portfolio_position is None:
            return
        size, avg_price = portfolio_position
        if size <= 0:
            return

        managed_size = size
        managed_avg = avg_price
        # 查 DB 的 baseline + entry_attempted（替代内存字典）
        db_pos = self._db.get_position(active.token_id) if self._db is not None else None
        baseline_size = db_pos.baseline_position_size if db_pos else 0.0
        baseline_avg = db_pos.baseline_position_avg if db_pos else 0.0
        entry_attempted = (
            self._db.is_entry_attempted(active.token_id) if self._db is not None else False
        )
        if baseline_size > 0 and entry_attempted:
            if size <= baseline_size + 0.01:
                return
            managed_size = size - baseline_size
            managed_cost = max(0.0, size * avg_price - baseline_size * baseline_avg)
            managed_avg = managed_cost / managed_size if managed_size > 0 else t.entry_price
        elif not self._portfolio_position_matches_current_intent(
            active.token_id, size, avg_price, t
        ):
            return

        with self._lock:
            current = self._positions_cache.get(active.token_id, {})
            current_size = current.get("size", 0.0) if current else 0.0
            if current_size >= managed_size:
                return
            self._positions_cache[active.token_id] = {
                "size": managed_size,
                "avg_price": managed_avg,
                "label": active.display(),
            }
        # 同步持仓到 DB
        if self._db is not None:
            pos_rec = PositionRecord(
                token_id=active.token_id,
                label=active.display(),
                size=managed_size,
                avg_price=managed_avg,
            )
            self._db_write_safe(
                "upsert_position (portfolio recon)",
                lambda: self._db.upsert_position(pos_rec),
            )
        print(
            "[runtime] portfolio reconciliation detected managed position "
            f"for {active.display()} ({managed_size} @ {managed_avg})"
        )
        return

    def _remember_layer_baseline(self, t: TradingParams, token_id: str) -> bool:
        position = self._get_portfolio_position(token_id)
        if position is None:
            return False
        baseline_size, baseline_avg = position
        exit_remaining = self._matching_open_order_remaining(token_id, "SELL", t.exit_price)
        if exit_remaining is None:
            return False
        if self._db is not None:
            ok = self._db_write_safe(
                "set_baselines",
                lambda: self._db.set_baselines(
                    token_id, baseline_size, baseline_avg, exit_remaining
                ),
            )
            if not ok:
                return False
        return True

    def _get_portfolio_position(self, token_id: str) -> tuple[float, float] | None:
        funder = self._guard.config.funder
        if not funder:
            return (0.0, 0.0)
        try:
            response = httpx.get(
                "https://data-api.polymarket.com/positions",
                params={"user": funder, "limit": 200, "sizeThreshold": 0},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[runtime] portfolio position check failed: {exc}")
            return None

        items = data if isinstance(data, list) else []
        for item in items:
            item_token_id = str(
                item.get("asset") or item.get("assetId") or item.get("tokenId") or ""
            )
            if item_token_id == token_id:
                return (
                    _to_float(item.get("size")) or 0.0,
                    _to_float(item.get("avgPrice")) or 0.0,
                )
        return (0.0, 0.0)

    def _portfolio_position_matches_current_intent(
        self,
        token_id: str,
        size: float,
        avg_price: float,
        t: TradingParams,
    ) -> bool:
        entry_attempted = (
            self._db.is_entry_attempted(token_id) if self._db is not None else False
        )
        if entry_attempted:
            return avg_price <= t.entry_price + 0.02 and size >= 0.01
        return (
            avg_price <= t.entry_price + 0.02
            and _close_enough(size, t.share_amount, min_abs=0.05, rel=0.02)
        )

    def _get_position(self, token_id: str) -> PositionData:
        with self._lock:
            pos = self._positions_cache.get(token_id)
            if not pos:
                return PositionData(token_id=token_id)
            return PositionData(
                token_id=token_id,
                size=pos.get("size", 0.0),
                avg_price=pos.get("avg_price", 0.0),
            )

    def _has_matching_open_order(
        self,
        token_id: str,
        side: str,
        *,
        price: float | None,
        size: float | None,
    ) -> bool | None:
        """Return whether a similar resting order exists, or None when unknown."""
        try:
            orders = self._client.get_open_orders() if self._client else []
        except Exception:  # noqa: BLE001
            return None
        for o in orders:
            if not isinstance(o, dict):
                o = trading._order_to_dict(o)
            o_token = str(
                o.get("asset_id") or o.get("token_id") or o.get("market") or ""
            )
            o_side = str(o.get("side", "")).upper()
            if o_token != token_id or o_side != side.upper():
                continue
            o_price = _to_float(o.get("price"))
            o_size = _to_float(
                o.get("original_size") or o.get("size") or o.get("remaining_size")
            )
            if _close_enough(o_price, price, min_abs=0.005, rel=0.02) and _close_enough(
                o_size, size, min_abs=0.05, rel=0.02
            ):
                return True
        return False

    def _matching_open_order_remaining(
        self,
        token_id: str,
        side: str,
        price: float | None,
    ) -> float | None:
        try:
            orders = self._client.get_open_orders() if self._client else []
        except Exception:  # noqa: BLE001
            return None
        remaining_total = 0.0
        for o in orders:
            if not isinstance(o, dict):
                o = trading._order_to_dict(o)
            o_token = str(
                o.get("asset_id") or o.get("token_id") or o.get("market") or ""
            )
            o_side = str(o.get("side", "")).upper()
            o_price = _to_float(o.get("price"))
            if o_token != token_id or o_side != side.upper():
                continue
            if not _close_enough(o_price, price, min_abs=0.005, rel=0.02):
                continue
            original_size = _to_float(o.get("original_size") or o.get("size")) or 0.0
            matched_size = _to_float(o.get("size_matched")) or 0.0
            remaining_total += max(0.0, original_size - matched_size)
        return remaining_total

    def _trade_matches_current_intent(
        self,
        trade: dict[str, Any],
        t: TradingParams,
        side: str,
        size: float,
        price: float,
    ) -> bool:
        # 收集 trade 涉及的所有 order_id（taker + 所有 maker）。
        # CLOB v2 Trade 对象无顶层 order_id，分散在 taker_order_id 和
        # maker_orders[].order_id 中。任一命中 bot 的 order bucket 即为 managed。
        trade_order_ids = _extract_trade_order_ids(trade)
        # 从 DB 查询该 token 的 PLACED/PARTIAL 订单 order_id 集合（替代内存 set）
        active_token = t.active_market().token_id if t.active_market() else ""
        managed_order_ids: set[str] = set()
        if self._db is not None and active_token:
            side_filter = "BUY" if side.startswith("B") else "SELL"
            unfinished = self._db.query_unfinished_orders(
                token_id=active_token, side=side_filter
            )
            for o in unfinished:
                if o.order_id:
                    managed_order_ids.add(o.order_id)
        if trade_order_ids and managed_order_ids:
            return bool(trade_order_ids & managed_order_ids)

        if side.startswith("B"):
            target_notional = t.share_amount * t.entry_price
            trade_notional = size * price
            return (
                price <= t.entry_price + 0.01
                and _close_enough(size, t.share_amount, min_abs=0.05, rel=0.02)
                and _close_enough(trade_notional, target_notional, min_abs=1.0, rel=0.10)
            )

        if side.startswith("S"):
            with self._lock:
                position = self._positions_cache.get(active_token)
                current_size = position.get("size", 0.0) if position else 0.0
            expected_price = t.exit_price
            return current_size > 0 and _close_enough(price, expected_price, min_abs=0.005, rel=0.02)

        return False

    # ------------------------------------------------------------------ #
    # Open-orders change notification
    # ------------------------------------------------------------------ #
    def _maybe_notify_open_orders(self, t: TradingParams) -> None:
        if not self._notifier or not self._notifier.enabled:
            return
        try:
            open_orders = trading.get_open_orders(self._client) if self._client else []
        except Exception:  # noqa: BLE001
            return
        sig = ",".join(
            sorted(
                str(o.get("id", "")) + "|" + str(o.get("status", ""))
                for o in open_orders
            )
        )
        if sig != self._open_orders_sig:
            self._open_orders_sig = sig
            self._notifier.notify_open_orders(open_orders, t.markets)

    def _push_status(self) -> None:
        with self._lock:
            positions = [
                {
                    "token_id": tid,
                    "label": p.get("label", ""),
                    "size": p.get("size", 0.0),
                    "avg_price": p.get("avg_price", 0.0),
                }
                for tid, p in self._positions_cache.items()
                if p.get("size", 0.0) > 0
            ]
        t = self._guard.snapshot()
        active = t.active_market()
        self._notifier.notify_status(t.markets, active, positions, t, self._cycle)


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
    """从 CLOB v2 Trade 对象收集所有相关 order_id。

    Trade 对象无顶层 order_id，分散在：
    - taker_order_id：taker 侧订单 id
    - maker_orders[].order_id：maker 侧订单 id 数组
    - 兼容旧字段 order_id / orderID（如有）

    bot 的限价单成交时通常是 maker，故必须检查 maker_orders 数组，
    否则 maker 成交会漏检，导致 position 不更新、重复挂 SELL 等问题。
    """
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
