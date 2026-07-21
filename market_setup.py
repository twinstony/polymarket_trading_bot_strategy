"""
启动时交互式市场配置。

流程：
1. 输入 Polymarket 市场 URL 或 slug（回车跳过使用当前 .env 配置）
2. 通过 Gamma API 解析该市场所有可交易 market 及其 outcomes（下注 token）
3. 命令行选择 market 与 outcome，MARKET_LABEL 自动补全为人类可读内容
4. 依次输入 entry_price / exit_price / share_amount / take_profit_pct / stop_loss_pct
   （均可回车跳过保留当前值）
5. 汇总策略信息由用户确认后写入 config.json 并开始运行
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

import httpx

from config import Config, ConfigGuard

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"


def run_interactive_setup(config: Config, guard: ConfigGuard) -> bool:
    """运行交互式市场配置。返回 True 表示可继续运行，False 表示应退出。"""
    print("\n" + "=" * 72)
    print(" Polymarket 交互式市场配置")
    print("=" * 72)

    current = guard.active_market()
    if current and current.token_id:
        t = config.trading
        print(
            f"当前活动市场: {current.display()}\n"
            f"  entry={t.entry_price} exit={t.exit_price} "
            f"shares={t.share_amount} conditional_entry={t.conditional_entry}"
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

    market = select_market(markets)
    if market is None:
        print("[setup] 已取消")
        return False

    token_id, outcome_name = select_outcome(market)
    if token_id is None:
        print("[setup] 已取消")
        return False

    label = build_label(market, outcome_name)

    params = input_params(config)
    if params is None:
        print("[setup] 已取消")
        return False

    if not confirm(label, token_id, params, config):
        print("[setup] 用户取消，退出")
        return False

    # 写入配置（持久化到 config.json）
    guard.add_or_switch_market(token_id, label)
    guard.update(**params)
    print(f"[setup] 配置已保存: {label}")
    return True


# --------------------------------------------------------------------------- #
# slug 解析
# --------------------------------------------------------------------------- #
def extract_slug(raw: str) -> str:
    """从完整 URL 或裸 slug 中提取 slug。"""
    raw = raw.strip()
    if not raw:
        return ""
    # 含 http(s):// 视为 URL，取最后一段路径
    if "://" in raw:
        parsed = urllib.parse.urlparse(raw)
        path = parsed.path.rstrip("/")
        if not path:
            return ""
        return path.rsplit("/", 1)[-1]
    # 裸 slug：去掉可能的前导斜杠
    return raw.strip("/").rsplit("/", 1)[-1]


# --------------------------------------------------------------------------- #
# Gamma API 查询
# --------------------------------------------------------------------------- #
def fetch_markets(slug: str) -> list[dict[str, Any]]:
    """查询 slug 对应的所有可交易 market。

    优先用 /events 端点（一个 event 含多个 market），
    fallback 到 /markets 端点（单个 market）。
    返回统一结构的 dict 列表。
    """
    # 1) 优先 events 端点
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

    # 2) fallback markets 端点
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
    # 若 outcomes 与 tokenIds 数量不一致，按较短的截断
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
    """outcomes / clobTokenIds 在 Gamma 返回里是 JSON 字符串。"""
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
# 交互：选择 outcome（下注方向）
# --------------------------------------------------------------------------- #
def select_outcome(market: dict[str, Any]) -> tuple[str, str] | None:
    """展示某 market 的所有 outcomes 供选择，返回 (token_id, outcome_name) 或 None。"""
    print(f"\n市场: {market['question']}")
    print("下注选项（含实时盘口）:")

    outcomes = market["outcomes"]
    token_ids = market["clob_token_ids"]
    prices: list[tuple[float | None, float | None]] = []
    for i, (name, tid) in enumerate(zip(outcomes, token_ids), 1):
        bid, ask = fetch_token_price(tid)
        prices.append((bid, ask))
        price_str = f"  买价={ask}" if ask is not None else ""
        price_str += f" 卖价={bid}" if bid is not None else ""
        if not price_str:
            price_str = "  (无盘口)"
        print(f"  [{i}] {name}  token=...{tid[-8:]}{price_str}")

    while True:
        raw = input(f"\n选择下注方向编号 (1-{len(outcomes)})，q 取消: ").strip()
        if raw.lower() == "q":
            return None
        try:
            idx = int(raw)
            if 1 <= idx <= len(outcomes):
                return token_ids[idx - 1], outcomes[idx - 1]
            print(f"[setup] 请输入 1-{len(outcomes)} 之间的数字")
        except ValueError:
            print("[setup] 无效输入，请输入数字")


# --------------------------------------------------------------------------- #
# 交互：输入交易参数
# --------------------------------------------------------------------------- #
# 哨兵值：区分"回车跳过"与"取消"
_SKIP = object()   # 回车跳过（保留当前值，不更新）
_CANCEL = object()  # 用户按 q 取消整个流程


def input_params(config: Config) -> dict[str, Any] | None:
    """依次询问 5 个参数，回车跳过保留当前值。返回适合 guard.update 的 kwargs。"""
    t = config.trading
    params: dict[str, Any] = {}

    # entry_price / exit_price / share_amount：必填浮点数，回车=保留当前
    for key, label_zh in (
        ("entry_price", "买入价 entry_price"),
        ("exit_price", "卖出价 exit_price"),
        ("share_amount", "买入数量 share_amount"),
    ):
        current = getattr(t, key)
        val = _ask_float(f"{label_zh}（当前 {current}，回车保留）: ", current)
        if val is None:  # 用户按 q 取消
            return None
        if val != current:
            params[key] = val

    # take_profit_pct / stop_loss_pct：可选，回车跳过，n 清除，q 取消
    for key, label_zh in (
        ("take_profit_pct", "止盈百分比 take_profit_pct"),
        ("stop_loss_pct", "止损百分比 stop_loss_pct"),
    ):
        current = getattr(t, key)
        cur_label = "未设置" if current is None else current
        val = _ask_optional_float(
            f"{label_zh}（当前 {cur_label}，回车跳过，n 清除，0.1=10%）: "
        )
        if val is _CANCEL:
            return None
        if val is _SKIP:
            continue  # 保留当前值，不加入 params
        # val 是 float（新值）或 None（用户输入 n 清除）
        if val != current:
            params[key] = val

    return params


def _ask_float(prompt: str, current: float) -> float | None:
    """问一个必填浮点数，回车=保留当前值，q=取消返回 None。"""
    while True:
        raw = input(prompt).strip()
        if raw.lower() == "q":
            return None
        if raw == "":
            return current
        try:
            v = float(raw)
            if v <= 0:
                print("[setup] 必须大于 0")
                continue
            return v
        except ValueError:
            print("[setup] 无效数字，请重新输入")


def _ask_optional_float(prompt: str) -> Any:
    """问一个可选浮点数。

    返回：
      - _SKIP：用户回车跳过（保留当前）
      - _CANCEL：用户按 q 取消整个流程
      - None：用户输入 n 主动清除
      - float：用户输入的值
    """
    while True:
        raw = input(prompt).strip()
        if raw.lower() == "q":
            return _CANCEL
        if raw == "":
            return _SKIP
        if raw.lower() == "n":
            return None  # 清除
        try:
            return float(raw)
        except ValueError:
            print("[setup] 无效数字，请重新输入（回车跳过，n 清除）")


def build_label(market: dict[str, Any], outcome_name: str) -> str:
    """生成人类可读的 MARKET_LABEL。"""
    question = market["question"]
    return f"{question} [{outcome_name}]"


# --------------------------------------------------------------------------- #
# 确认
# --------------------------------------------------------------------------- #
def confirm(
    label: str,
    token_id: str,
    params: dict[str, Any],
    config: Config,
) -> bool:
    """展示策略摘要并由用户确认。"""
    t = config.trading
    # params 里有的用 params，没有的用当前值
    entry = params.get("entry_price", t.entry_price)
    exit_p = params.get("exit_price", t.exit_price)
    shares = params.get("share_amount", t.share_amount)
    tp = params.get("take_profit_pct", t.take_profit_pct)
    sl = params.get("stop_loss_pct", t.stop_loss_pct)

    notional = entry * shares
    tp_str = f"{tp*100:.1f}%" if isinstance(tp, float) else "未设置"
    sl_str = f"{sl*100:.1f}%" if isinstance(sl, float) else "未设置"

    print("\n" + "=" * 50)
    print(" 策略确认")
    print("=" * 50)
    print(f"  市场标签: {label}")
    print(f"  Token ID: {token_id}")
    print(f"  买入价:   {entry}")
    print(f"  卖出价:   {exit_p}")
    print(f"  买入数量: {shares} 份  (约 {notional:.2f} USDC)")
    print(f"  止盈百分比: {tp_str}")
    print(f"  止损百分比: {sl_str}")
    print(f"  条件入场:   {t.conditional_entry}  (true=等待价格, false=立即挂单)")
    print("=" * 50)

    raw = input("确认开始运行？(Y/n): ").strip()
    return raw.lower() in ("", "y", "yes")
