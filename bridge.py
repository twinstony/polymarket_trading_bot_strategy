"""
FastAPI Bridge for Polymarket Trading Bot.

Local-only HTTP control plane for the Chrome side-panel extension. Runs in
the same asyncio event loop as the bot, holds a ``RuntimeManager`` singleton
reference, and exposes 16 REST endpoints (7 GET + 5 POST write + 4 POST global).

Constraints (对齐项目硬约束):
- localhost only (bridge_host 默认 127.0.0.1, no auth)
- Write endpoints (下单/撤单) acquire ``manager._order_lock`` before operating
- POST /api/order validates trading_enabled / exit_price / entry_price / share_amount
- bridge holds no business state — all data read live from RuntimeManager
- API exceptions propagate (converted to HTTPException at the bridge boundary)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

import market_setup
import trading
from config import Config, StrategyConfig
from runtime import RuntimeManager, StrategyRuntime

PORTFOLIO_POSITIONS_URL = "https://data-api.polymarket.com/positions"
PORTFOLIO_VALUE_URL = "https://data-api.polymarket.com/value"


# --------------------------------------------------------------------------- #
# Pydantic request models
# --------------------------------------------------------------------------- #
class OrderRequest(BaseModel):
    token_id: str
    label: str = ""
    entry_price: float
    exit_price: float
    share_amount: float
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    enabled: bool = True


class CancelRequest(BaseModel):
    order_id: str | None = None
    token_id: str | None = None
    side: str | None = None


class CloseRequest(BaseModel):
    token_id: str
    mode: str  # "market" | "limit"


class TokenRequest(BaseModel):
    token_id: str


class PreviewRequest(BaseModel):
    action: str  # "stop_all" | "close_all" | "delete_all"


# --------------------------------------------------------------------------- #
# Error helper — raises HTTPException with {error, message} detail
# --------------------------------------------------------------------------- #
def _bridge_error(code: str, message: str, status_code: int) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={"error": code, "message": message},
    )


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _close_enough(actual: float | None, expected: float | None, *, min_abs: float, rel: float) -> bool:
    if actual is None or expected is None:
        return False
    return abs(actual - expected) <= max(min_abs, abs(expected) * rel)


def _find_runtime(manager: RuntimeManager, token_id: str) -> StrategyRuntime | None:
    """Locate the StrategyRuntime managing ``token_id``."""
    for rt in manager._runtimes:
        if rt._cfg.token_id == token_id:
            return rt
    return None


def _has_matching_buy_order(
    orders: list[dict[str, Any]], token_id: str, price: float, size: float
) -> bool:
    """Return True if a matching BUY order already rests on the book."""
    for o in orders:
        o_token = str(o.get("asset_id") or o.get("token_id") or o.get("market") or "")
        o_side = str(o.get("side", "")).upper()
        if o_token != token_id or o_side != "BUY":
            continue
        o_price = _to_float(o.get("price"))
        o_size = _to_float(o.get("original_size") or o.get("size") or o.get("remaining_size"))
        if _close_enough(o_price, price, min_abs=0.005, rel=0.02) and _close_enough(
            o_size, size, min_abs=0.05, rel=0.02
        ):
            return True
    return False


def _extract_order_id(resp: Any) -> str | None:
    if not isinstance(resp, dict):
        return None
    oid = str(resp.get("orderID") or resp.get("order_id") or "")
    return oid or None


async def _fetch_portfolio_marks(funder: str) -> dict[str, float]:
    """Fetch current mark prices from Polymarket data-api. Returns {token_id: price}."""
    if not funder:
        return {}
    try:
        resp = await asyncio.to_thread(
            httpx.get,
            PORTFOLIO_POSITIONS_URL,
            params={"user": funder, "limit": 200, "sizeThreshold": 0},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {}
    marks: dict[str, float] = {}
    if not isinstance(data, list):
        return marks
    for item in data:
        token_id = str(
            item.get("asset") or item.get("assetId") or item.get("tokenId") or ""
        )
        if not token_id:
            continue
        price = item.get("curPrice") or item.get("currentPrice") or item.get("price")
        if price is not None:
            try:
                marks[token_id] = float(price)
            except (TypeError, ValueError):
                pass
    return marks


async def _fetch_usdc_balance(funder: str) -> float | None:
    """Fetch available USDC cash balance from Polymarket data-api value endpoint.

    The Polymarket CLOB V2 SDK lacks ``get_balances``, so the public value API
    is used instead. It returns ``{"value", "cash", ...}`` where ``cash`` is the
    spendable USDC balance for the funder address.

    Returns ``None`` when the balance cannot be determined (empty funder,
    transport error, or unexpected payload). Callers must treat ``None`` as
    "skip balance check" rather than "zero balance" to avoid false positives.
    """
    if not funder:
        return None
    try:
        resp = await asyncio.to_thread(
            httpx.get,
            PORTFOLIO_VALUE_URL,
            params={"user": funder},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    cash = data.get("cash")
    if cash is None:
        # Fallback field names used by some value-API payload variants.
        cash = data.get("usdc") or data.get("balance")
    try:
        return float(cash) if cash is not None else None
    except (TypeError, ValueError):
        return None


def _collect_token_order_ids(
    orders: list[dict[str, Any]], token_id: str, side: str | None = None
) -> list[str]:
    """Collect order IDs from ``orders`` matching ``token_id`` (and optional ``side``)."""
    ids: list[str] = []
    for o in orders:
        o_token = str(o.get("asset_id") or o.get("token_id") or o.get("market") or "")
        if o_token != token_id:
            continue
        if side is not None and str(o.get("side", "")).upper() != side.upper():
            continue
        oid = str(o.get("id") or o.get("order_id") or o.get("orderID") or "")
        if oid:
            ids.append(oid)
    return ids


def _build_affected(snapshots: list[dict[str, Any]], *, only_holding: bool = False) -> list[dict[str, Any]]:
    """Build the affected_positions list from snapshots."""
    out: list[dict[str, Any]] = []
    for s in snapshots:
        if only_holding and s.get("position_size", 0) <= 0:
            continue
        out.append(
            {
                "label": s["label"],
                "token_id": s["token_id"],
                "state": s["state"],
                "size": s["position_size"],
                "avg_price": s["avg_price"],
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def create_bridge_app(manager: RuntimeManager, config: Config, client) -> FastAPI:
    """Create the FastAPI application wired to the bot's RuntimeManager.

    The app holds only references (manager / config / client) — no business
    state. All data is read live from the RuntimeManager on each request.
    """
    app = FastAPI(title="Polymarket Bridge", docs_url="/docs", redoc_url=None)

    # ===================================================================== #
    # GET — read endpoints (7)
    # ===================================================================== #

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        start = time.perf_counter()
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "status": "ok",
            "bot_running": True,
            "strategies_count": len(manager._runtimes),
            "latency_ms": latency_ms,
            "funder": config.funder,
        }

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return {
            "trading_enabled": config.trading_enabled,
            "poll_interval": config.poll_interval,
            "conditional_entry": config.conditional_entry,
            "default_entry_price": config.default_entry_price,
            "default_exit_price": config.default_exit_price,
            "default_share_amount": config.default_share_amount,
            "default_take_profit_pct": config.default_take_profit_pct,
            "default_stop_loss_pct": config.default_stop_loss_pct,
        }

    @app.get("/api/url/parse")
    async def parse_url(
        url: str | None = Query(None),
        slug: str | None = Query(None),
    ) -> dict[str, Any]:
        if not url and not slug:
            _bridge_error("INVALID_REQUEST", "Either ?url= or ?slug= is required", 400)

        if url:
            actual_slug = market_setup.extract_slug(url)
        else:
            actual_slug = slug or ""

        if not actual_slug:
            _bridge_error("INVALID_REQUEST", f"Cannot parse slug from input", 400)

        try:
            markets = await asyncio.to_thread(market_setup.fetch_markets, actual_slug)
        except market_setup.MarketFetchError as exc:
            _bridge_error(
                "MARKET_FETCH_ERROR",
                f"Polymarket API unreachable: {exc}",
                502,
            )
        if not markets:
            _bridge_error("MARKET_NOT_FOUND", f"No markets found for slug: {actual_slug}", 404)

        event_title = markets[0].get("event_title", actual_slug) if markets else actual_slug

        # Attach live book snapshots for each token — 并发查询所有 token 的盘口，
        # 把 N 次串行 HTTP 调用压成 1 次并发，解析耗时从 3-15s 降到 ~500ms。
        all_token_ids: list[str] = []
        token_to_market_idx: dict[str, int] = {}
        for mi, m in enumerate(markets):
            for tid in m.get("clob_token_ids", []):
                if tid and tid not in token_to_market_idx:
                    token_to_market_idx[tid] = mi
                    all_token_ids.append(tid)
        book_results = await asyncio.gather(
            *[asyncio.to_thread(market_setup.fetch_token_price, tid) for tid in all_token_ids],
            return_exceptions=True,
        )
        # 按 market 归集盘口快照
        for m in markets:
            m["books"] = []
        for tid, res in zip(all_token_ids, book_results):
            mi = token_to_market_idx.get(tid)
            if mi is None:  # pragma: no cover
                continue
            if isinstance(res, Exception):
                m = markets[mi]
                m["books"].append({"token_id": tid, "best_bid": None, "best_ask": None})
            else:
                bid, ask = res
                markets[mi]["books"].append({"token_id": tid, "best_bid": bid, "best_ask": ask})

        return {"slug": actual_slug, "event_title": event_title, "markets": markets}

    @app.get("/api/markets/{slug}")
    async def get_market(slug: str) -> dict[str, Any]:
        try:
            markets = await asyncio.to_thread(market_setup.fetch_markets, slug)
        except market_setup.MarketFetchError as exc:
            _bridge_error(
                "MARKET_FETCH_ERROR",
                f"Polymarket API unreachable: {exc}",
                502,
            )
        if not markets:
            _bridge_error("MARKET_NOT_FOUND", f"No markets found for slug: {slug}", 404)

        m = markets[0]
        tids = [t for t in m.get("clob_token_ids", []) if t]
        book_results = await asyncio.gather(
            *[asyncio.to_thread(market_setup.fetch_token_price, t) for t in tids],
            return_exceptions=True,
        )
        books: list[dict[str, Any]] = []
        for tid, res in zip(tids, book_results):
            if isinstance(res, Exception):
                books.append({"token_id": tid, "best_bid": None, "best_ask": None})
            else:
                bid, ask = res
                books.append({"token_id": tid, "best_bid": bid, "best_ask": ask})
        m["books"] = books
        return m

    @app.get("/api/positions")
    async def get_positions() -> dict[str, Any]:
        return {"positions": manager.status_snapshot()}

    @app.get("/api/orders")
    async def get_orders() -> dict[str, Any]:
        orders = await asyncio.to_thread(trading.get_open_orders, client)
        return {"orders": orders}

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        start = time.perf_counter()
        snapshots = manager.status_snapshot()
        marks = await _fetch_portfolio_marks(config.funder)

        position_count = 0
        total_cost = 0.0
        current_value = 0.0
        max_cycle = 0
        for s in snapshots:
            size = s.get("position_size", 0) or 0
            if size > 0:
                position_count += 1
                avg = s.get("avg_price", 0) or 0
                total_cost += size * avg
                mark = marks.get(s["token_id"], avg)
                current_value += size * mark
            cycle = s.get("cycle", 0) or 0
            if cycle > max_cycle:
                max_cycle = cycle

        pnl_usdc = current_value - total_cost
        pnl_pct = (pnl_usdc / total_cost * 100) if total_cost > 0 else 0.0
        latency_ms = int((time.perf_counter() - start) * 1000)

        return {
            "position_count": position_count,
            "total_cost_usdc": round(total_cost, 2),
            "current_value_usdc": round(current_value, 2),
            "pnl_usdc": round(pnl_usdc, 2),
            "pnl_pct": round(pnl_pct, 2),
            "connection": {"online": True, "latency_ms": latency_ms},
            "bot_running": True,
            "cycle": max_cycle,
        }

    # ===================================================================== #
    # POST — write endpoints (5)
    # ===================================================================== #

    @app.post("/api/order")
    async def place_order(req: OrderRequest) -> dict[str, Any]:
        # --- Security: trading_enabled gate (SubTask 3.6) ---
        if not config.trading_enabled:
            _bridge_error("TRADING_DISABLED", "trading_enabled is false", 409)

        # --- Validation: exit_price required (SubTask 3.6) ---
        if not req.exit_price or req.exit_price <= 0:
            _bridge_error("EXIT_PRICE_REQUIRED", "exit_price is required and must be > 0", 400)

        # --- Validation: entry_price ∈ (0, 1) ---
        if not (0 < req.entry_price < 1):
            _bridge_error("INVALID_PRICE", "entry_price must be in (0, 1)", 400)

        # --- Validation: share_amount > 0 ---
        if req.share_amount <= 0:
            _bridge_error("INVALID_PRICE", "share_amount must be > 0", 400)

        try:
            # --- Duplicate-order check (bot's _has_matching_open_order logic) ---
            open_orders = await asyncio.to_thread(trading.get_open_orders, client)
            if _has_matching_buy_order(open_orders, req.token_id, req.entry_price, req.share_amount):
                _bridge_error("DUPLICATE_ORDER", "A matching BUY order already rests on the book", 409)

            # --- Market status check: reject if market is closed or settling ---
            # Per project hard constraint: markets with ask=None and bid≈0.999
            # (pending settlement) must skip entry. ``fetch_token_price`` returns
            # the live CLOB book directly, no slug reverse-lookup required.
            best_bid, best_ask = await asyncio.to_thread(
                market_setup.fetch_token_price, req.token_id
            )
            if best_ask is None and best_bid is not None and best_bid >= 0.999:
                _bridge_error(
                    "MARKET_CLOSED",
                    f"Market is closed or settling (best_ask=None, best_bid={best_bid:.3f})",
                    409,
                )

            # --- Balance check: reject if USDC balance is insufficient ---
            # Per project hard constraint: balance validation before sending
            # orders to prevent "not enough balance" errors. The CLOB V2 SDK
            # lacks get_balances, so we query the Polymarket data-api value
            # endpoint for the funder's cash balance. If the lookup fails
            # (returns None), the check is skipped rather than falsely rejecting.
            required_usdc = req.share_amount * req.entry_price
            usdc_balance = await _fetch_usdc_balance(config.funder)
            if usdc_balance is not None and usdc_balance < required_usdc:
                _bridge_error(
                    "INSUFFICIENT_BALANCE",
                    f"Insufficient USDC balance: {usdc_balance:.2f} < {required_usdc:.2f} required",
                    409,
                )

            # --- Construct StrategyConfig + add to manager ---
            cfg = StrategyConfig(
                token_id=req.token_id,
                label=req.label or req.token_id,
                entry_price=req.entry_price,
                exit_price=req.exit_price,
                share_amount=req.share_amount,
                take_profit_pct=req.take_profit_pct,
                stop_loss_pct=req.stop_loss_pct,
                enabled=req.enabled,
            )
            rt = manager.add_strategy(cfg)

            # --- Place initial BUY under order_lock ---
            order_id: str | None = None
            async with manager._order_lock:
                resp = await asyncio.to_thread(
                    trading.enter_position,
                    client, req.token_id, "BUY", req.share_amount, req.entry_price,
                )

            if resp is not None:
                order_id = _extract_order_id(resp)
                if order_id:
                    rt._entry_order_ids.add(order_id)
                rt._entry_attempted = True
                rt._has_open_buy_order = True

            # --- Start the strategy monitoring loop ---
            task = asyncio.create_task(rt.run(), name=f"strategy:{rt.label}")
            manager._tasks.append(task)

            return {
                "position_id": req.token_id,
                "order_id": order_id,
                "state": rt._current_state(),
            }
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _bridge_error("BOT_ERROR", f"Failed to place order: {exc}", 500)

    @app.post("/api/order/cancel")
    async def cancel_order_endpoint(req: CancelRequest) -> dict[str, Any]:
        if not req.order_id and not req.token_id:
            _bridge_error("INVALID_REQUEST", "Either order_id or token_id is required", 400)

        try:
            if req.order_id:
                async with manager._order_lock:
                    ok = await asyncio.to_thread(trading.cancel_order, client, req.order_id)
                cancelled = [req.order_id] if ok else []
            else:
                side = req.side.upper() if req.side else None
                open_orders = await asyncio.to_thread(trading.get_open_orders, client)
                ids = _collect_token_order_ids(open_orders, req.token_id, side)
                async with manager._order_lock:
                    count = await asyncio.to_thread(
                        trading.cancel_open_orders_for_token, client, req.token_id, side
                    )
                cancelled = ids if count > 0 else []

            # 撤单后清掉对应 runtime 的 buy 状态标志，避免状态卡在 BUY_PENDING
            # （撤单后 _has_matching_open_order 异步返回 None 时 _check_entry 会提前 return，
            #  导致 _has_open_buy_order / _entry_attempted 不被清理）
            if req.token_id:
                rt = _find_runtime(manager, req.token_id)
                if rt is not None:
                    if not side or side == "BUY":
                        rt._has_open_buy_order = False
                        rt._entry_attempted = False
                    if not side or side == "SELL":
                        rt._has_open_sell_order = False

            return {"cancelled": cancelled}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _bridge_error("BOT_ERROR", f"Failed to cancel order: {exc}", 500)

    @app.post("/api/position/close")
    async def close_position(req: CloseRequest) -> dict[str, Any]:
        mode = req.mode.lower()
        if mode not in ("market", "limit"):
            _bridge_error("INVALID_REQUEST", "mode must be 'market' or 'limit'", 400)

        rt = _find_runtime(manager, req.token_id)
        if rt is None:
            _bridge_error("TOKEN_NOT_FOUND", f"No strategy found for token: {req.token_id}", 422)

        try:
            size = rt._position.size
            cancelled_orders: list[str] = []

            async with manager._order_lock:
                # 1. Cancel all SELL orders for this token.
                open_orders = await asyncio.to_thread(trading.get_open_orders, client)
                cancelled_orders = _collect_token_order_ids(open_orders, req.token_id, "SELL")
                await asyncio.to_thread(
                    trading.cancel_open_orders_for_token, client, req.token_id, "SELL"
                )
                rt._has_open_sell_order = False

                if size <= 0:
                    return {
                        "cancelled_orders": cancelled_orders,
                        "exit_order_id": None,
                        "exit_price": None,
                        "size": 0,
                        "state": rt._current_state(),
                    }

                # 2. Determine exit price.
                if mode == "market":
                    md = await asyncio.to_thread(trading.get_market_data, client, req.token_id)
                    exit_price = md.best_bid
                    if exit_price is None or exit_price <= 0:
                        _bridge_error(
                            "CLOB_UNAVAILABLE",
                            "No best_bid available for market sell",
                            503,
                        )
                else:  # limit
                    exit_price = rt._cfg.exit_price
                    if exit_price is None or exit_price <= 0:
                        _bridge_error(
                            "EXIT_PRICE_REQUIRED",
                            "Strategy exit_price not set; cannot place limit sell",
                            400,
                        )

                # 3. Place SELL order.
                resp = await asyncio.to_thread(
                    trading.exit_position,
                    client, req.token_id, "SELL", size, exit_price,
                )

            exit_order_id = _extract_order_id(resp)
            if exit_order_id:
                rt._exit_order_ids.add(exit_order_id)
                rt._has_open_sell_order = True

            return {
                "cancelled_orders": cancelled_orders,
                "exit_order_id": exit_order_id,
                "exit_price": exit_price,
                "size": size,
                "state": rt._current_state(),
            }
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _bridge_error("BOT_ERROR", f"Failed to close position: {exc}", 500)

    @app.post("/api/position/stop")
    async def stop_position(req: TokenRequest) -> dict[str, Any]:
        rt = _find_runtime(manager, req.token_id)
        if rt is None:
            _bridge_error("TOKEN_NOT_FOUND", f"No strategy found for token: {req.token_id}", 422)

        try:
            cancelled_orders: list[str] = []
            async with manager._order_lock:
                open_orders = await asyncio.to_thread(trading.get_open_orders, client)
                cancelled_orders = _collect_token_order_ids(open_orders, req.token_id)
                await asyncio.to_thread(
                    trading.cancel_open_orders_for_token, client, req.token_id
                )

            # 停止策略：禁用 enabled（供 snapshot 报告）+ 清掉 buy 状态标志
            # 避免卡在 BUY_PENDING + 停止运行循环（rt.stop 通过 _stop 事件退出 while 循环）
            rt._cfg.enabled = False
            rt._has_open_buy_order = False
            rt._has_open_sell_order = False
            rt._entry_attempted = False
            rt.stop()

            return {
                "cancelled_orders": cancelled_orders,
                "state": rt._current_state(),
            }
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _bridge_error("BOT_ERROR", f"Failed to stop position: {exc}", 500)

    @app.post("/api/position/delete")
    async def delete_position(req: TokenRequest) -> dict[str, Any]:
        rt = _find_runtime(manager, req.token_id)
        if rt is None:
            _bridge_error("TOKEN_NOT_FOUND", f"No strategy found for token: {req.token_id}", 422)

        try:
            rt.stop()
            manager._runtimes.remove(rt)
            return {"removed": True}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _bridge_error("BOT_ERROR", f"Failed to delete position: {exc}", 500)

    # ===================================================================== #
    # POST — global command endpoints (4)
    # ===================================================================== #

    @app.post("/api/global/preview")
    async def global_preview(req: PreviewRequest) -> dict[str, Any]:
        action = req.action
        if action not in ("stop_all", "close_all", "delete_all"):
            _bridge_error("INVALID_REQUEST", "action must be stop_all, close_all, or delete_all", 400)

        try:
            snapshots = manager.status_snapshot()

            if action == "stop_all":
                open_orders = await asyncio.to_thread(trading.get_open_orders, client)
                buy_count = sum(1 for o in open_orders if str(o.get("side", "")).upper() == "BUY")
                sell_count = sum(1 for o in open_orders if str(o.get("side", "")).upper() == "SELL")
                impact: dict[str, Any] = {
                    "stop_strategies": len(snapshots),
                    "cancel_buy_orders": buy_count,
                    "cancel_sell_orders": sell_count,
                }
                affected = _build_affected(snapshots)

            elif action == "close_all":
                holding = [s for s in snapshots if s.get("position_size", 0) > 0]
                marks = await _fetch_portfolio_marks(config.funder)
                current_mark = sum(
                    s["position_size"] * marks.get(s["token_id"], s["avg_price"]) for s in holding
                )
                open_orders = await asyncio.to_thread(trading.get_open_orders, client)
                sell_count = sum(1 for o in open_orders if str(o.get("side", "")).upper() == "SELL")
                impact = {
                    "cancel_sell_orders": sell_count,
                    "market_sell_positions": len(holding),
                    "estimated_recover_usdc": round(current_mark, 2),
                    "current_mark_usdc": round(current_mark, 2),
                    "slippage_risk_usdc": round(current_mark * 0.02, 2),
                }
                affected = _build_affected(snapshots, only_holding=True)

            else:  # delete_all
                impact = {
                    "remove_records": len(snapshots),
                    "note": "不撤销现存挂单，需先全部停止" if snapshots else "",
                }
                affected = _build_affected(snapshots)

            return {
                "action": action,
                "impact": impact,
                "affected_positions": affected,
            }
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _bridge_error("BOT_ERROR", f"Failed to preview: {exc}", 500)

    @app.post("/api/global/stop_all")
    async def global_stop_all() -> dict[str, Any]:
        try:
            snapshots = manager.status_snapshot()
            affected = _build_affected(snapshots)

            open_orders = await asyncio.to_thread(trading.get_open_orders, client)
            buy_count = sum(1 for o in open_orders if str(o.get("side", "")).upper() == "BUY")
            sell_count = sum(1 for o in open_orders if str(o.get("side", "")).upper() == "SELL")

            results: list[dict[str, Any]] = []
            async with manager._order_lock:
                for rt in list(manager._runtimes):
                    await asyncio.to_thread(
                        trading.cancel_open_orders_for_token, client, rt._cfg.token_id
                    )
                    rt._cfg.enabled = False
                    rt.stop()
                    results.append({"token_id": rt._cfg.token_id, "stopped": True})

            impact = {
                "stop_strategies": len(snapshots),
                "cancel_buy_orders": buy_count,
                "cancel_sell_orders": sell_count,
            }
            return {
                "action": "stop_all",
                "impact": impact,
                "affected_positions": affected,
                "executed": True,
                "results": results,
            }
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _bridge_error("BOT_ERROR", f"Failed to stop all: {exc}", 500)

    @app.post("/api/global/close_all")
    async def global_close_all() -> dict[str, Any]:
        try:
            snapshots = manager.status_snapshot()
            marks = await _fetch_portfolio_marks(config.funder)
            holding = [s for s in snapshots if s.get("position_size", 0) > 0]
            affected = _build_affected(snapshots, only_holding=True)
            current_mark = sum(
                s["position_size"] * marks.get(s["token_id"], s["avg_price"]) for s in holding
            )

            open_orders = await asyncio.to_thread(trading.get_open_orders, client)
            sell_count = sum(1 for o in open_orders if str(o.get("side", "")).upper() == "SELL")

            results: list[dict[str, Any]] = []
            async with manager._order_lock:
                for rt in list(manager._runtimes):
                    size = rt._position.size
                    if size <= 0:
                        continue
                    # Cancel existing SELL orders for this token.
                    await asyncio.to_thread(
                        trading.cancel_open_orders_for_token, client, rt._cfg.token_id, "SELL"
                    )
                    rt._has_open_sell_order = False
                    # Market sell at best_bid.
                    md = await asyncio.to_thread(trading.get_market_data, client, rt._cfg.token_id)
                    exit_price = md.best_bid
                    if exit_price is None or exit_price <= 0:
                        results.append(
                            {"token_id": rt._cfg.token_id, "error": "no best_bid available"}
                        )
                        continue
                    resp = await asyncio.to_thread(
                        trading.exit_position, client, rt._cfg.token_id, "SELL", size, exit_price
                    )
                    exit_order_id = _extract_order_id(resp)
                    if exit_order_id:
                        rt._exit_order_ids.add(exit_order_id)
                        rt._has_open_sell_order = True
                    results.append(
                        {
                            "token_id": rt._cfg.token_id,
                            "exit_price": exit_price,
                            "size": size,
                            "exit_order_id": exit_order_id,
                        }
                    )

            impact = {
                "cancel_sell_orders": sell_count,
                "market_sell_positions": len(holding),
                "estimated_recover_usdc": round(current_mark, 2),
                "current_mark_usdc": round(current_mark, 2),
                "slippage_risk_usdc": round(current_mark * 0.02, 2),
            }
            return {
                "action": "close_all",
                "impact": impact,
                "affected_positions": affected,
                "executed": True,
                "results": results,
            }
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _bridge_error("BOT_ERROR", f"Failed to close all: {exc}", 500)

    @app.post("/api/global/delete_all")
    async def global_delete_all() -> dict[str, Any]:
        try:
            snapshots = manager.status_snapshot()
            affected = _build_affected(snapshots)

            count = len(manager._runtimes)
            for rt in list(manager._runtimes):
                rt.stop()
            manager._runtimes.clear()

            impact = {"remove_records": count}
            return {
                "action": "delete_all",
                "impact": impact,
                "affected_positions": affected,
                "executed": True,
                "results": [{"removed": count}],
            }
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _bridge_error("BOT_ERROR", f"Failed to delete all: {exc}", 500)

    return app
