"""
Startup reconciliation and crash-recovery logic.

Flow:
1. ``start_session()``       - create a session row in the DB.
2. ``show_state_summary()``  - print unfinished orders / positions / last session.
3. ``countdown_window()``    - 15s (configurable) user-intervention window:
                                 Enter -> RESUME, 'f' -> FRESH_START, timeout -> RESUME.
4. If FRESH_START: ``fresh_start()`` (archive + clear), then enter main loop.
5. If RESUME:
   a. ``archive_old_data()``   - archive stale closed orders/fills.
   b. ``reconcile()``          - force 3-way reconciliation (open orders,
                                 portfolio, recent trades) against Polymarket.
   c. ``handle_unfinished_orders()`` - resolve PENDING / TIMEOUT_UNCONFIRMED /
                                 unpaired BUY-FILLED orders.
   d. ``check_critical_and_wait()`` - if CRITICAL mismatches found, pause for
                                 user y/n confirmation before entering the loop.

All reconciliation results are written to the ``reconciliations`` audit table.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any
from uuid import uuid4

import httpx
import trading
from order_state import OrderStatus, new_intent_id


class Recovery:
    """Startup reconciliation and crash-recovery coordinator."""

    def __init__(self, persistence, client, config, notifier=None):
        self._db = persistence
        self._client = client
        self._config = config
        self._notifier = notifier
        self._session_id = new_intent_id()
        self._critical_items: list[str] = []

    # =================================================================== #
    # Session
    # =================================================================== #
    def start_session(self) -> str:
        """Create a session row and return the session_id."""
        self._db.insert_session(self._session_id, "RESUME", "SUCCESS")
        return self._session_id

    def finish_session(self, status: str, last_cycle: int) -> None:
        self._db.update_session_end(self._session_id, status, last_cycle)

    # =================================================================== #
    # State summary
    # =================================================================== #
    def show_state_summary(self) -> None:
        """Print a summary of persisted state for the user."""
        print("\n" + "=" * 60)
        print("[recovery] PERSISTED STATE SUMMARY")
        print("=" * 60)

        # Last session
        last = self._db.get_last_session()
        if last:
            print(f"  Last session: {last['session_id'][:8]}  started={last['started_at']}  "
                  f"mode={last['recovery_mode']}  status={last['recovery_status']}")
        else:
            print("  No previous sessions (fresh database).")

        # Unfinished orders
        unfinished = self._db.query_unfinished_orders()
        if unfinished:
            print(f"\n  Unfinished orders ({len(unfinished)}):")
            for o in unfinished:
                print(f"    {o.side:4s} {o.status:20s} {o.token_id[:16]}... "
                      f"size={o.size} filled={o.filled_size} order_id={o.order_id or 'N/A'}")
        else:
            print("\n  No unfinished orders.")

        # Buy filled without paired SELL
        unpaired = self._db.query_buy_filled_without_sell()
        if unpaired:
            print(f"\n  ⚠ BUY FILLED without paired SELL ({len(unpaired)}):")
            for o in unpaired:
                print(f"    intent={o.intent_id[:8]}  token={o.token_id[:16]}...  "
                      f"size={o.size} filled={o.filled_size}")

        # Positions
        positions = self._db.get_all_positions()
        if positions:
            print(f"\n  Open positions ({len(positions)}):")
            for p in positions:
                print(f"    {p.token_id[:16]}...  size={p.size}  avg={p.avg_price}  "
                      f"entry_attempted={p.entry_attempted}")
        else:
            print("\n  No open positions.")

        print("=" * 60 + "\n")

    # =================================================================== #
    # Countdown window (user intervention)
    # =================================================================== #
    def countdown_window(self, timeout_sec: int) -> str:
        """Display a countdown and wait for user input.

        Returns ``"RESUME"`` or ``"FRESH_START"``.
        """
        print(f"\n[recovery] Starting in {timeout_sec}s. Options:")
        print(f"  Enter  -> RESUME (reconcile + auto-recover)")
        print(f"  f      -> FRESH_START (archive + clear DB, requires confirmation)")
        print(f"  (timeout -> RESUME)\n")

        result: list[str | None] = [None]

        def _read_input():
            try:
                line = input().strip().lower()
                result[0] = line
            except (EOFError, OSError):
                result[0] = ""

        reader = threading.Thread(target=_read_input, daemon=True)
        reader.start()

        for remaining in range(timeout_sec, 0, -1):
            if result[0] is not None:
                break
            sys.stdout.write(f"\r[recovery] Auto-RESUME in {remaining:2d}s... ")
            sys.stdout.flush()
            time.sleep(1)

        print()  # newline after countdown

        if result[0] is None:
            print("[recovery] Countdown ended -> RESUME (default)")
            return "RESUME"

        choice = result[0]
        if choice in ("f", "fresh", "fresh_start"):
            return self._confirm_fresh_start()
        print("[recovery] User chose RESUME")
        return "RESUME"

    def _confirm_fresh_start(self) -> str:
        """Two-step confirmation for FRESH_START. Returns mode string."""
        print("\n[recovery] ⚠ FRESH_START will archive ALL current orders/fills/positions")
        print("[recovery]   and clear the main tables. This cannot be undone.")
        print("[recovery]   Sessions and reconciliation history will be preserved.")
        confirm = input("[recovery] Type 'yes' to confirm FRESH_START, else cancel: ").strip().lower()
        if confirm == "yes":
            print("[recovery] FRESH_START confirmed")
            return "FRESH_START"
        print("[recovery] FRESH_START cancelled -> RESUME")
        return "RESUME"

    # =================================================================== #
    # Fresh start
    # =================================================================== #
    def fresh_start(self) -> None:
        """Archive all current data and clear main tables.

        After clearing, force 3-way reconciliation to sync remote state
        (open orders / portfolio / recent trades) back into the empty DB.
        Then handle any unfinished orders and check for CRITICAL mismatches.
        """
        result = self._db.fresh_start()
        print(f"[recovery] FRESH_START complete: "
              f"archived {result['archived_orders']} orders, "
              f"{result['archived_fills']} fills")
        self._db.update_session_mode(self._session_id, "FRESH_START")
        if self._notifier and self._notifier.enabled:
            self._notifier.notify(
                f"⚠ FRESH_START executed. Archived {result['archived_orders']} orders, "
                f"{result['archived_fills']} fills. Main tables cleared."
            )
        # v4: 强制对账同步远端状态（清空后远端挂单/持仓需拉回 DB）
        print("[recovery] FRESH_START 后执行强制对账...")
        self.reconcile(mode="FRESH_START")
        self.handle_unfinished_orders()
        self.check_critical_and_wait()

    # =================================================================== #
    # Archival
    # =================================================================== #
    def archive_old_data(self) -> None:
        """Archive stale data before reconciliation."""
        result = self._db.archive_old_data(
            order_retention_days=self._config.archive_retention_days,
            seen_trades_retention_days=self._config.seen_trades_retention_days,
            recon_retention_days=self._config.recon_retention_days,
        )
        if any(result.values()):
            print(f"[recovery] Archive: {result}")
        else:
            print("[recovery] Archive: nothing to archive")

    # =================================================================== #
    # Reconciliation (3-way: open orders / portfolio / recent trades)
    # =================================================================== #
    def reconcile(self, mode: str = "RESUME") -> None:
        """Force 3-way reconciliation against Polymarket remote state."""
        print(f"\n[recovery] === RECONCILIATION (mode={mode}) ===")
        self._critical_items.clear()

        self._reconcile_open_orders()
        self._reconcile_portfolio()
        self._reconcile_recent_trades()

        print(f"[recovery] Reconciliation complete. "
              f"Critical items: {len(self._critical_items)}")
        for item in self._critical_items:
            print(f"  ⚠ {item}")

    def _reconcile_open_orders(self) -> None:
        """Compare local PLACED/PARTIAL orders with remote open orders."""
        local_orders = self._db.query_unfinished_orders()
        if not local_orders:
            print("[recovery] open-orders recon: no local unfinished orders")
            return

        try:
            remote_orders = trading.get_open_orders(self._client) if self._client else []
        except Exception as exc:
            print(f"[recovery] open-orders recon: get_open_orders failed: {exc}")
            for lo in local_orders:
                self._db.insert_reconciliation(
                    self._session_id, "OPEN_ORDERS", lo.token_id,
                    local_state={"intent_id": lo.intent_id, "status": lo.status},
                    remote_state=None,
                    mismatch_type="UNKNOWN",
                    resolution="PENDING",
                    notes=f"get_open_orders failed: {exc}",
                )
            return

        remote_ids = {str(o.get("id", "")) for o in remote_orders if isinstance(o, dict)}

        for lo in local_orders:
            if lo.status in (OrderStatus.PENDING.value, OrderStatus.TIMEOUT_UNCONFIRMED.value):
                # These orders may not have an order_id yet (PENDING) or their
                # status is uncertain (TIMEOUT_UNCONFIRMED). Check if they
                # appear in remote open orders.
                if lo.order_id and lo.order_id in remote_ids:
                    # Found on exchange -> update to PLACED
                    self._db.update_order_status(lo.intent_id, OrderStatus.PLACED.value)
                    self._db.insert_reconciliation(
                        self._session_id, "OPEN_ORDERS", lo.token_id,
                        local_state={"intent_id": lo.intent_id, "status": lo.status},
                        remote_state={"order_id": lo.order_id, "found": True},
                        mismatch_type="STATUS_DIFF",
                        resolution="AUTO_FIXED",
                        notes=f"{lo.status} -> PLACED (found in remote open orders)",
                    )
                    print(f"[recovery]   {lo.intent_id[:8]} {lo.side} {lo.status} -> PLACED (remote found)")
                elif lo.order_id:
                    # Has order_id but not in remote open orders -> check trades
                    self._confirm_order_from_trades(lo)
                else:
                    # PENDING without order_id: API was never called or crashed
                    # before getting a response. Cancel it (safe: no exchange order exists).
                    self._db.update_order_status(
                        lo.intent_id, OrderStatus.CANCELED.value,
                        notes="PENDING without order_id, canceled by reconciliation"
                    )
                    self._db.insert_reconciliation(
                        self._session_id, "OPEN_ORDERS", lo.token_id,
                        local_state={"intent_id": lo.intent_id, "status": lo.status},
                        remote_state=None,
                        mismatch_type="MISSING_REMOTE",
                        resolution="AUTO_FIXED",
                        notes="PENDING without order_id -> CANCELED",
                    )
                    print(f"[recovery]   {lo.intent_id[:8]} PENDING -> CANCELED (no order_id)")
            elif lo.order_id and lo.order_id not in remote_ids:
                # PLACED/PARTIAL but not in remote -> may have been filled or canceled
                self._confirm_order_from_trades(lo)

    def _confirm_order_from_trades(self, order) -> None:
        """Check recent trades to determine if an order was filled or canceled."""
        try:
            from py_clob_client_v2.clob_types import TradeParams
            params = TradeParams(asset_id=order.token_id)
            raw_trades = self._client.get_trades(params=params) if self._client else []
        except Exception as exc:
            print(f"[recovery]   {order.intent_id[:8]} could not confirm from trades: {exc}")
            self._critical_items.append(
                f"Order {order.intent_id[:8]} ({order.side} {order.token_id[:16]}...) "
                f"status={order.status} not in remote open orders and trades query failed"
            )
            self._db.insert_reconciliation(
                self._session_id, "TRADES", order.token_id,
                local_state={"intent_id": order.intent_id, "status": order.status},
                remote_state=None,
                mismatch_type="UNKNOWN",
                resolution="PENDING",
                notes=f"trades query failed: {exc}",
            )
            return

        trades = [trading._order_to_dict(t) for t in raw_trades] if raw_trades else []
        matching_fills = []
        for tr in trades:
            trade_order_ids = _extract_trade_order_ids(tr)
            if order.order_id and order.order_id in trade_order_ids:
                matching_fills.append(tr)

        if matching_fills:
            # Order was (at least partially) filled
            total_filled = sum(
                float(tr.get("size") or tr.get("matched_amount") or 0) for tr in matching_fills
            )
            is_full = total_filled >= order.size - 0.05
            new_status = OrderStatus.FILLED.value if is_full else OrderStatus.PARTIAL.value
            self._db.update_order_filled(
                order.intent_id, total_filled, total_filled, is_full
            )
            # Insert fill records
            for tr in matching_fills:
                tid = str(tr.get("id") or tr.get("trade_id") or "")
                if tid and not self._db.is_trade_seen(tid):
                    import json
                    self._db.insert_fill(
                        _make_fill_record(tr, order)
                    )
                    self._db.mark_trade_seen(tid)
            self._db.insert_reconciliation(
                self._session_id, "TRADES", order.token_id,
                local_state={"intent_id": order.intent_id, "status": order.status},
                remote_state={"fills_found": len(matching_fills), "total_filled": total_filled},
                mismatch_type="STATUS_DIFF",
                resolution="AUTO_FIXED",
                notes=f"{order.status} -> {new_status} (found {len(matching_fills)} fills)",
            )
            print(f"[recovery]   {order.intent_id[:8]} {order.status} -> {new_status} "
                  f"(found {len(matching_fills)} fills, total={total_filled})")
        else:
            # Not in open orders and no fills found -> canceled
            self._db.update_order_status(
                order.intent_id, OrderStatus.CANCELED.value,
                notes="Not in remote open orders, no fills found -> CANCELED"
            )
            self._db.insert_reconciliation(
                self._session_id, "TRADES", order.token_id,
                local_state={"intent_id": order.intent_id, "status": order.status},
                remote_state={"fills_found": 0},
                mismatch_type="MISSING_REMOTE",
                resolution="AUTO_FIXED",
                notes=f"{order.status} -> CANCELED (remote cancel detected)",
            )
            print(f"[recovery]   {order.intent_id[:8]} {order.status} -> CANCELED (remote cancel)")
            if order.side == "BUY":
                # BUY 被取消 → 阻止重启后重挂（v4 补强）
                self._db.set_entry_attempted(order.token_id, True)
                print(f"[recovery]   {order.intent_id[:8]} BUY canceled -> entry_attempted=True (block re-entry)")
            elif order.side == "SELL":
                # SELL was canceled but position may still exist -> needs re-protection
                self._critical_items.append(
                    f"SELL order {order.intent_id[:8]} for token {order.token_id[:16]}... "
                    f"was canceled remotely. Position needs re-protection in main loop."
                )

    def _reconcile_portfolio(self) -> None:
        """Compare local positions with Polymarket portfolio."""
        funder = self._config.funder
        if not funder:
            print("[recovery] portfolio recon: no FUNDER configured, skipping")
            return

        try:
            response = httpx.get(
                "https://data-api.polymarket.com/positions",
                params={"user": funder, "limit": 200, "sizeThreshold": 0},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            print(f"[recovery] portfolio recon: API failed: {exc}")
            # 写审计记录 + 追加 CRITICAL（v4: 补审计 trail）
            self._db.insert_reconciliation(
                self._session_id, "PORTFOLIO", None,
                local_state=None,
                remote_state="API_UNAVAILABLE",
                mismatch_type="API_UNAVAILABLE",
                resolution="PENDING",
                notes=str(exc),
            )
            self._critical_items.append(
                "Portfolio API unavailable — cannot verify positions"
            )
            return

        items = data if isinstance(data, list) else []
        remote_positions: dict[str, tuple[float, float]] = {}
        for item in items:
            tid = str(
                item.get("asset") or item.get("assetId") or item.get("tokenId") or ""
            )
            if tid:
                size = float(item.get("size") or 0)
                avg = float(item.get("avgPrice") or 0)
                remote_positions[tid] = (size, avg)

        local_positions = self._db.get_all_positions()
        for lp in local_positions:
            remote = remote_positions.get(lp.token_id)
            if remote is None:
                # Local has position but remote doesn't -> position may have been settled
                self._db.insert_reconciliation(
                    self._session_id, "PORTFOLIO", lp.token_id,
                    local_state={"size": lp.size, "avg": lp.avg_price},
                    remote_state=None,
                    mismatch_type="MISSING_REMOTE",
                    resolution="PENDING",
                    notes="Local position not found in remote portfolio (may be settled)",
                )
                self._critical_items.append(
                    f"Local position {lp.token_id[:16]}... (size={lp.size}) "
                    f"not found in remote portfolio"
                )
            else:
                remote_size = remote[0]
                size_diff = abs(remote_size - lp.size)
                if size_diff > max(0.1, lp.size * 0.1):
                    # Difference > 10% -> potential manual trade
                    self._db.insert_reconciliation(
                        self._session_id, "PORTFOLIO", lp.token_id,
                        local_state={"size": lp.size, "avg": lp.avg_price},
                        remote_state={"size": remote_size, "avg": remote[1]},
                        mismatch_type="SIZE_DIFF",
                        resolution="PENDING",
                        notes=f"Size diff {size_diff:.4f} > 10% threshold",
                    )
                    self._critical_items.append(
                        f"Position {lp.token_id[:16]}... size mismatch: "
                        f"local={lp.size} remote={remote_size} (diff={size_diff:.4f})"
                    )

        print(f"[recovery] portfolio recon: checked {len(local_positions)} local "
              f"vs {len(remote_positions)} remote positions")

    def _reconcile_recent_trades(self) -> None:
        """Check recent trades for any fills not yet recorded locally."""
        try:
            from py_clob_client_v2.clob_types import TradeParams
            # Get trades for all tokens with local positions or unfinished orders
            local_tokens = set()
            for p in self._db.get_all_positions():
                local_tokens.add(p.token_id)
            for o in self._db.query_unfinished_orders():
                local_tokens.add(o.token_id)

            if not local_tokens:
                print("[recovery] trades recon: no local tokens to check")
                return

            new_fills = 0
            for token_id in local_tokens:
                params = TradeParams(asset_id=token_id)
                raw_trades = self._client.get_trades(params=params) if self._client else []
                trades = [trading._order_to_dict(t) for t in raw_trades] if raw_trades else []
                for tr in trades:
                    tid = str(tr.get("id") or tr.get("trade_id") or "")
                    if not tid or self._db.is_trade_seen(tid):
                        continue
                    # New trade found that we haven't seen
                    trade_order_ids = _extract_trade_order_ids(tr)
                    matching_orders = self._db.query_orders_by_order_ids(trade_order_ids)
                    if matching_orders:
                        order = matching_orders[0]
                        self._db.insert_fill(_make_fill_record(tr, order))
                        self._db.mark_trade_seen(tid)
                        new_fills += 1
                        self._db.insert_reconciliation(
                            self._session_id, "TRADES", token_id,
                            local_state={"seen": False},
                            remote_state={"trade_id": tid},
                            mismatch_type="MISSING_LOCAL",
                            resolution="AUTO_FIXED",
                            notes=f"Fill recorded for order {order.intent_id[:8]}",
                        )
                    else:
                        # Trade doesn't match any local order -> mark as seen to avoid rechecking
                        self._db.mark_trade_seen(tid)

            print(f"[recovery] trades recon: found {new_fills} unrecorded fill(s)")
        except Exception as exc:
            print(f"[recovery] trades recon: failed: {exc}")

    # =================================================================== #
    # Handle unfinished orders
    # =================================================================== #
    def handle_unfinished_orders(self) -> None:
        """Post-reconciliation cleanup of unfinished orders."""
        print("\n[recovery] === HANDLING UNFINISHED ORDERS ===")

        # 1. Check BUY FILLED without paired SELL
        unpaired_buys = self._db.query_buy_filled_without_sell()
        for buy in unpaired_buys:
            t = self._config.trading
            if t.exit_price is not None:
                print(f"[recovery]   BUY FILLED {buy.intent_id[:8]} has no paired SELL; "
                      f"will be picked up by _ensure_exit_order in main loop")
                self._db.insert_reconciliation(
                    self._session_id, "UNPAIRED", buy.token_id,
                    local_state={"buy_intent": buy.intent_id, "size": buy.filled_size},
                    remote_state=None,
                    mismatch_type="MISSING_LOCAL",
                    resolution="PENDING",
                    notes="BUY FILLED without SELL; main loop will place protective SELL",
                )
            else:
                self._critical_items.append(
                    f"BUY FILLED {buy.intent_id[:8]} (token {buy.token_id[:16]}...) "
                    f"has no paired SELL and EXIT_PRICE is not configured"
                )
                self._db.insert_reconciliation(
                    self._session_id, "UNPAIRED", buy.token_id,
                    local_state={"buy_intent": buy.intent_id, "size": buy.filled_size},
                    remote_state=None,
                    mismatch_type="MISSING_LOCAL",
                    resolution="PENDING",
                    notes="CRITICAL: BUY FILLED, no SELL, EXIT_PRICE missing",
                )

        # 2. Check remaining unfinished orders
        remaining = self._db.query_unfinished_orders()
        if remaining:
            print(f"[recovery]   {len(remaining)} unfinished order(s) will be monitored in main loop")
        else:
            print("[recovery]   No unfinished orders remaining")

    # =================================================================== #
    # CRITICAL checkpoint
    # =================================================================== #
    def check_critical_and_wait(self) -> bool:
        """If CRITICAL items exist, pause for user confirmation.

        Returns True if OK to proceed, False if user chose to exit.
        """
        if not self._critical_items:
            return True

        print(f"\n[recovery] ⚠ {len(self._critical_items)} CRITICAL item(s) detected:")
        for i, item in enumerate(self._critical_items, 1):
            print(f"  {i}. {item}")

        print("\n[recovery] Options:")
        print("  y  -> proceed to main loop (CRITICAL items logged but not blocking)")
        print("  n  -> exit program (DB state preserved, can RESUME again later)")

        try:
            choice = input("[recovery] Proceed? (y/n): ").strip().lower()
        except (EOFError, OSError):
            choice = "y"

        if choice in ("n", "no"):
            print("[recovery] User chose to exit. DB state preserved.")
            self.finish_session("PARTIAL", 0)
            return False

        print("[recovery] Proceeding to main loop with CRITICAL items logged.")
        return True


# --------------------------------------------------------------------------- #
# Helpers (shared with runtime.py)
# --------------------------------------------------------------------------- #
def _extract_trade_order_ids(trade: dict[str, Any]) -> set[str]:
    """Extract all order_ids from a CLOB v2 Trade object."""
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


def _make_fill_record(trade: dict[str, Any], order) -> Any:
    """Create a FillRecord from a trade dict and matching OrderRecord."""
    from persistence import FillRecord
    import json

    tid = str(trade.get("id") or trade.get("trade_id") or "")
    token_id = str(
        trade.get("asset_id") or trade.get("token_id") or trade.get("market") or ""
    )
    side = str(trade.get("side", "")).upper()
    size = float(trade.get("size") or trade.get("matched_amount") or 0)
    price = float(trade.get("price") or 0)

    return FillRecord(
        trade_id=tid,
        token_id=token_id,
        side=side,
        size=size,
        price=price,
        order_id=order.order_id,
        intent_id=order.intent_id,
        raw_trade=json.dumps(trade, default=str) if trade else None,
    )
