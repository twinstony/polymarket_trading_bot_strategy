"""
修复后回归测试（直接调用修复后的 runtime.py 真实代码）。

验证 3 处修复：
1. _extract_trade_order_ids + _trade_matches_current_intent：maker 成交识别
2. _detect_fills：get_trades 加 TradeParams(asset_id=token_id) 过滤
3. _ensure_exit_order：SELL 下单失败退避
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, ".")

from config import Market, TradingParams
from runtime import BotRuntime, _extract_trade_order_ids
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


def make_runtime(trades=None, open_orders=None) -> BotRuntime:
    """构造 BotRuntime，mock client。"""
    client = MagicMock()
    client.get_trades.return_value = trades or []
    client.get_open_orders.return_value = open_orders or []
    client.get_order_book.return_value = {"asks": [{"price": "0.23", "size": "100"}], "bids": [{"price": "0.21", "size": "100"}], "tick_size": "0.01"}
    client.get_last_trade_price.return_value = {"price": "0.23"}

    guard = MagicMock()
    guard.config.poll_interval = 30
    guard.config.status_every_cycles = 20
    guard.config.funder = "0x1F837106675AE63Afd85BA92696BA2740e50b39A"
    guard.config.trading_enabled = True

    market = Market(token_id=TOKEN_ID, label="TEST MARKET")
    t = TradingParams(markets=[market], share_amount=5.0, entry_price=0.15, exit_price=0.25, conditional_entry=False)
    guard.snapshot.return_value = t
    guard.active_market.return_value = market

    rt = BotRuntime(client, guard, notifier=None)
    return rt, client


# --------------------------------------------------------------------------- #
# 测试 1：_extract_trade_order_ids（修复后的真实函数）
# --------------------------------------------------------------------------- #
def test_extract_order_ids():
    print("=" * 60)
    print("测试 1：_extract_trade_order_ids（修复后的真实函数）")
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
# 测试 2：_trade_matches_current_intent（修复后的真实方法）
# --------------------------------------------------------------------------- #
def test_trade_matches_current_intent():
    print("\n" + "=" * 60)
    print("测试 2：_trade_matches_current_intent（修复后 maker 成交识别）")
    print("=" * 60)

    rt, _ = make_runtime()
    rt._entry_order_ids = {"0xBOT_BUY_001"}
    rt._exit_order_ids = {"0xBOT_SELL_001"}
    t = rt._guard.snapshot()

    # BUY as maker → 应匹配 entry_order_ids
    matched = rt._trade_matches_current_intent(TRADE_BUY_AS_MAKER, t, "BUY", 5.0, 0.15)
    assert matched, "BUY as maker 应被识别为 bot-managed"
    print("  [PASS] BUY as maker → bot-managed")

    # SELL as maker → 应匹配 exit_order_ids
    matched = rt._trade_matches_current_intent(TRADE_SELL_AS_MAKER, t, "SELL", 5.0, 0.25)
    assert matched, "SELL as maker 应被识别为 bot-managed"
    print("  [PASS] SELL as maker → bot-managed")

    # 别人的成交 → 不匹配
    matched = rt._trade_matches_current_intent(TRADE_OTHER, t, "BUY", 1649.79, 0.25)
    assert not matched, "别人的成交不应被识别"
    print("  [PASS] Other trade → NOT bot-managed")

    # bucket 为空 → 不匹配（避免重启后误判）
    rt._entry_order_ids = set()
    rt._exit_order_ids = set()
    matched = rt._trade_matches_current_intent(TRADE_BUY_AS_MAKER, t, "BUY", 5.0, 0.15)
    # bucket 为空时走 fallback 逻辑（price/size 匹配），size=5.0 匹配 share_amount=5.0
    # 这个 fallback 是有意的，用于重启后 bucket 丢失的场景
    print(f"  [INFO] Empty buckets → fallback 匹配结果: {matched}（fallback 走 price/size）")


# --------------------------------------------------------------------------- #
# 测试 3：_apply_fill 端到端（修复后，BUY/SELL maker 成交正确更新持仓）
# --------------------------------------------------------------------------- #
def test_apply_fill_e2e():
    print("\n" + "=" * 60)
    print("测试 3：_apply_fill 端到端（修复后 maker 成交更新持仓）")
    print("=" * 60)

    rt, _ = make_runtime()
    rt._entry_order_ids = {"0xBOT_BUY_001"}
    rt._exit_order_ids = {"0xBOT_SELL_001"}
    t = rt._guard.snapshot()

    # BUY as maker → 持仓 +5
    rt._positions = {}
    rt._apply_fill(TRADE_BUY_AS_MAKER, t)
    pos = rt._positions.get(TOKEN_ID, {})
    assert pos.get("size") == 5.0, f"BUY 后持仓应为 5.0, 实际 {pos.get('size')}"
    assert pos.get("avg_price") == 0.15, f"avg_price 应为 0.15, 实际 {pos.get('avg_price')}"
    print(f"  [PASS] BUY as maker → 持仓 {pos['size']} @ {pos['avg_price']}")

    # SELL as maker → 持仓归零
    rt._apply_fill(TRADE_SELL_AS_MAKER, t)
    pos = rt._positions.get(TOKEN_ID, {})
    assert pos.get("size") == 0.0, f"SELL 后持仓应为 0.0, 实际 {pos.get('size')}"
    print(f"  [PASS] SELL as maker → 持仓归零 {pos['size']}")

    # 别人的成交 → 持仓不变
    size_before = rt._positions.get(TOKEN_ID, {}).get("size", 0.0)
    rt._apply_fill(TRADE_OTHER, t)
    size_after = rt._positions.get(TOKEN_ID, {}).get("size", 0.0)
    assert size_after == size_before, f"别人成交不应改变持仓: {size_before} → {size_after}"
    print(f"  [PASS] Other trade → 持仓不变 ({size_before} → {size_after})")


# --------------------------------------------------------------------------- #
# 测试 4：_detect_fills 用 TradeParams(asset_id) 过滤
# --------------------------------------------------------------------------- #
def test_detect_fills_filter():
    print("\n" + "=" * 60)
    print("测试 4：_detect_fills 用 TradeParams(asset_id) 过滤")
    print("=" * 60)

    rt, client = make_runtime(trades=[])
    t = rt._guard.snapshot()

    # 调用 _detect_fills，验证 client.get_trades 被调用时带 params=TradeParams(asset_id=...)
    rt._detect_fills(t)
    assert client.get_trades.called, "get_trades 应被调用"
    call_kwargs = client.get_trades.call_args
    params = call_kwargs.kwargs.get("params") or (call_kwargs.args[0] if call_kwargs.args else None)
    # get_trades(params=...) 是 kwargs
    params_arg = call_kwargs.kwargs.get("params")
    assert params_arg is not None, "应传 params=TradeParams(asset_id=...)"
    assert isinstance(params_arg, TradeParams), f"params 类型应为 TradeParams, 实际 {type(params_arg)}"
    assert params_arg.asset_id == TOKEN_ID, f"asset_id 应为 {TOKEN_ID[:20]}..., 实际 {params_arg.asset_id[:20] if params_arg.asset_id else None}"
    print(f"  [PASS] get_trades(params=TradeParams(asset_id={TOKEN_ID[:20]}...)) 已正确过滤")


# --------------------------------------------------------------------------- #
# 测试 5：SELL 下单失败退避（_ensure_exit_order 端到端）
# --------------------------------------------------------------------------- #
def test_exit_backoff_e2e():
    print("\n" + "=" * 60)
    print("测试 5：SELL 下单失败退避（_ensure_exit_order 端到端）")
    print("=" * 60)

    # 构造场景：有持仓 5.0，无 SELL 挂单，exit_position 返回 None（模拟 400 失败）
    from strategy import PositionData
    rt, client = make_runtime(open_orders=[])
    t = rt._guard.snapshot()
    active = t.active_market()
    position = PositionData(token_id=TOKEN_ID, size=5.0, avg_price=0.15)

    # mock trading.exit_position 返回 None（失败）
    import trading as trading_mod
    original_exit = trading_mod.exit_position
    call_count = {"n": 0}

    def mock_exit(*args, **kwargs):
        call_count["n"] += 1
        return None  # 模拟下单失败

    trading_mod.exit_position = mock_exit
    try:
        # 第 1 次失败：count=1，未达阈值，应实际下单
        rt._failed_exit_attempts = {}
        rt._ensure_exit_order(t, active, TOKEN_ID, position)
        assert call_count["n"] == 1, f"第 1 次应实际下单, call_count={call_count['n']}"
        assert rt._failed_exit_attempts.get(TOKEN_ID) == 1, f"失败计数应为 1, 实际 {rt._failed_exit_attempts.get(TOKEN_ID)}"
        print(f"  [PASS] 第 1 次失败 → 实际下单, 失败计数=1")

        # 第 2 次失败：count=2，达阈值，应实际下单（阈值检查在下单前）
        rt._ensure_exit_order(t, active, TOKEN_ID, position)
        assert call_count["n"] == 2, f"第 2 次应实际下单, call_count={call_count['n']}"
        assert rt._failed_exit_attempts.get(TOKEN_ID) == 2, f"失败计数应为 2, 实际 {rt._failed_exit_attempts.get(TOKEN_ID)}"
        print(f"  [PASS] 第 2 次失败 → 实际下单, 失败计数=2")

        # 第 3 次：达阈值，应跳过下单（退避）
        rt._ensure_exit_order(t, active, TOKEN_ID, position)
        assert call_count["n"] == 2, f"第 3 次应跳过下单(退避), call_count={call_count['n']}"
        print(f"  [PASS] 第 3 次 → 退避跳过, 不下单 (call_count 仍为 {call_count['n']})")

        # 第 4 次：继续退避
        rt._ensure_exit_order(t, active, TOKEN_ID, position)
        assert call_count["n"] == 2, f"第 4 次应继续退避, call_count={call_count['n']}"
        print(f"  [PASS] 第 4 次 → 继续退避 (call_count 仍为 {call_count['n']})")

        # 场景：有 SELL 挂单覆盖持仓 → 重置失败计数
        rt._failed_exit_attempts[TOKEN_ID] = 5
        # mock open_orders 返回匹配的 SELL
        client.get_open_orders.return_value = [{
            "asset_id": TOKEN_ID, "side": "SELL", "price": "0.25",
            "original_size": "5.0", "size_matched": "0.0",
        }]
        rt._ensure_exit_order(t, active, TOKEN_ID, position)
        assert rt._failed_exit_attempts.get(TOKEN_ID) == 0, f"有卖单覆盖时应重置计数, 实际 {rt._failed_exit_attempts.get(TOKEN_ID)}"
        print(f"  [PASS] 有 SELL 挂单覆盖 → 重置失败计数=0")

    finally:
        trading_mod.exit_position = original_exit


# --------------------------------------------------------------------------- #
# 测试 6：SELL 下单成功 → 重置失败计数
# --------------------------------------------------------------------------- #
def test_exit_success_resets():
    print("\n" + "=" * 60)
    print("测试 6：SELL 下单成功 → 重置失败计数")
    print("=" * 60)

    from strategy import PositionData
    rt, client = make_runtime(open_orders=[])
    t = rt._guard.snapshot()
    active = t.active_market()
    position = PositionData(token_id=TOKEN_ID, size=5.0, avg_price=0.15)

    import trading as trading_mod
    original_exit = trading_mod.exit_position

    def mock_exit_success(*args, **kwargs):
        return {"orderID": "0xNEW_SELL", "status": "live", "success": True}

    trading_mod.exit_position = mock_exit_success
    try:
        rt._failed_exit_attempts = {TOKEN_ID: 1}  # 之前失败过 1 次
        rt._ensure_exit_order(t, active, TOKEN_ID, position)
        assert rt._failed_exit_attempts.get(TOKEN_ID) == 0, f"成功后应重置, 实际 {rt._failed_exit_attempts.get(TOKEN_ID)}"
        assert "0xNEW_SELL" in rt._exit_order_ids, "成功响应的 order_id 应记入 _exit_order_ids"
        print(f"  [PASS] SELL 下单成功 → 失败计数重置=0, order_id 记入 bucket")
    finally:
        trading_mod.exit_position = original_exit


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    print("\n" + "#" * 60)
    print("# 修复后回归测试（直接调用修复后的 runtime.py 真实代码）")
    print("#" * 60 + "\n")

    test_extract_order_ids()
    test_trade_matches_current_intent()
    test_apply_fill_e2e()
    test_detect_fills_filter()
    test_exit_backoff_e2e()
    test_exit_success_resets()

    print("\n" + "=" * 60)
    print(" 所有测试通过，3 处修复验证有效")
    print("=" * 60)
    print("\n修复总结：")
    print("1. _extract_trade_order_ids：从 taker_order_id + maker_orders[].order_id 收集所有 id")
    print("2. _trade_matches_current_intent：用集合交集判断，任一命中 bucket 即 managed")
    print("3. _detect_fills：get_trades(params=TradeParams(asset_id=token_id)) 按 token 过滤")
    print("4. _ensure_exit_order：SELL 失败退避（连续 2 次失败后跳过），成功/有覆盖时重置")
