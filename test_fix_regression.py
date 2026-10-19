"""
回归测试（适配 asyncio multi-strategy 架构）。

验证从旧 BotRuntime 移植到新 StrategyRuntime 的核心逻辑：
1. _extract_trade_order_ids：maker 成交的 order_id 收集
2. _trade_matches_intent：maker 成交识别（heuristic fallback）
3. _apply_fill：BUY/SELL maker 成交正确更新持仓
4. _detect_fills：get_trades 用 TradeParams(asset_id) 过滤（async）
5. _ensure_exit_order：SELL 下单失败退避（async）
6. _ensure_exit_order：SELL 下单成功 → 重置失败计数（async）
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

sys.path.insert(0, ".")

from config import StrategyConfig
from runtime import StrategyRuntime, _extract_trade_order_ids
from py_clob_client_v2.clob_types import TradeParams

# --------------------------------------------------------------------------- #
# 真实 Trade 数据结构（来自 get_trades() 实测返回）
# --------------------------------------------------------------------------- #
TOKEN_ID = "33586519889730026036950558438004360715459263658773090162684586391573975721659"

TRADE_BUY_AS_MAKER = {
    "id": "trade-buy-001",
    "taker_order_id": "0xTAKER001",
    "asset_id": TOKEN_ID,
    "side": "BUY",
    "size": "5.0",
    "price": "0.15",
    "maker_address": "0xOTHER",
    "maker_orders": [
        {"order_id": "0xMAKER_OTHER", "maker_address": "0xOTHER", "size": "2.0", "price": "0.15"},
        {"order_id": "0xBOT_BUY_001", "maker_address": "0x1F837106675AE63Afd85BA92696BA2740e50b39A", "size": "3.0", "price": "0.15"},
    ],
}

TRADE_SELL_AS_MAKER = {
    "id": "trade-sell-001",
    "taker_order_id": "0xTAKER002",
    "asset_id": TOKEN_ID,
    "side": "SELL",
    "size": "5.0",
    "price": "0.25",
    "maker_address": "0xOTHER",
    "maker_orders": [
        {"order_id": "0xBOT_SELL_001", "maker_address": "0x1F837106675AE63Afd85BA92696BA2740e50b39A", "size": "5.0", "price": "0.25"},
    ],
}

TRADE_OTHER = {
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


def make_runtime(trades=None, open_orders=None) -> tuple[StrategyRuntime, MagicMock]:
    """构造 StrategyRuntime，mock client 和 config。"""
    client = MagicMock()
    client.get_trades.return_value = trades or []
    client.get_open_orders.return_value = open_orders or []
    client.get_order_book.return_value = {
        "asks": [{"price": "0.23", "size": "100"}],
        "bids": [{"price": "0.21", "size": "100"}],
        "tick_size": "0.01",
    }
    client.get_last_trade_price.return_value = {"price": "0.23"}

    config = MagicMock()
    config.poll_interval = 30
    config.status_every_cycles = 20
    config.funder = "0x1F837106675AE63Afd85BA92696BA2740e50b39A"
    config.trading_enabled = True
    config.conditional_entry = False

    sc = StrategyConfig(
        token_id=TOKEN_ID,
        label="TEST MARKET",
        entry_price=0.15,
        exit_price=0.25,
        share_amount=5.0,
    )

    # asyncio.Lock needs an event loop; create one for the test.
    loop = asyncio.new_event_loop()
    order_lock = asyncio.Lock()

    rt = StrategyRuntime(sc, client, notifier=None, order_lock=order_lock, config=config)
    return rt, client


# --------------------------------------------------------------------------- #
# 测试 1：_extract_trade_order_ids
# --------------------------------------------------------------------------- #
def test_extract_order_ids():
    print("=" * 60)
    print("测试 1：_extract_trade_order_ids")
    print("=" * 60)

    ids = _extract_trade_order_ids(TRADE_BUY_AS_MAKER)
    expected = {"0xTAKER001", "0xMAKER_OTHER", "0xBOT_BUY_001"}
    assert ids == expected, f"BUY maker 提取错误: {ids} != {expected}"
    print(f"  [PASS] BUY as maker: {ids}")

    ids = _extract_trade_order_ids(TRADE_SELL_AS_MAKER)
    expected = {"0xTAKER002", "0xBOT_SELL_001"}
    assert ids == expected, f"SELL maker 提取错误: {ids} != {expected}"
    print(f"  [PASS] SELL as maker: {ids}")

    ids = _extract_trade_order_ids(TRADE_OTHER)
    expected = {"0xTAKER_OTHER", "0xMAKER_OTHER_2"}
    assert ids == expected, f"other 提取错误: {ids} != {expected}"
    print(f"  [PASS] Other trade: {ids}")


# --------------------------------------------------------------------------- #
# 测试 2：_trade_matches_intent（heuristic fallback）
# --------------------------------------------------------------------------- #
def test_trade_matches_intent():
    print("\n" + "=" * 60)
    print("测试 2：_trade_matches_intent（heuristic fallback）")
    print("=" * 60)

    rt, _ = make_runtime()

    # BUY: price=0.15 <= entry+0.01, size=5.0 matches share_amount
    matched = rt._trade_matches_intent("BUY", 5.0, 0.15)
    assert matched, "BUY should match intent (price/size match)"
    print("  [PASS] BUY → matches intent")

    # SELL: no position yet → should not match
    matched = rt._trade_matches_intent("SELL", 5.0, 0.25)
    assert not matched, "SELL with no position should not match"
    print("  [PASS] SELL (no position) → does not match")

    # SELL with position → should match (price=0.25 matches exit_price)
    rt._position.size = 5.0
    matched = rt._trade_matches_intent("SELL", 5.0, 0.25)
    assert matched, "SELL with position should match"
    print("  [PASS] SELL (with position) → matches intent")

    # Other trade: size=1649.79 doesn't match share_amount=5.0
    matched = rt._trade_matches_intent("BUY", 1649.79, 0.25)
    assert not matched, "Large trade should not match intent"
    print("  [PASS] Other trade → does not match")


# --------------------------------------------------------------------------- #
# 测试 3：_apply_fill 端到端
# --------------------------------------------------------------------------- #
def test_apply_fill_e2e():
    print("\n" + "=" * 60)
    print("测试 3：_apply_fill 端到端")
    print("=" * 60)

    rt, _ = make_runtime()
    rt._entry_order_ids = {"0xBOT_BUY_001"}
    rt._exit_order_ids = {"0xBOT_SELL_001"}

    # BUY as maker → bot 的 maker_orders 中 0xBOT_BUY_001 成交 size=3.0
    rt._apply_fill(TRADE_BUY_AS_MAKER)
    assert rt._position.size == 3.0, f"BUY 后持仓应为 3.0 (maker 成交), 实际 {rt._position.size}"
    assert rt._position.avg_price == 0.15, f"avg_price 应为 0.15, 实际 {rt._position.avg_price}"
    print(f"  [PASS] BUY as maker → 持仓 {rt._position.size} @ {rt._position.avg_price}")

    # SELL as maker → position 归零
    rt._apply_fill(TRADE_SELL_AS_MAKER)
    assert rt._position.size == 0.0, f"SELL 后持仓应为 0.0, 实际 {rt._position.size}"
    assert rt._closed, "SELL 成交后策略应标记为 closed"
    print(f"  [PASS] SELL as maker → 持仓归零 {rt._position.size}, closed={rt._closed}")

    # 别人的成交 → 持仓不变
    rt._closed = False  # reset for test
    size_before = rt._position.size
    rt._apply_fill(TRADE_OTHER)
    assert rt._position.size == size_before, f"别人成交不应改变持仓: {size_before} → {rt._position.size}"
    print(f"  [PASS] Other trade → 持仓不变 ({size_before} → {rt._position.size})")


# --------------------------------------------------------------------------- #
# 测试 4：_detect_fills 用 TradeParams(asset_id) 过滤（async）
# --------------------------------------------------------------------------- #
def test_detect_fills_filter():
    print("\n" + "=" * 60)
    print("测试 4：_detect_fills 用 TradeParams(asset_id) 过滤（async）")
    print("=" * 60)

    rt, client = make_runtime(trades=[])
    asyncio.run(rt._detect_fills())

    assert client.get_trades.called, "get_trades 应被调用"
    call_kwargs = client.get_trades.call_args
    params_arg = call_kwargs.kwargs.get("params")
    assert params_arg is not None, "应传 params=TradeParams(asset_id=...)"
    assert isinstance(params_arg, TradeParams), f"params 类型应为 TradeParams, 实际 {type(params_arg)}"
    assert params_arg.asset_id == TOKEN_ID, f"asset_id 不匹配"
    print(f"  [PASS] get_trades(params=TradeParams(asset_id=...)) 已正确过滤")


# --------------------------------------------------------------------------- #
# 测试 5：SELL 下单失败退避（async）
# --------------------------------------------------------------------------- #
def test_exit_backoff_e2e():
    print("\n" + "=" * 60)
    print("测试 5：SELL 下单失败退避（async）")
    print("=" * 60)

    from strategy import PositionData
    rt, client = make_runtime(open_orders=[])
    rt._position = PositionData(token_id=TOKEN_ID, size=5.0, avg_price=0.15)

    import trading as trading_mod
    original_exit = trading_mod.exit_position
    call_count = {"n": 0}

    def mock_exit(*args, **kwargs):
        call_count["n"] += 1
        return None  # 模拟下单失败

    trading_mod.exit_position = mock_exit
    try:
        # 第 1 次失败
        asyncio.run(rt._ensure_exit_order())
        assert call_count["n"] == 1, f"第 1 次应实际下单, call_count={call_count['n']}"
        assert rt._failed_exit_attempts == 1, f"失败计数应为 1, 实际 {rt._failed_exit_attempts}"
        print(f"  [PASS] 第 1 次失败 → 实际下单, 失败计数=1")

        # 第 2 次失败
        asyncio.run(rt._ensure_exit_order())
        assert call_count["n"] == 2, f"第 2 次应实际下单, call_count={call_count['n']}"
        assert rt._failed_exit_attempts == 2, f"失败计数应为 2, 实际 {rt._failed_exit_attempts}"
        print(f"  [PASS] 第 2 次失败 → 实际下单, 失败计数=2")

        # 第 3 次：达阈值，应跳过下单（退避）
        asyncio.run(rt._ensure_exit_order())
        assert call_count["n"] == 2, f"第 3 次应跳过下单(退避), call_count={call_count['n']}"
        print(f"  [PASS] 第 3 次 → 退避跳过, 不下单 (call_count 仍为 {call_count['n']})")

        # 场景：有 SELL 挂单覆盖持仓 → 重置失败计数
        rt._failed_exit_attempts = 5
        client.get_open_orders.return_value = [{
            "asset_id": TOKEN_ID, "side": "SELL", "price": "0.25",
            "original_size": "5.0", "size_matched": "0.0",
        }]
        asyncio.run(rt._ensure_exit_order())
        assert rt._failed_exit_attempts == 0, f"有卖单覆盖时应重置计数, 实际 {rt._failed_exit_attempts}"
        print(f"  [PASS] 有 SELL 挂单覆盖 → 重置失败计数=0")

    finally:
        trading_mod.exit_position = original_exit


# --------------------------------------------------------------------------- #
# 测试 6：SELL 下单成功 → 重置失败计数（async）
# --------------------------------------------------------------------------- #
def test_exit_success_resets():
    print("\n" + "=" * 60)
    print("测试 6：SELL 下单成功 → 重置失败计数（async）")
    print("=" * 60)

    from strategy import PositionData
    rt, client = make_runtime(open_orders=[])
    rt._position = PositionData(token_id=TOKEN_ID, size=5.0, avg_price=0.15)

    import trading as trading_mod
    original_exit = trading_mod.exit_position

    def mock_exit_success(*args, **kwargs):
        return {"orderID": "0xNEW_SELL", "status": "live", "success": True}

    trading_mod.exit_position = mock_exit_success
    try:
        rt._failed_exit_attempts = 1  # 之前失败过 1 次
        asyncio.run(rt._ensure_exit_order())
        assert rt._failed_exit_attempts == 0, f"成功后应重置, 实际 {rt._failed_exit_attempts}"
        assert "0xNEW_SELL" in rt._exit_order_ids, "成功响应的 order_id 应记入 _exit_order_ids"
        print(f"  [PASS] SELL 下单成功 → 失败计数重置=0, order_id 记入 bucket")
    finally:
        trading_mod.exit_position = original_exit


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    print("\n" + "#" * 60)
    print("# 回归测试（asyncio StrategyRuntime 架构）")
    print("#" * 60 + "\n")

    test_extract_order_ids()
    test_trade_matches_intent()
    test_apply_fill_e2e()
    test_detect_fills_filter()
    test_exit_backoff_e2e()
    test_exit_success_resets()

    print("\n" + "=" * 60)
    print(" 所有测试通过")
    print("=" * 60)
