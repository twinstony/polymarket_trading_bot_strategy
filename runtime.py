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

The runtime is also the source of truth the interactive Telegram bot queries
through ``status_snapshot``.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx
import trading
from config import ConfigGuard, TradingParams
from py_clob_client_v2.clob_types import TradeParams
from strategy import PositionData, should_enter


class BotRuntime:
    def __init__(self, client, config_guard: ConfigGuard, notifier):
        self._client = client
        self._guard = config_guard
        self._notifier = notifier

        # Local state --------------------------------------------------------
        # token_id -> {"size": float, "avg_price": float, "label": str}
        self._positions: dict[str, dict[str, Any]] = {}
        self._seen_trade_ids: set[str] = set()
        self._entry_attempted_tokens: set[str] = set()
        self._entry_order_ids: set[str] = set()
        self._exit_order_ids: set[str] = set()
        self._entry_position_baselines: dict[str, tuple[float, float]] = {}
        self._exit_order_baselines: dict[str, float] = {}
        self._open_orders_sig: str = ""
        self._cycle = 0
        # SELL 下单失败退避：token_id -> 连续失败次数。连续失败 >= 阈值时
        # 跳过 exit protection 下单，避免余额不足时连续 400 重试。
        self._failed_exit_attempts: dict[str, int] = {}
        self._exit_backoff_threshold = 2

        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None

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
                for tid, p in self._positions.items()
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

        active = self._guard.active_market()
        if self._notifier and self._notifier.enabled:
            self._notifier.notify_started(active)

        while not self._stop.is_set():
            self._cycle += 1
            try:
                self._cycle_once()
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                print(f"[runtime] cycle {self._cycle} error: {exc}")

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

        # 2. Fetch live market data.
        market_data = trading.get_market_data(self._client, token_id)

        # 3. Exit protection comes before any new entry. If the bot sees a
        # managed position, it must keep a matching SELL order working.
        position = self._get_position(token_id)
        if position.has_position:
            if self._ensure_exit_order(t, active, token_id, position):
                return
            print(f"[runtime] holding {active.display()} ({position.size} @ {position.avg_price})")
            self._maybe_notify_open_orders(t)
            return

        # 4. Entry check (only when not already holding this token).
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
            elif token_id in self._entry_attempted_tokens:
                print(f"[runtime] entry skipped — entry already attempted for {active.display()}")
            else:
                if t.conditional_entry:
                    # Conditional entry: wait until market ask is at/below entry price.
                    if should_enter(market_data, t.entry_price):
                        print(f"[runtime] entry signal for {active.display()}")
                        if not self._remember_layer_baseline(t, token_id):
                            print(f"[runtime] entry paused — could not record baseline for {active.display()}")
                            return
                        self._entry_attempted_tokens.add(token_id)
                        resp = trading.enter_position(
                            self._client, token_id, "BUY", t.share_amount, t.entry_price
                        )
                        self._remember_order_id(resp, self._entry_order_ids)
                    else:
                        print(f"[runtime] no entry signal for {active.display()}")
                else:
                    # Direct mode: immediately place a limit buy at entry price.
                    print(f"[runtime] direct entry for {active.display()} (conditional_entry=off)")
                    if not self._remember_layer_baseline(t, token_id):
                        print(f"[runtime] entry paused — could not record baseline for {active.display()}")
                        return
                    self._entry_attempted_tokens.add(token_id)
                    resp = trading.enter_position(
                        self._client, token_id, "BUY", t.share_amount, t.entry_price
                    )
                    self._remember_order_id(resp, self._entry_order_ids)

        # 5. Open-orders change notification.
        self._maybe_notify_open_orders(t)

    def _ensure_exit_order(
        self,
        t: TradingParams,
        active,
        token_id: str,
        position: PositionData,
    ) -> bool:
        """Return True when exit protection handled this cycle."""
        if t.exit_price is None:
            print(
                "[runtime] CRITICAL — position exists but EXIT_PRICE is not configured; "
                f"manual action required for {active.display()}"
            )
            return True

        has_exit = self._has_matching_open_order(
            token_id, "SELL", price=t.exit_price, size=position.size
        )
        exit_remaining = self._matching_open_order_remaining(token_id, "SELL", t.exit_price)
        if has_exit is None or exit_remaining is None:
            print(f"[runtime] exit paused — could not confirm open orders for {active.display()}")
            return True
        baseline_remaining = self._exit_order_baselines.get(token_id, 0.0)
        protected_size = max(0.0, exit_remaining - baseline_remaining)
        if protected_size + 0.05 >= position.size:
            # 已有匹配卖单覆盖持仓 → 重置失败计数（状态恢复正常）
            self._failed_exit_attempts[token_id] = 0
            print(f"[runtime] exit protected — sell order already exists for {active.display()}")
            return False

        # 退避检查：连续失败达阈值时跳过下单，避免余额不足连续 400
        failures = self._failed_exit_attempts.get(token_id, 0)
        if failures >= self._exit_backoff_threshold:
            print(
                f"[runtime] exit backed off — {failures} consecutive failures for "
                f"{active.display()}; manual intervention required"
            )
            return True

        missing_size = max(0.0, position.size - protected_size)
        print(f"[runtime] exit protection — placing SELL for {missing_size} {active.display()} at {t.exit_price}")
        resp = trading.exit_position(
            self._client, token_id, "SELL", missing_size, t.exit_price
        )
        if resp is None:
            # 下单失败（如余额不足 400）→ 累计失败次数
            self._failed_exit_attempts[token_id] = failures + 1
            print(
                f"[runtime] exit order failed — attempt {failures + 1}/"
                f"{self._exit_backoff_threshold} for {active.display()}"
            )
        else:
            # 下单成功 → 重置失败计数
            self._failed_exit_attempts[token_id] = 0
            self._remember_order_id(resp, self._exit_order_ids)
        return True

    # ------------------------------------------------------------------ #
    # Fill detection + position tracking
    # ------------------------------------------------------------------ #
    def _detect_fills(self, t: TradingParams) -> bool:
        # 用 asset_id 按 token 过滤，避免拉取全市场公开成交流水（实测
        # maker_address 参数服务端过滤无效，仅 asset_id 有效）。
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
                self._apply_fill(tr, t)
            else:
                new_trades.append(tr)

        if not new_trades:
            return True

        for tr in new_trades:
            self._apply_fill(tr, t)
            if self._notifier and self._notifier.enabled:
                self._notifier.notify_fill(tr, t.markets)
        return True

    def _extract_bot_fills(
        self, trade: dict[str, Any]
    ) -> list[tuple[str, float, float]]:
        """从一笔 trade 中提取 Bot 自己的成交记录。

        返回 ``[(direction, size, price), ...]``，其中：

        - ``direction`` 为 Bot 的实际方向（``"BUY"`` 或 ``"SELL"``），而非
          CLOB Trade 对象顶层的 ``side``（那是 taker 视角）。
        - ``size`` 为 Bot 实际成交股数。Bot 作为 maker 时取自
          ``maker_orders[].size``（Bot 自己那部分），而非 trade 顶层 size
          （那是 taker 总成交量，可能包含其他 maker 的成交）。
        - ``price`` 为成交价。

        方向判定依据命中的 order_id bucket：
        - 命中 ``_entry_order_ids`` → Bot 的 BUY 限价单被吃 → ``"BUY"``
        - 命中 ``_exit_order_ids`` → Bot 的 SELL 限价单被吃 → ``"SELL"``

        Bot 作为 taker 时方向由 ``trade.side`` 决定，size 取自 trade 顶层。

        若无 order_id 命中（buckets 为空或 trade 不含 Bot 订单），返回空列表，
        由调用方走启发式 fallback。
        """
        fills: list[tuple[str, float, float]] = []
        trade_price = _to_float(trade.get("price")) or 0.0

        # 1. Bot 作为 taker（trade.side 即 Bot 方向，size 为 taker 总成交量）
        taker_id = str(trade.get("taker_order_id") or "")
        if taker_id:
            taker_size = _to_float(
                trade.get("size") or trade.get("matched_amount")
            ) or 0.0
            if taker_id in self._entry_order_ids:
                fills.append(("BUY", taker_size, trade_price))
            elif taker_id in self._exit_order_ids:
                fills.append(("SELL", taker_size, trade_price))

        # 2. Bot 作为 maker（方向由命中的 bucket 决定，size 取自 maker_orders）
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

    def _update_position(
        self, token_id: str, label: str, side: str, size: float, price: float
    ) -> None:
        """根据一笔 Bot 成交更新本地持仓。side 为 Bot 实际方向（BUY/SELL）。"""
        with self._lock:
            pos = self._positions.setdefault(
                token_id, {"size": 0.0, "avg_price": 0.0, "label": label}
            )
            if not label:
                pos["label"] = label
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

    def _apply_fill(self, trade: dict[str, Any], t: TradingParams) -> None:
        token_id = str(
            trade.get("asset_id") or trade.get("token_id") or trade.get("market") or ""
        )
        if not token_id:
            return
        side = str(trade.get("side", "")).upper()
        trade_size = _to_float(trade.get("size") or trade.get("matched_amount")) or 0.0
        price = _to_float(trade.get("price")) or 0.0
        label = next((m.label for m in t.markets if m.token_id == token_id), "")
        active = t.active_market()
        if active is None or token_id != active.token_id:
            return

        # 优先：基于 order_id 的精确匹配。
        # 解决两个核心问题：
        # 1. trade.side 是 taker 视角，Bot 作为 maker 的 SELL 被吃时 side="BUY"，
        #    但 Bot 实际方向是 SELL，应减少持仓。
        # 2. trade.size 是 taker 总成交量（可能包含多个 maker），Bot 作为 maker
        #    只成交了 maker_orders[].size 那部分。
        bot_fills = self._extract_bot_fills(trade)
        if bot_fills:
            for fill_side, fill_size, fill_price in bot_fills:
                self._update_position(token_id, label, fill_side, fill_size, fill_price)
                print(
                    "[runtime] applied bot fill "
                    f"({active.display()} side={fill_side} "
                    f"size={fill_size} price={fill_price})"
                )
            return

        # Fallback：无 order_id 命中。
        # 若 trade 含 order_id 且 buckets 非空但未命中 → 别人的成交，直接忽略，
        # 不走启发式（避免误判）。
        trade_order_ids = _extract_trade_order_ids(trade)
        has_buckets = bool(self._entry_order_ids or self._exit_order_ids)
        if trade_order_ids and has_buckets:
            print(
                "[runtime] ignored fill that does not match current bot intent "
                f"({active.display()} side={side} size={trade_size} price={price})"
            )
            return

        # 无 order_id 信息或 buckets 为空 → 走启发式匹配（重启后无状态场景）
        if not self._trade_matches_current_intent(trade, t, side, trade_size, price):
            print(
                "[runtime] ignored fill that does not match current bot intent "
                f"({active.display()} side={side} size={trade_size} price={price})"
            )
            return

        self._update_position(token_id, label, side, trade_size, price)

    def _reconcile_position_from_portfolio(self, t: TradingParams) -> None:
        """Use Polymarket portfolio positions as a fallback fill detector.

        双向修正：
        - 向上修正：portfolio 持仓 > 本地持仓 → BUY 成交未被本地捕获
        - 向下修正：portfolio 持仓 < 本地持仓 → SELL 成交未被本地捕获
          （如 maker SELL 被吃但 trade.side=BUY 导致 _apply_fill 漏检）
        """
        active = t.active_market()
        if active is None or not active.token_id:
            return
        portfolio_position = self._get_portfolio_position(active.token_id)
        if portfolio_position is None:
            return
        size, avg_price = portfolio_position

        with self._lock:
            current = self._positions.get(active.token_id, {})
            current_size = current.get("size", 0.0) if current else 0.0

        # 向下修正：portfolio 显示持仓 < 本地持仓 → SELL 成交未被本地捕获
        # （含 portfolio 显示 0 股的完全平仓场景）
        if size < current_size - 0.05:
            with self._lock:
                if size <= 0:
                    self._positions.pop(active.token_id, None)
                else:
                    self._positions[active.token_id] = {
                        "size": size,
                        "avg_price": avg_price,
                        "label": active.display(),
                    }
            print(
                "[runtime] portfolio reconciliation corrected position downward "
                f"for {active.display()} (local={current_size} -> portfolio={size})"
            )
            return

        if size <= 0:
            return

        # 向上修正（原有逻辑）：检测 BUY 成交
        managed_size = size
        managed_avg = avg_price
        baseline = self._entry_position_baselines.get(active.token_id)
        if baseline and active.token_id in self._entry_attempted_tokens:
            baseline_size, baseline_avg = baseline
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
            current = self._positions.get(active.token_id, {})
            current_size = current.get("size", 0.0) if current else 0.0
            if current_size >= managed_size:
                return
            self._positions[active.token_id] = {
                "size": managed_size,
                "avg_price": managed_avg,
                "label": active.display(),
            }
        print(
            "[runtime] portfolio reconciliation detected managed position "
            f"for {active.display()} ({managed_size} @ {managed_avg})"
        )
        return

    def _remember_layer_baseline(self, t: TradingParams, token_id: str) -> bool:
        position = self._get_portfolio_position(token_id)
        if position is None:
            return False
        self._entry_position_baselines[token_id] = position
        exit_remaining = self._matching_open_order_remaining(token_id, "SELL", t.exit_price)
        if exit_remaining is None:
            return False
        self._exit_order_baselines[token_id] = exit_remaining
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
        if token_id in self._entry_attempted_tokens:
            return avg_price <= t.entry_price + 0.02 and size >= 0.01
        return (
            avg_price <= t.entry_price + 0.02
            and _close_enough(size, t.share_amount, min_abs=0.05, rel=0.02)
        )

    def _get_position(self, token_id: str) -> PositionData:
        with self._lock:
            pos = self._positions.get(token_id)
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
        """启发式 fallback 匹配（无 order_id 或 buckets 为空时使用）。

        注意：order_id 精确匹配已在 ``_apply_fill`` 中通过 ``_extract_bot_fills``
        完成。此方法仅作为 fallback，基于价格/数量/持仓状态做启发式判断，
        不再依赖 trade.side 做 order_id 分流（那是 taker 视角，Bot 作为 maker
        时方向相反，会导致 SELL 成交被误判为"不匹配"而忽略）。
        """
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
                position = self._positions.get(t.active_market().token_id if t.active_market() else "")
                current_size = position.get("size", 0.0) if position else 0.0
            expected_price = t.exit_price
            return current_size > 0 and _close_enough(price, expected_price, min_abs=0.005, rel=0.02)

        return False

    def _remember_order_id(self, response: Any, bucket: set[str]) -> None:
        if not isinstance(response, dict):
            return
        order_id = str(response.get("orderID") or response.get("order_id") or "")
        if order_id:
            bucket.add(order_id)

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
                for tid, p in self._positions.items()
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
