"""
启动时交互式市场配置（纯内存，不持久化）。

流程：
1. 输入 Polymarket 市场 URL 或 slug（输入新 URL 会清空已有策略列表）
2. 通过 Gamma API 解析该市场所有可交易 market 及其 outcomes（下注 token）
3. 命令行选择 market
4. 选择下注方向：
   - 单边：输入 outcome 编号，为该 outcome 配置参数
   - 双边：输入 b，为该 market 所有 outcomes 分别配置参数
5. 每个策略独立设置 entry_price / exit_price / share_amount / 止盈% / 止损%
6. 汇总确认
7. 可循环添加更多策略（同一市场选其他 outcome，或输入新 URL 重新开始）
8. 返回 list[StrategyConfig]，由 RuntimeManager 接管

策略列表纯内存，每次启动为空，不写入 config.json。
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

import httpx

from config import Config, StrategyConfig

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"


def run_interactive_setup(config: Config) -> list[StrategyConfig]:
    """运行交互式市场配置。返回策略列表（纯内存）。"""
    print("\n" + "=" * 72)
    print(" Polymarket 交互式市场配置（内存模式，不持久化）")
    print("=" * 72)

    strategies: list[StrategyConfig] = []

    while True:
        raw = input(
            "\n输入 Polymarket 市场 URL 或 slug"
            + ("" if strategies else "（必填）")
            + "，q 退出: "
        ).strip()
        if raw.lower() == "q":
            if strategies:
                print(f"[setup] 保留已配置的 {len(strategies)} 个策略")
                break
            print("[setup] 未配置任何策略，退出")
            return []
        if not raw:
            if strategies:
                break  # 已有策略，回车=完成
            print("[setup] 首次配置必须输入市场 URL 或 slug")
            continue

        # 输入新 URL → 清空已有策略列表
        if strategies:
            print(f"[setup] 输入新市场，清空已有 {len(strategies)} 个策略")
            strategies.clear()

        slug = extract_slug(raw)
        if not slug:
            print(f"[setup] 无法从输入解析 slug: {raw}")
            continue

        markets = fetch_markets(slug)
        if not markets:
            print(f"[setup] 未找到 slug={slug} 对应的市场，或市场均已关闭")
            continue

        market = select_market(markets)
        if market is None:
            continue

        # 为选中的 market 配置策略（单边 / 双边 / 循环追加）
        strategies.extend(configure_market_strategies(market, config))

        print(f"\n[setup] 当前共 {len(strategies)} 个策略")
        for i, s in enumerate(strategies, 1):
            print(f"  [{i}] {s.label}  entry={s.entry_price} exit={s.exit_price} size={s.share_amount}")

        more = input("\n继续添加策略？(y/N): ").strip().lower()
        if more not in ("y", "yes"):
            break

    return strategies


# --------------------------------------------------------------------------- #
# 为单个 market 配置策略（单边 / 双边）
# --------------------------------------------------------------------------- #
def configure_market_strategies(
    market: dict[str, Any], config: Config
) -> list[StrategyConfig]:
    """为选中的 market 配置一个或多个策略，返回 StrategyConfig 列表。"""
    outcomes = market["outcomes"]
    token_ids = market["clob_token_ids"]
    n = len(outcomes)

    print(f"\n市场: {market['question']}")
    print("下注选项（含实时盘口）:")
    prices: list[tuple[float | None, float | None]] = []
    for i, (name, tid) in enumerate(zip(outcomes, token_ids), 1):
        bid, ask = fetch_token_price(tid)
        prices.append((bid, ask))
        price_str = f"  买价={ask}" if ask is not None else ""
        price_str += f" 卖价={bid}" if bid is not None else ""
        if not price_str:
            price_str = "  (无盘口)"
        print(f"  [{i}] {name}  token=...{tid[-8:]}{price_str}")

    if n >= 2:
        print(f"  [b] 双边下注 — 为所有 {n} 个 outcomes 分别配置参数")

    result: list[StrategyConfig] = []
    while True:
        raw = input(
            f"\n选择下注方向编号 (1-{n})"
            + ("，b=双边" if n >= 2 else "")
            + ("，q=完成此市场" if result else "，q=取消")
            + ": "
        ).strip()

        if raw.lower() == "q":
            if result:
                return result
            return []

        if n >= 2 and raw.lower() == "b":
            # 双边下注：为每个 outcome 独立配置
            print(f"\n--- 双边下注：为 {n} 个 outcomes 分别配置 ---")
            for i, (name, tid) in enumerate(zip(outcomes, token_ids), 1):
                bid, ask = prices[i - 1]
                print(f"\n[{i}/{n}] {name}  (买价={ask} 卖价={bid})")
                sc = configure_single_strategy(market, name, tid, config)
                if sc is None:
                    print(f"[setup] 跳过 {name}")
                    continue
                result.append(sc)
            if result:
                return result
            print("[setup] 双边下注未配置任何策略")
            continue

        # 单边下注
        try:
            idx = int(raw)
            if not (1 <= idx <= n):
                print(f"[setup] 请输入 1-{n} 之间的数字")
                continue
        except ValueError:
            print("[setup] 无效输入")
            continue

        name = outcomes[idx - 1]
        tid = token_ids[idx - 1]
        bid, ask = prices[idx - 1]
        print(f"\n配置: {name}  (买价={ask} 卖价={bid})")
        sc = configure_single_strategy(market, name, tid, config)
        if sc is None:
            print("[setup] 已取消此策略")
            continue
        result.append(sc)

        more = input("为此市场继续添加策略？(y/N): ").strip().lower()
        if more not in ("y", "yes"):
            return result


def configure_single_strategy(
    market: dict[str, Any],
    outcome_name: str,
    token_id: str,
    config: Config,
) -> StrategyConfig | None:
    """为一个 outcome 配置完整策略参数，返回 StrategyConfig 或 None（取消）。"""
    label = build_label(market, outcome_name)

    entry = _ask_float("买入价 entry_price", config.default_entry_price)
    if entry is None:
        return None

    exit_p = _ask_float("卖出价 exit_price", config.default_exit_price)
    if exit_p is None:
        return None

    size = _ask_float("买入数量 share_amount", config.default_share_amount)
    if size is None:
        return None

    tp = _ask_optional_float("止盈百分比 take_profit_pct", config.default_take_profit_pct)
    if tp is _CANCELLED:
        return None

    sl = _ask_optional_float("止损百分比 stop_loss_pct", config.default_stop_loss_pct)
    if sl is _CANCELLED:
        return None

    sc = StrategyConfig(
        token_id=token_id,
        label=label,
        entry_price=entry,
        exit_price=exit_p,
        share_amount=size,
        take_profit_pct=tp,
        stop_loss_pct=sl,
    )

    if not confirm_strategy(sc):
        return None

    return sc


# --------------------------------------------------------------------------- #
# slug 解析
# --------------------------------------------------------------------------- #
def extract_slug(raw: str) -> str:
    """从完整 URL 或裸 slug 中提取 slug。"""
    raw = raw.strip()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urllib.parse.urlparse(raw)
        path = parsed.path.rstrip("/")
        if not path:
            return ""
        return path.rsplit("/", 1)[-1]
    return raw.strip("/").rsplit("/", 1)[-1]


# --------------------------------------------------------------------------- #
# Gamma API 查询
# --------------------------------------------------------------------------- #
def fetch_markets(slug: str) -> list[dict[str, Any]]:
    """查询 slug 对应的所有可交易 market。"""
    try:
        resp = httpx.get(GAMMA_EVENTS_URL, params={"slug": slug}, timeout=20)
        resp.raise_for_status()
        events = resp.json()
        if isinstance(events, list) and events:
            ev = events[0]
            ev_title = str(ev.get("title") or slug)
            raw_markets = ev.get("markets") or []
            if raw_markets:
                return [_normalize_market(m, ev_title) for m in raw_markets]
    except Exception as exc:  # noqa: BLE001
        print(f"[setup] events 查询失败: {exc}")

    try:
        resp = httpx.get(GAMMA_MARKETS_URL, params={"slug": slug}, timeout=20)
        resp.raise_for_status()
        ms = resp.json()
        if isinstance(ms, list) and ms:
            return [_normalize_market(m, str(ms[0].get("question") or slug)) for m in ms]
    except Exception as exc:  # noqa: BLE001
        print(f"[setup] markets 查询失败: {exc}")

    return []


def _normalize_market(m: dict[str, Any], event_title: str) -> dict[str, Any]:
    """把 Gamma 返回的 market 归一化为统一结构。"""
    outcomes = _parse_json_list_field(m.get("outcomes"))
    token_ids = _parse_json_list_field(m.get("clobTokenIds"))
    n = min(len(outcomes), len(token_ids))
    return {
        "question": str(m.get("question") or event_title),
        "event_title": event_title,
        "outcomes": outcomes[:n],
        "clob_token_ids": token_ids[:n],
        "tick_size": float(m.get("orderPriceMinTickSize") or 0.01),
        "closed": bool(m.get("closed") or not m.get("active")),
        "accepting": bool(m.get("acceptingOrders")),
        "end_date": str(m.get("endDate") or ""),
    }


def _parse_json_list_field(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(x) for x in data]
        except json.JSONDecodeError:
            return []
    return []


# --------------------------------------------------------------------------- #
# 实时盘口查询
# --------------------------------------------------------------------------- #
def fetch_token_price(token_id: str) -> tuple[float | None, float | None]:
    """返回 (best_bid, best_ask)，失败返回 (None, None)。"""
    try:
        resp = httpx.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=15)
        resp.raise_for_status()
        book = resp.json()
        asks = book.get("asks") or []
        bids = book.get("bids") or []
        best_ask = min((float(a["price"]) for a in asks if a.get("price")), default=None)
        best_bid = max((float(b["price"]) for b in bids if b.get("price")), default=None)
        return best_bid, best_ask
    except Exception:  # noqa: BLE001
        return None, None


# --------------------------------------------------------------------------- #
# 交互：选择 market
# --------------------------------------------------------------------------- #
def select_market(markets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """展示所有 market 供用户选择，返回选中的 market 或 None（取消）。"""
    print(f"\n找到 {len(markets)} 个市场:")
    for i, m in enumerate(markets, 1):
        status = "[已关闭]" if m["closed"] else ("[可交易]" if m["accepting"] else "[暂停]")
        print(f"  [{i}] {m['question']}  {status}")

    while True:
        raw = input(f"\n选择市场编号 (1-{len(markets)})，q 取消: ").strip()
        if raw.lower() == "q":
            return None
        try:
            idx = int(raw)
            if 1 <= idx <= len(markets):
                m = markets[idx - 1]
                if m["closed"]:
                    print("[setup] 该市场已关闭，请选择其他市场")
                    continue
                return m
            print(f"[setup] 请输入 1-{len(markets)} 之间的数字")
        except ValueError:
            print("[setup] 无效输入，请输入数字")


# --------------------------------------------------------------------------- #
# 交互：参数输入
# --------------------------------------------------------------------------- #
_SKIP = object()      # 回车跳过（保留默认值）
_CANCELLED = object()  # 用户按 q 取消


def _ask_float(label_zh: str, default: float) -> float | None:
    """问一个必填浮点数，回车=用默认值，q=取消返回 None。"""
    while True:
        raw = input(f"{label_zh}（默认 {default}，回车保留）: ").strip()
        if raw.lower() == "q":
            return None
        if raw == "":
            return default
        try:
            v = float(raw)
            if v <= 0:
                print("[setup] 必须大于 0")
                continue
            return v
        except ValueError:
            print("[setup] 无效数字，请重新输入")


def _ask_optional_float(label_zh: str, default: float | None) -> Any:
    """问一个可选浮点数。

    返回：
      - _SKIP：回车跳过（用默认值）
      - _CANCELLED：q 取消
      - None：n 清除
      - float：用户输入的值
    """
    cur_label = "未设置" if default is None else default
    while True:
        raw = input(
            f"{label_zh}（默认 {cur_label}，回车保留，n 清除，0.1=10%）: "
        ).strip()
        if raw.lower() == "q":
            return _CANCELLED
        if raw == "":
            return default  # 用默认值（可能是 None）
        if raw.lower() == "n":
            return None  # 清除
        try:
            return float(raw)
        except ValueError:
            print("[setup] 无效数字（回车保留，n 清除）")


def build_label(market: dict[str, Any], outcome_name: str) -> str:
    return f"{market['question']} [{outcome_name}]"


# --------------------------------------------------------------------------- #
# 确认
# --------------------------------------------------------------------------- #
def confirm_strategy(sc: StrategyConfig) -> bool:
    """展示单个策略摘要并由用户确认。"""
    notional = sc.entry_price * sc.share_amount
    tp_str = f"{sc.take_profit_pct*100:.1f}%" if sc.take_profit_pct is not None else "未设置"
    sl_str = f"{sc.stop_loss_pct*100:.1f}%" if sc.stop_loss_pct is not None else "未设置"

    print("\n" + "-" * 50)
    print(" 策略确认")
    print("-" * 50)
    print(f"  市场标签: {sc.label}")
    print(f"  Token ID: {sc.token_id}")
    print(f"  买入价:   {sc.entry_price}")
    print(f"  卖出价:   {sc.exit_price}")
    print(f"  买入数量: {sc.share_amount} 份  (约 {notional:.2f} USDC)")
    print(f"  止盈百分比: {tp_str}")
    print(f"  止损百分比: {sl_str}")
    print("-" * 50)

    raw = input("确认此策略？(Y/n): ").strip()
    return raw.lower() in ("", "y", "yes")
