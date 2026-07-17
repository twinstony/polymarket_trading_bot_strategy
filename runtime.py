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

import trading
from config import ConfigGuard, TradingParams
from strategy import PositionData, should_enter, should_exit


class BotRuntime:
    def __init__(self, client, config_guard: ConfigGuard, notifier):
        self._client = client
        self._guard = config_guard
        self._notifier = notifier

        # Local state --------------------------------------------------------
        # token_id -> {"size": float, "avg_price": float, "label": str}
        self._positions: dict[str, dict[str, Any]] = {}
        self._seen_trade_ids: set[str] = set()
        self._open_orders_sig: str = ""
        self._cycle = 0

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
        t = self._guard.snapshot()
        active = t.active_market()
        if active is None or not active.token_id:
            print("[runtime] no active market configured; skipping cycle")
            return

        token_id = active.token_id

        # 1. Detect fills first so positions are current before decisions.
        self._detect_fills(t)

        # 2. Fetch live market data.
        market_data = trading.get_market_data(self._client, token_id)

        # 3. Entry check (only when not already holding this token).
        position = self._get_position(token_id)
        if not position.has_position:
            # Guard: don't place another entry order if one is already resting.
            if self._has_open_entry_order(token_id):
                print(f"[runtime] entry skipped — open order already exists for {active.display()}")
            else:
                if t.conditional_entry:
                    # Conditional entry: wait until market ask is at/below entry price.
                    if should_enter(market_data, t.entry_price):
                        print(f"[runtime] entry signal for {active.display()}")
                        trading.enter_position(
                            self._client, token_id, "BUY", t.share_amount, t.entry_price
                        )
                    else:
                        print(f"[runtime] no entry signal for {active.display()}")
                else:
                    # Direct mode: immediately place a limit buy at entry price.
                    print(f"[runtime] direct entry for {active.display()} (conditional_entry=off)")
                    trading.enter_position(
                        self._client, token_id, "BUY", t.share_amount, t.entry_price
                    )
        else:
            # 4. Exit check.
            if should_exit(
                position,
                market_data,
                exit_price=t.exit_price,
                take_profit_pct=t.take_profit_pct,
                stop_loss_pct=t.stop_loss_pct,
            ):
                print(f"[runtime] exit signal for {active.display()}")
                trading.exit_position(
                    self._client, token_id, "SELL", position.size, t.exit_price
                )
            else:
                print(f"[runtime] holding {active.display()} ({position.size} @ {position.avg_price})")

        # 5. Open-orders change notification.
        self._maybe_notify_open_orders(t)

    # ------------------------------------------------------------------ #
    # Fill detection + position tracking
    # ------------------------------------------------------------------ #
    def _detect_fills(self, t: TradingParams) -> None:
        trades = trading.get_recent_trades(self._client) if self._client else []
        if not trades:
            return

        first_run = not self._seen_trade_ids
        new_trades: list[dict[str, Any]] = []
        for tr in trades:
            tid = str(tr.get("id") or tr.get("trade_id") or tr.get("order_id") or "")
            if not tid:
                continue
            if tid in self._seen_trade_ids:
                continue
            self._seen_trade_ids.add(tid)
            if not first_run:
                new_trades.append(tr)

        if not new_trades:
            return

        for tr in new_trades:
            self._apply_fill(tr, t)
            if self._notifier and self._notifier.enabled:
                self._notifier.notify_fill(tr, t.markets)

    def _apply_fill(self, trade: dict[str, Any], t: TradingParams) -> None:
        token_id = str(
            trade.get("asset_id") or trade.get("token_id") or trade.get("market") or ""
        )
        if not token_id:
            return
        side = str(trade.get("side", "")).upper()
        size = _to_float(trade.get("size") or trade.get("matched_amount")) or 0.0
        price = _to_float(trade.get("price")) or 0.0
        label = next((m.label for m in t.markets if m.token_id == token_id), "")

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
                    (old_avg * old_size + price * size) / new_size if new_size > 0 else price
                )
                pos["size"] = new_size
            elif side.startswith("S"):
                pos["size"] = max(0.0, pos["size"] - size)

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

    def _has_open_entry_order(self, token_id: str) -> bool:
        """Return True if there is already a resting order for this token."""
        try:
            orders = trading.get_open_orders(self._client) if self._client else []
        except Exception:  # noqa: BLE001
            return False
        for o in orders:
            o_token = str(
                o.get("asset_id") or o.get("token_id") or o.get("market") or ""
            )
            if o_token == token_id:
                return True
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
