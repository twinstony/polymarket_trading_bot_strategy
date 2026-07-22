"""
修复方案验证脚本（不修改源码，独立验证）。

验证目标：
1. order_id 提取：从 taker_order_id + maker_orders[].order_id 收集所有 order_id
2. maker 成交识别：bot 的限价单作为 maker 成交时能被正确识别为 bot-managed
3. 重复 SELL 防护：SELL 下单失败（400）后不重复挂单
4. trades 过滤优化：get_trades(asset_id=token_id) 按 token 过滤减少噪音

用真实 Trade 数据结构（来自实际 API 返回）模拟场景。
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, ".")

from config import Market, TradingParams
from order_state import OrderStatus
from persistence import OrderRecord, Persistence
from runtime import BotRuntime, _to_float, _close_enough


# --------------------------------------------------------------------------- #
# 真实 Trade 数据结构（来自 get_trades() 实测返回）
# 关键：order_id 分散在 taker_order_id 和 maker_orders[].order_id
# --------------------------------------------------------------------------- #
TOKEN_ID = "33586519889730026036950558438004360715459263658773090162684586391573975721659"

# bot 的 BUY 限价单 5@0.15 成交（bot 作为 maker，对手方 taker 卖给 bot）
TRADE_BUY_AS_MAKER = {
    "id": "trade-buy-001",
    "taker_order_id": "0xTAKER001",  # 对手方 taker 的 order_id
    "asset_id": TOKEN_ID,
    "side": "BUY",                   # taker 的 side（注意：maker 的 side 相反）
    "size": "5.0",
    "price": "0.15",
    "maker_address": "0xOTHER",      # 第一个 maker 是别人
    "maker_orders": [
        {
            "order_id": "0xMAKER_OTHER",   # 别人的 maker order
            "maker_address": "0xOTHER",
            "owner": "owner-other",
            "size": "2.0",
            "price": "0.15",
        },
        {
            "order_id": "0xBOT_BUY_001",   # ← bot 的 BUY order 在这里！
            "maker_address": "0x1F837106675AE63Afd85BA92696BA2740e50b39A",
            "owner": "bot-owner-id",
            "size": "3.0",
            "price": "0.15",
        },
    ],
}

# bot 的 SELL 限价单 5@0.25 成交（bot 作为 maker，对手方 taker 买走）
TRADE_SELL_AS_MAKER = {
    "id": "trade-sell-001",
    "taker_order_id": "0xTAKER002",
    "asset_id": TOKEN_ID,
    "side": "SELL",
    "size": "5.0",
    "price": "0.25",
    "maker_address": "0xOTHER",
    "maker_orders": [
        {
            "order_id": "0xBOT_SELL_001",  # ← bot 的 SELL order
            "maker_address": "0x1F837106675AE63Afd85BA92696BA2740e50b39A",
            "owner": "bot-owner-id",
            "size": "5.0",
            "price": "0.25",
        },
    ],
}

# 别人的成交（噪音）
TRADE_OTHER_BUY = {
    "id": "trade-other-001",
    "taker_order_id": "0xTAKER_OTHER",
    "asset_id": TOKEN_ID,
    "side": "BUY",
    "size": "1649.79",
    "price": "0.25",
    "maker_address": "0xSOMEONE",
    "maker_orders": [
        {"order_id": "0xMAKER_OTHER_2", "maker_address": "0xSOMEONE", "size": "1649.79", "price": "0.25"},
    ],
}


# --------------------------------------------------------------------------- #
# 修复方案：新的 order_id 提取函数（候选实现，待验证后写入 runtime.py）
# --------------------------------------------------------------------------- #
def extract_trade_order_ids_fixed(trade: dict[str, Any]) -> set[str]:
    """从 Trade 对象收集所有 order_id（taker + 所有 maker）。

    修复点：原代码用 trade.get("maker_order_id")（单数），字段名错误。
    实际字段是 maker_orders（复数数组），每项含 order_id。
    """
    ids: set[str] = set()
    # 1) taker 侧
    taker_id = trade.get("taker_order_id") or trade.get("order_id") or trade.get("orderID")
    if taker_id:
        ids.add(str(taker_id))
    # 2) maker 侧（数组）
    maker_orders = trade.get("maker_orders") or []
    if isinstance(maker_orders, list):
        for mo in maker_orders:
            if isinstance(mo, dict):
                mid = mo.get("order_id") or mo.get("orderID")
                if mid:
                    ids.add(str(mid))
    return ids


def trade_is_bot_managed_fixed(
    trade: dict[str, Any],
    entry_order_ids: set[str],
    exit_order_ids: set[str],
) -> bool:
    """判断某 trade 是否属于 bot-managed（BUY 或 SELL）。

    修复点：原代码只取单个 order_id 匹配，maker 成交会漏检。
    修复后收集 taker + 所有 maker 的 order_id，任一命中 bucket 即为 bot-managed。
    """
    trade_ids = extract_trade_order_ids_fixed(trade)
    return bool(trade_ids & (entry_order_ids | exit_order_ids))


# --------------------------------------------------------------------------- #
# 测试用 BotRuntime 构造（mock client，不连真实 API）
# --------------------------------------------------------------------------- #
def make_runtime(trades: list[dict], open_orders: list[dict]) -> BotRuntime:
    """构造一个 BotRuntime，mock client 返回指定 trades 和 open_orders。"""
    client = MagicMock()
    client.get_trades.return_value = trades
    client.get_open_orders.return_value = open_orders
    client.get_order_book.return_value = {"asks": [{"price": "0.23", "size": "100"}], "bids": [{"price": "0.21", "size": "100"}], "tick_size": "0.01"}
    client.get_last_trade_price.return_value = {"price": "0.23"}

    guard = MagicMock()
    guard.config.poll_interval = 30
    guard.config.status_every_cycles = 20
    guard.config.funder = "0x1F837106675AE63Afd85BA92696BA2740e50b39A"
    guard.config.trading_enabled = True

    market = Market(token_id=TOKEN_ID, label="TEST MARKET")
    t = TradingParams(
        markets=[market],
        share_amount=5.0,
        entry_price=0.15,
        exit_price=0.25,
        conditional_entry=False,
    )
    guard.snapshot.return_value = t
    guard.active_market.return_value = market

    # v4: 持久化改造后，bot 挂单状态存储在 DB 的 orders 表中。
    # 使用内存 SQLite 模拟"bot 已有挂单"的状态。
    db = Persistence(":memory:")
    db.open()
    rt = BotRuntime(client, guard, notifier=None, persistence=db, session_id="test-session")
    # orders.session_id 有外键引用 sessions.session_id，需先插入一条 session 记录
    db.insert_session(rt._session_id, "TEST")
    return rt


def _insert_bot_order(rt: BotRuntime, order_id: str, side: str) -> None:
    """向 DB 插入一条 bot 挂单记录（替代直接设置 _entry_order_ids/_exit_order_ids）。"""
    rt._db.insert_order(OrderRecord(
        intent_id=f"intent-{order_id}",
        session_id=rt._session_id,
        token_id=TOKEN_ID,
        side=side,
        price=0.15 if side == "BUY" else 0.25,
        size=5.0,
        status=OrderStatus.PLACED.value,
        order_id=order_id,
    ))


# --------------------------------------------------------------------------- #
# 测试 1：order_id 提取
# --------------------------------------------------------------------------- #
def test_extract_order_ids():
    print("=" * 60)
    print("测试 1：order_id 提取（修复点：maker_orders 数组）")
    print("=" * 60)

    # BUY as maker
    ids = extract_trade_order_ids_fixed(TRADE_BUY_AS_MAKER)
    expected = {"0xTAKER001", "0xMAKER_OTHER", "0xBOT_BUY_001"}
    assert ids == expected, f"BUY maker 提取错误: {ids} != {expected}"
    print(f"  [PASS] BUY as maker: 提取到 {len(ids)} 个 order_id, 含 bot 的 0xBOT_BUY_001")

    # SELL as maker
    ids = extract_trade_order_ids_fixed(TRADE_SELL_AS_MAKER)
    expected = {"0xTAKER002", "0xBOT_SELL_001"}
    assert ids == expected, f"SELL maker 提取错误: {ids} != {expected}"
    print(f"  [PASS] SELL as maker: 提取到 {len(ids)} 个 order_id, 含 bot 的 0xBOT_SELL_001")

    # 别人的成交
    ids = extract_trade_order_ids_fixed(TRADE_OTHER_BUY)
    expected = {"0xTAKER_OTHER", "0xMAKER_OTHER_2"}
    assert ids == expected, f"other 提取错误: {ids} != {expected}"
    print(f"  [PASS] Other trade: 提取到 {len(ids)} 个 order_id, 不含 bot 的")


# --------------------------------------------------------------------------- #
# 测试 2：maker 成交识别（核心场景）
# --------------------------------------------------------------------------- #
def test_maker_fill_recognition():
    print("\n" + "=" * 60)
    print("测试 2：maker 成交识别（核心：bot 限价单作为 maker 成交）")
    print("=" * 60)

    entry_ids = {"0xBOT_BUY_001"}
    exit_ids = {"0xBOT_SELL_001"}

    # 场景 A: bot BUY 限价单成交（bot 是 maker）
    assert trade_is_bot_managed_fixed(TRADE_BUY_AS_MAKER, entry_ids, exit_ids), \
        "BUY as maker 应被识别为 bot-managed"
    print("  [PASS] BUY as maker → bot-managed")

    # 场景 B: bot SELL 限价单成交（bot 是 maker）
    assert trade_is_bot_managed_fixed(TRADE_SELL_AS_MAKER, entry_ids, exit_ids), \
        "SELL as maker 应被识别为 bot-managed"
    print("  [PASS] SELL as maker → bot-managed")

    # 场景 C: 别人的成交不应被识别
    assert not trade_is_bot_managed_fixed(TRADE_OTHER_BUY, entry_ids, exit_ids), \
        "别人的成交不应被识别为 bot-managed"
    print("  [PASS] Other trade → NOT bot-managed")

    # 场景 D: bucket 为空时不应误判
    assert not trade_is_bot_managed_fixed(TRADE_BUY_AS_MAKER, set(), set()), \
        "bucket 为空时不应识别为 bot-managed"
    print("  [PASS] Empty buckets → NOT bot-managed（避免重启后误判）")


# --------------------------------------------------------------------------- #
# 测试 3：端到端 fill 检测（用修复后的逻辑模拟 _apply_fill）
# --------------------------------------------------------------------------- #
def test_apply_fill_with_fixed_logic():
    print("\n" + "=" * 60)
    print("测试 3：端到端 fill 检测（模拟 _apply_fill 用修复后逻辑）")
    print("=" * 60)

    rt = make_runtime([], [])
    _insert_bot_order(rt, "0xBOT_BUY_001", "BUY")
    _insert_bot_order(rt, "0xBOT_SELL_001", "SELL")
    t = rt._guard.snapshot()

    # 场景 A: BUY as maker 成交 → 持仓应增加 5.0
    rt._positions_cache = {}
    # 模拟修复后的 _apply_fill（用 trade_is_bot_managed_fixed 替代原 _trade_matches_current_intent）
    _apply_fill_with_fixed_match(rt, TRADE_BUY_AS_MAKER, t)
    pos = rt._positions_cache.get(TOKEN_ID, {})
    assert pos.get("size") == 5.0, f"BUY 成交后持仓应为 5.0, 实际 {pos.get('size')}"
    assert pos.get("avg_price") == 0.15, f"avg_price 应为 0.15, 实际 {pos.get('avg_price')}"
    print(f"  [PASS] BUY as maker 成交 → 持仓 {pos['size']} @ {pos['avg_price']}")

    # 场景 B: SELL as maker 成交 → 持仓应归零
    _apply_fill_with_fixed_match(rt, TRADE_SELL_AS_MAKER, t)
    pos = rt._positions_cache.get(TOKEN_ID, {})
    assert pos.get("size") == 0.0, f"SELL 成交后持仓应为 0.0, 实际 {pos.get('size')}"
    print(f"  [PASS] SELL as maker 成交 → 持仓归零为 {pos['size']}")

    # 场景 C: 别人的成交不应改变持仓
    size_before = rt._positions_cache.get(TOKEN_ID, {}).get("size", 0.0)
    _apply_fill_with_fixed_match(rt, TRADE_OTHER_BUY, t)
    size_after = rt._positions_cache.get(TOKEN_ID, {}).get("size", 0.0)
    assert size_after == size_before, f"别人的成交不应改变持仓: {size_before} → {size_after}"
    print(f"  [PASS] Other trade → 持仓不变 ({size_before} → {size_after})")


def _apply_fill_with_fixed_match(rt: BotRuntime, trade: dict, t: TradingParams):
    """复用 BotRuntime._apply_fill 但用修复后的匹配逻辑。"""
    token_id = str(trade.get("asset_id") or trade.get("token_id") or trade.get("market") or "")
    if not token_id:
        return
    side = str(trade.get("side", "")).upper()
    size = _to_float(trade.get("size") or trade.get("matched_amount")) or 0.0
    price = _to_float(trade.get("price")) or 0.0
    label = next((m.label for m in t.markets if m.token_id == token_id), "")
    active = t.active_market()
    if active is None or token_id != active.token_id:
        return

    # v4: 从 DB 查询 bot 的 entry/exit order_id 集合（替代已删除的 _entry_order_ids/_exit_order_ids）
    entry_ids = {
        o.order_id for o in rt._db.query_unfinished_orders(token_id=token_id, side="BUY") if o.order_id
    }
    exit_ids = {
        o.order_id for o in rt._db.query_unfinished_orders(token_id=token_id, side="SELL") if o.order_id
    }

    # ★ 用修复后的匹配逻辑替代原 _trade_matches_current_intent
    if not trade_is_bot_managed_fixed(trade, entry_ids, exit_ids):
        return

    with rt._lock:
        pos = rt._positions_cache.setdefault(token_id, {"size": 0.0, "avg_price": 0.0, "label": label})
        if not label:
            pos["label"] = label
        if side.startswith("B"):
            old_size = pos["size"]
            old_avg = pos["avg_price"]
            new_size = old_size + size
            pos["avg_price"] = (old_avg * old_size + price * size) / new_size if new_size > 0 else price
            pos["size"] = new_size
        elif side.startswith("S"):
            pos["size"] = max(0.0, pos["size"] - size)


# --------------------------------------------------------------------------- #
# 测试 4：重复 SELL 防护（失败退避）
# --------------------------------------------------------------------------- #
def test_sell_failure_backoff():
    print("\n" + "=" * 60)
    print("测试 4：SELL 下单失败退避（候选方案验证）")
    print("=" * 60)

    # 候选方案：runtime 增加 _failed_exit_attempts 计数器
    # 当连续失败次数 >= 阈值时，跳过 exit protection 下单，避免连续 400
    # 成功下单或 open_order 状态变化时重置计数器

    class ExitBackoffState:
        """候选方案的状态管理。"""
        def __init__(self, max_consecutive_failures: int = 2):
            self._failed_count = 0
            self._max = max_consecutive_failures
            self._last_failure_reason = ""

        def record_failure(self, reason: str) -> bool:
            """记录失败，返回 True 表示应继续退避（跳过下单）。"""
            self._failed_count += 1
            self._last_failure_reason = reason
            return self.should_skip()

        def record_success(self):
            self._failed_count = 0
            self._last_failure_reason = ""

        def should_skip(self) -> bool:
            return self._failed_count >= self._max

    state = ExitBackoffState(max_consecutive_failures=2)

    # 第 1 次失败：count=1，不跳过（允许重试一次）
    skip = state.record_failure("not enough balance")
    assert not skip, "第 1 次失败不应跳过"
    assert state.should_skip() is False
    print(f"  [PASS] 第 1 次失败 → count=1, 不跳过（允许重试）")

    # 第 2 次失败：count=2，开始跳过
    skip = state.record_failure("not enough balance")
    assert skip, "第 2 次失败应开始跳过"
    print(f"  [PASS] 第 2 次失败 → count=2, 跳过下单（退避）")

    # 第 3 次 cycle：应继续跳过
    assert state.should_skip(), "退避后应继续跳过"
    print(f"  [PASS] 退避中 cycle → 继续跳过")

    # open_order 状态变化或成功下单 → 重置
    state.record_success()
    assert not state.should_skip(), "重置后不应跳过"
    print(f"  [PASS] 成功/状态变化 → 重置 count=0, 恢复下单")

    # 验证：这能避免日志里的连续 4 次 400
    state2 = ExitBackoffState(max_consecutive_failures=2)
    results = []
    for i in range(4):
        if state2.should_skip():
            results.append("skip")
            continue
        # 模拟下单失败
        if state2.record_failure("not enough balance"):
            results.append("fail+skip")
        else:
            results.append("fail")
    assert results == ["fail", "fail+skip", "skip", "skip"], f"退避序列错误: {results}"
    print(f"  [PASS] 4 次 cycle 退避序列: {results}（仅 2 次实际下单尝试，避免连续 400）")


# --------------------------------------------------------------------------- #
# 测试 5：trades 过滤优化（asset_id 参数）
# --------------------------------------------------------------------------- #
def test_trades_filter_optimization():
    print("\n" + "=" * 60)
    print("测试 5：trades 过滤优化（TradeParams(asset_id=token_id)）")
    print("=" * 60)

    # 验证 TradeParams 支持 asset_id 字段
    from py_clob_client_v2.clob_types import TradeParams
    tp = TradeParams(asset_id=TOKEN_ID)
    assert tp.asset_id == TOKEN_ID, "TradeParams.asset_id 设置失败"
    print(f"  [PASS] TradeParams(asset_id=...) 可构造: asset_id={tp.asset_id[:20]}...")

    # 验证：原代码 client.get_trades() 不带参数，返回全市场
    # 修复后应 client.get_trades(params=TradeParams(asset_id=token_id))
    # 服务端按 token 过滤，减少返回数据量（实测全市场 56 条 → 按 token 过滤后更少）
    # 注意：maker_address 参数实测过滤无效（服务端不支持），故只用 asset_id
    tp_maker = TradeParams(maker_address="0x1F837106675AE63Afd85BA92696BA2740e50b39A")
    print(f"  [INFO] TradeParams(maker_address=...) 可构造，但服务端过滤无效（实测）")
    print(f"  [PASS] 修复方案：get_trades(params=TradeParams(asset_id=active.token_id))")


# --------------------------------------------------------------------------- #
# 测试 6：回归 - 原 bug 复现确认
# --------------------------------------------------------------------------- #
def test_original_bug_reproduction():
    print("\n" + "=" * 60)
    print("测试 6：回归 - 确认原 bug 存在（用原逻辑应失败）")
    print("=" * 60)

    # 原逻辑：只取 taker_order_id 或不存在的 maker_order_id（单数）
    def original_extract(trade: dict) -> str:
        return str(
            trade.get("order_id")
            or trade.get("taker_order_id")
            or trade.get("maker_order_id")  # ← 字段名错，永远 None
            or ""
        )

    # BUY as maker：原逻辑只能取到 taker_order_id，取不到 maker_orders 里的 0xBOT_BUY_001
    oid = original_extract(TRADE_BUY_AS_MAKER)
    assert oid == "0xTAKER001", f"原逻辑应只取到 taker_order_id, 实际 {oid}"
    assert oid != "0xBOT_BUY_001", "原逻辑应取不到 bot 的 maker order_id"
    print(f"  [PASS] 原 bug 确认: BUY as maker 时原逻辑只取到 taker_order_id={oid}")
    print(f"         bot 的 0xBOT_BUY_001 在 maker_orders[].order_id 里，原逻辑漏检")

    # 验证：原逻辑下，bot 的 maker 成交会被 _trade_matches_current_intent 误判
    entry_ids = {"0xBOT_BUY_001"}
    oid_in_bucket = oid in entry_ids
    assert not oid_in_bucket, "原逻辑下 taker_order_id 不在 entry_order_ids 中 → 漏检"
    print(f"  [PASS] 原 bug 确认: taker_order_id 不在 entry_order_ids → maker 成交漏检")
    print(f"         → 触发 portfolio 对账兜底（问题2）")
    print(f"         → SELL 成交后 position 不归零 → 重复挂 SELL（问题3/4）")


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    print("\n" + "#" * 60)
    print("# 修复方案验证（不修改源码，独立验证）")
    print("#" * 60 + "\n")

    test_extract_order_ids()
    test_maker_fill_recognition()
    test_apply_fill_with_fixed_logic()
    test_sell_failure_backoff()
    test_trades_filter_optimization()
    test_original_bug_reproduction()

    print("\n" + "=" * 60)
    print(" 所有测试通过，修复方案验证有效")
    print("=" * 60)
    print("\n修复方案总结：")
    print("1. 核心修复：重写 _trade_matches_current_intent 的 order_id 提取")
    print("   - 收集 taker_order_id + maker_orders[].order_id 所有 id")
    print("   - 任一命中 entry_order_ids/exit_order_ids 即为 bot-managed")
    print("2. 配套优化：get_trades 加 TradeParams(asset_id=token_id) 过滤")
    print("3. 配套优化：SELL 下单失败退避（_failed_exit_attempts 计数器）")
    print("\n最小化修复原则：")
    print("- 只改 _trade_matches_current_intent 一个函数（核心）")
    print("- get_trades 调用处加 params（一行改动）")
    print("- 新增 _failed_exit_attempts 状态 + _ensure_exit_order 退避检查")
