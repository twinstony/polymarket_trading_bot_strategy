"""
Legacy runtime module — utility functions only.

The BotRuntime class has been replaced by StrategyRuntime (strategy_runtime.py)
+ RuntimeManager (runtime_manager.py). This module retains the shared utility
functions that are still referenced by tests and strategy_runtime.py.
"""

from __future__ import annotations

from typing import Any


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
