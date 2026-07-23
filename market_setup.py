"""
启动时交互式市场配置（多策略版）。

流程：
1. 输入 Polymarket 市场 URL 或 slug（回车跳过使用当前配置）
2. 通过 Gamma API 解析该市场所有可交易 market 及其 outcomes（下注 token）
3. 命令行选择 market 与 outcome
4. 为该 outcome 设置 entry_price / exit_price / share_amount
5. 可循环添加更多 strategy（双边下注 = 添加两个 outcome 各为一个 strategy）
6. 汇总所有策略由用户确认后写入 config.json 并开始运行
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

import httpx

from config import Config, ConfigGuard, Strategy

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"

# 模块级 httpx client，配置 retries 缓解间歇性 SSL EOF（VPN/代理环境下常见）
_http_client = httpx.Client(
    http2=False,
    timeout=httpx.Timeout(30.0, connect=10.0),
    limits=httpx.Limits(
        max_keepalive_connections=5,
        max_connections=10,
        keepalive_expiry=5.0,
    ),
    transport=httpx.HTTPTransport(retries=3),
)


def run_interactive_setup(config: Config, guard: ConfigGuard) -> bool:
    """运行交互式市场配置。返回 True 表示可继续运行，False 表示应退出。"""
    print("\n" + "=" * 72)
    print(" Polymarket 交互式市场配置（多策略）")
    print("=" * 72)

    existing = guard.list_strategies()
    if existing:
        print(f"当前已配置 {len(existing)} 个策略:")
        for s in existing:
            print(
                f"  [{s.strategy_id}] {s.display()}  "
                f"entry={s.entry_price} exit={s.exit_price} size={s.share_amount} "
                f"enabled={s.enabled}"
            )

    raw = input(
        "\n输入 Polymarket 市场 URL 或 slug（回车跳过使用当前配置）: "
    ).strip()
    if not raw:
        print("[setup] 沿用现有配置启动")
        return True

    slug = extract_slug(raw)
    if not slug:
        print(f"[setup] 无法从输入解析 slug: {raw}")
        return False

    markets = fetch_markets(slug)
    if not markets:
        print(f"[setup] 未找到 slug={slug} 对应的市场，或市场均已关闭")
        return False

    # 收集所有要添加的 strategies
    new_strategies: list[Strategy] = []
    while True:
        market = select_market(markets)
        if market is None:
            if new_strategies:
                print("[setup] 已有策略待添加，继续确认流程")
                break
            print("[setup] 已取消")
            return False

        outcome = select_outcome(market)
        if outcome is None:
            print("[setup] 已取消该 outcome")
        else:
            token_id, outcome_name = outcome
            if token_id == "both":
                # 双边下注：为每个 outcome 分别设置参数
                m_outcomes = market["outcomes"]
                m_token_ids = market["clob_token_ids"]
                print(f"[setup] 双边下注模式：将为 {len(m_outcomes)} 个方向分别设置参数")
                for i, (tid, oname) in enumerate(zip(m_token_ids, m_outcomes), 1):
                    label = build_label(market, oname)
                    print(f"\n--- [{i}/{len(m_outcomes)}] {label} ---")
                    strat = input_strategy_params(tid, label, oname, config)
                    if strat is not None:
                        new_strategies.append(strat)
                        print(f"[setup] 已添加策略: {label}")
                    else:
                        print(f"[setup] 已跳过 {label}")
            else:
                label = build_label(market, outcome_name)
                strat = input_strategy_params(token_id, label, outcome_name, config)
                if strat is not None:
                    new_strategies.append(strat)
                    print(f"[setup] 已添加策略: {label}")

        # 问是否继续添加
        more = input("\n继续添加策略？(y/N): ").strip().lower()
        if more not in ("y", "yes"):
            break

    if not new_strategies:
        print("[setup] 未添加任何策略")
        return True

    if not confirm_strategies(new_strategies, config):
        print("[setup] 用户取消，退出")
        return False

    # 用户配置了新策略，清空旧策略列表（纯内存，不持久化）
    # 这样每次启动只跑用户本次配置的策略，不累积旧数据
    old_strategies = guard.list_strategies()
    for old in old_strategies:
        guard.remove_strategy(old.strategy_id)
    for strat in new_strategies:
        guard.add_strategy(strat)
    print(f"[setup] 已加载 {len(new_strategies)} 个策略（已清空旧策略）")
    return True


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
        resp = _http_client.get(GAMMA_EVENTS_URL, params={"slug": slug})
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
        resp = _http_client.get(GAMMA_MARKETS_URL, params={"slug": slug})
        resp.raise_for_status()
        ms = resp.json()
        if isinstance(ms, list) and ms:
            return [_normalize_market(m, str(ms[0].get("question") or slug)) for m in ms]
    except Exception as exc:  # noqa: BLE001
        print(f"[setup] markets 查询失败: {exc}")

    return []


def _normalize_market(m: dict[str, Any], event_title: str) -> dict[str, Any]:
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
    try:
        resp = _http_client.get(CLOB_BOOK_URL, params={"token_id": token_id})
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
# 交互：选择 outcome（单边）
# --------------------------------------------------------------------------- #
def select_outcome(market: dict[str, Any]) -> tuple[str, str] | None:
    """展示某 market 的所有 outcomes 供选择。

    返回值：
      - (token_id, outcome_name)：单边下注
      - ("both", "both")：双边下注（为每个 outcome 分别设置参数）
      - None：取消
    """
    print(f"\n市场: {market['question']}")
    print("下注选项（含实时盘口）:")

    outcomes = market["outcomes"]
    token_ids = market["clob_token_ids"]
    for i, (name, tid) in enumerate(zip(outcomes, token_ids), 1):
        bid, ask = fetch_token_price(tid)
        price_str = f"  买价={ask}" if ask is not None else ""
        price_str += f" 卖价={bid}" if bid is not None else ""
        if not price_str:
            price_str = "  (无盘口)"
        print(f"  [{i}] {name}  token=...{tid[-8:]}{price_str}")

    dual_hint = "，b=双边下注" if len(outcomes) >= 2 else ""
    while True:
        raw = input(f"\n选择下注方向编号 (1-{len(outcomes)}){dual_hint}，q 取消: ").strip()
        if raw.lower() == "q":
            return None
        if raw.lower() == "b" and len(outcomes) >= 2:
            return ("both", "both")
        try:
            idx = int(raw)
            if 1 <= idx <= len(outcomes):
                return token_ids[idx - 1], outcomes[idx - 1]
            print(f"[setup] 请输入 1-{len(outcomes)} 之间的数字")
        except ValueError:
            print("[setup] 无效输入，请输入数字或 b")


# --------------------------------------------------------------------------- #
# 交互：输入单个 strategy 参数
# --------------------------------------------------------------------------- #
def input_strategy_params(
    token_id: str,
    label: str,
    outcome_name: str,
    config: Config,
) -> Strategy | None:
    """为单个 strategy 询问 entry/exit/amount/止盈止损，返回 Strategy 或 None（取消）。"""
    t = config.trading
    print(f"\n--- 设置策略参数: {label} ---")

    entry = _ask_float(f"买入价 entry_price（当前 {t.entry_price}，回车沿用）: ", t.entry_price)
    if entry is None:
        return None
    exit_p = _ask_float(f"卖出价 exit_price（当前 {t.exit_price}，回车沿用）: ", t.exit_price)
    if exit_p is None:
        return None
    amount = _ask_float(f"买入数量 share_amount（当前 {t.share_amount}，回车沿用）: ", t.share_amount)
    if amount is None:
        return None
    tp = _ask_pct(
        f"止盈百分比 take_profit_pct（当前 {t.take_profit_pct}，回车沿用，off=关闭）: ",
        t.take_profit_pct,
    )
    sl = _ask_pct(
        f"止损百分比 stop_loss_pct（当前 {t.stop_loss_pct}，回车沿用，off=关闭）: ",
        t.stop_loss_pct,
    )

    return Strategy(
        token_id=token_id,
        label=label,
        outcome_name=outcome_name,
        entry_price=entry,
        exit_price=exit_p,
        share_amount=amount,
        enabled=True,
        take_profit_pct=tp,
        stop_loss_pct=sl,
    )


def _ask_float(prompt: str, default: float) -> float | None:
    """问一个必填浮点数，回车=沿用默认值，q=取消返回 None。"""
    while True:
        raw = input(prompt).strip()
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


def build_label(market: dict[str, Any], outcome_name: str) -> str:
    return f"{market['question']} [{outcome_name}]"


# --------------------------------------------------------------------------- #
# 百分比输入辅助（止盈止损等）
# --------------------------------------------------------------------------- #
def _ask_pct(prompt: str, default: float | None) -> float | None:
    """询问百分比，回车=沿用默认，off=返回 None（关闭），q=沿用默认。"""
    while True:
        raw = input(prompt).strip()
        if raw.lower() in ("", "q"):
            return default
        if raw.lower() == "off":
            return None
        try:
            v = float(raw)
            if v <= 0:
                print("[setup] 必须大于 0（或 off 关闭）")
                continue
            return v
        except ValueError:
            print("[setup] 无效数字，请重新输入（或 off 关闭）")


# --------------------------------------------------------------------------- #
# 确认
# --------------------------------------------------------------------------- #
def confirm_strategies(strategies: list[Strategy], config: Config) -> bool:
    """展示所有待添加策略并由用户确认。"""
    t = config.trading
    print("\n" + "=" * 60)
    print(f" 策略确认（共 {len(strategies)} 个）")
    print("=" * 60)
    for i, s in enumerate(strategies, 1):
        notional = s.entry_price * s.share_amount
        tp_str = s.take_profit_pct if s.take_profit_pct is not None else "关闭"
        sl_str = s.stop_loss_pct if s.stop_loss_pct is not None else "关闭"
        print(f"  [{i}] {s.display()}")
        print(f"      token=...{s.token_id[-8:]} outcome={s.outcome_name}")
        print(f"      entry={s.entry_price} exit={s.exit_price} size={s.share_amount} "
              f"(约 {notional:.2f} USDC)")
        print(f"      止盈={tp_str} 止损={sl_str}")
    print(f"  条件入场: {t.conditional_entry}  (true=等待价格, false=立即挂单)")
    if len(strategies) == 2:
        print("  提示: 已配置 2 个策略，可视为双边下注")
    print("=" * 60)

    raw = input("确认开始运行？(Y/n): ").strip()
    return raw.lower() in ("", "y", "yes")
