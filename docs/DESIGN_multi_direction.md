# Polymarket 多方向下注 — 设计文档（含 Slug→事件→市场 调研）

> 本文档为可直接交付开发工程师的完整设计。第一部分为对 Polymarket 真实数据模型的调研结论
> （已用 `research/gamma_probe.py` 对线上 API 实证，2026-07-18），第二部分为据此修订后的实现设计。
> 修订重点：slug 解析必须走 **事件（Event）** 维度而非单市场维度，并正确处理 **negRisk** 与
> **一场比赛多个子市场**（胜负/让分/大小球/角球/小局等）的结构。

---

# 第一部分：调研报告

## 1.1 调研方法
- 用项目 venv（含 `requests`）直接以 HTTP/1.1 请求 Gamma Data API 与 CLOB `/book`，不依赖
  `py-clob-client-v2`（其在沙箱因 HTTP/2 不可用，但 raw `requests` 可用，已在工作记忆中验证）。
- 探针脚本：`research/gamma_probe.py`，支持 `discover / event / markets / book / clobmarket`。
- 实证对象：`world-cup-winner`、`netanyahu-out-before-2027`、`will-jesus-christ-return-before-2027`、
  `fifwc-fra-eng-2026-07-18-more-markets`、`lol-t1-drx-2026-05-20` 等。

## 1.2 核心结论：slug → Event → Markets → Outcomes → token_ids

Polymarket 的现代数据模型是 **事件（Event）** 中心的，**不是** “一个 slug = 一个带 N 个 outcome 的市场”。
v1/v2 设计中 `resolve_slug` 调用 `GET /markets?slug=<slug>` 是**错误**的——对事件型 slug 它返回 0 条
（已实证：`/markets?slug=netanyahu-out-before-2027` → 0 条）。

正确的层级：

```
slug  ──GET /events?slug=<slug>──▶  Event
                                      ├─ id, slug, title, negRisk (事件级), active, closed, tags[]
                                      └─ markets: [ SubMarket, ... ]          ← 数组，每个是一个二元市场
                                            SubMarket:
                                              ├─ id, slug, question, conditionId
                                              ├─ negRisk (市场级, bool)
                                              ├─ negRiskMarketId, negRiskRequestId (可空)
                                              ├─ active, closed, enableOrderBook
                                              ├─ orderMinSize        (如 5)
                                              ├─ orderPriceMinTickSize (如 0.001 / 0.01)
                                              ├─ outcomes            = JSON字符串 '["Yes","No"]'
                                              ├─ outcomePrices       = JSON字符串 '["0.51","0.49"]'
                                              └─ clobTokenIds        = JSON字符串 '["<yesToken>","<noToken>"]'
```

**关键点**：
1. **每个 SubMarket 都是二元 Yes/No**，`clobTokenIds` 恒为 **2 个** token（Yes token、No token），
   与 `outcomes`/`outcomePrices` 三个数组**等长、按下标一一对应**。
2. `clobTokenIds[i]` 即 CLOB 下单用的 `token_id`，`book.market == SubMarket.conditionId`（已实证）。
3. `outcomes`/`outcomePrices`/`clobTokenIds` 都是 **JSON 字符串**，必须 `json.loads`。
4. 一个 Event 可含 1 个市场（简单二元问题）或几十至上百个子市场（一场比赛/一个多选项事件）。

## 1.3 三种结构形态（全部已实证）

| 形态 | 事件 negRisk | 子市场数 | 子市场 negRisk | 真实示例 |
|---|---|---|---|---|
| **A. 简单二元单市场** | None/False | 1 | False | `will-jesus-christ-return-before-2027`（1 市场，Yes/No） |
| **B. 非 negRisk 多子市场** | False | N（独立阈值/类型） | False | `netanyahu-out-before-2027`（6 个“X 日期前下台？”）；`fifwc-fra-eng-2026-07-18-more-markets`（45 个 Spread）；`lol-t1-drx-2026-05-20`（60 个：胜负/大小局/让分/局内 Baron 等）；`mlb-tb-bos-2026-05-09`（18 个） |
| **C. negRisk 多子市场（互斥多选项）** | True | N（每选项一个二元市场） | True | `world-cup-winner`（60 个“X 会夺冠？”）；`democratic-presidential-nominee-2028`（128 个）；`fifwc-esp-arg-2026-07-19-exact-score`（17 个“精确比分 X-Y？”） |

- **形态 B**：每个子市场彼此独立（不同问题/阈值/玩法），可任意组合下注。即用户所述
  “世界杯一场比赛对应胜负、角球、让分等多种方向”、“LoL/CS 含常规胜负 + 小局胜负”。
- **形态 C**：子市场互斥（只有一个选项会赢），由 negRisk 适配器合约把价格约束为和≈1。
  典型：冠军归属、提名归属、精确比分。
- **一场比赛可能拆成多个 Event**：如 France vs England 有 `...-more-markets`（Spread，形态 B）、
  `...-exact-score`（形态 C）、以及主赛事 slug 各自独立。用户给一个 slug 解析一个 Event。

## 1.4 negRisk 对下单的影响（关键 Bug）

实证 `world-cup-winner` 子市场 “Will Spain win…” 的 CLOB 盘口：
```
GET https://clob.polymarket.com/book?token_id=<spain-yes-token>
  market         = 0x7976b8db…992892   (== 该子市场 conditionId)
  neg_risk       = True                 ← 盘口响应里带 neg_risk 标志
  tick_size      = 0.001
  min_order_size = 5
```

SDK（`py_clob_client_v2`）中：
- `PartialCreateOrderOptions(tick_size=..., neg_risk=...)` —— **下单必须传 `neg_risk`**。
- `OrderBookSummary` 响应含 `neg_risk` 字段。

**当前 `trading._post_limit_order` 只传 `tick_size`、未传 `neg_risk`**（见 `trading.py` L113-122）。
→ **所有 negRisk 市场（形态 C：冠军/提名/精确比分）下单都会失败。** 必须修复：从盘口读 `neg_risk`
并与 `tick_size` 一并传入 `PartialCreateOrderOptions`。

## 1.5 Gamma 端点与限流
- `GET https://gamma-api.polymarket.com/events?slug=<slug>` —— **主用**，返回 Event 数组（含 markets[]）。
- `GET https://gamma-api.polymarket.com/markets?slug=<slug>` —— 仅当 slug 恰为某 SubMarket 自身 slug
  时返回 1 条；对 Event slug 返回 0。**作为兜底**：当 `/events?slug=` 返回空时回退用它，把单市场包成
  “1 市场的 Event”。
- `/events?slug=` 在极少数情况下可能命中多个 Event（同名/历史）。命中 >1 → 列出候选让用户选。
- Gamma 读接口**无需鉴权**；响应带 `Cache-Control: public, max-age=300`；`/markets` 带 `deprecation`
  header 指向 `/markets/keyset`（`?slug=` 与 `/events` 仍正常）。需处理 `429`（退避重试）。
- 子市场数可达上百 → 一次 `/events?slug=` 响应可能较大；建议 `timeout=15~20`，必要时 stream 解析。

## 1.6 字段速查（实证）
| 字段 | 含义 | 类型/示例 |
|---|---|---|
| `Event.id` / `slug` / `title` | 事件标识 | int / str / str |
| `Event.negRisk` | 事件级 negRisk | bool 或 None |
| `Event.markets[]` | 子市场数组 | list |
| `SubMarket.id` / `slug` / `question` | 子市场标识与问题 | int / str / str |
| `SubMarket.conditionId` | CLOB 市场 id（== `book.market`） | hex str |
| `SubMarket.negRisk` | 市场级 negRisk（下单用） | bool |
| `SubMarket.orderMinSize` | 最小下单量（shares） | str→float，如 5 |
| `SubMarket.orderPriceMinTickSize` | 价格步长 | str→float，0.001/0.01 |
| `SubMarket.outcomes` | `["Yes","No"]`（JSON 字符串） | str→list |
| `SubMarket.outcomePrices` | `["0.51","0.49"]`（JSON 字符串） | str→list |
| `SubMarket.clobTokenIds` | `["<yes>","<no>"]`（JSON 字符串） | str→list |
| CLOB `book.neg_risk` | 下单必传 neg_risk | bool |
| CLOB `book.tick_size` / `min_order_size` | 下单参数 | str |

---

# 第二部分：修订后的实现设计

## 2.1 顶层“下注方向”模型

一个**可下注方向 = (Event, SubMarket, Yes|No)**，即某事件某子市场的某一边代币。
- 形态 A：1 子市场 × 2 边 = 2 个方向。
- 形态 B/C：N 子市场 × 2 边 = 2N 个方向（可上百）。
- “双向下注”在单子市场内 = 同时配置 Yes 与 No 两个方向（价格和≈1，可做对冲/套利）。
- 配置一个方向 = 选定一个 `token_id` 并附其独立参数（进入价/止盈价/数量/止盈%/止损%/触发模式）
  + 下单元信息（`neg_risk`/`condition_id`/`order_min_size`/`tick_size`）。

## 2.2 新模块 `market_resolver.py`（重写，改走 /events）

### 2.2.1 数据结构
```python
@dataclass
class ResolvedOutcome:
    outcome: str                 # "Yes" / "No"（或 Over/Under 等命名，但恒为二元）
    outcome_index: int           # 0 或 1
    token_id: str                # CLOB token_id
    current_price: float | None  # 来自 outcomePrices[i]
    best_bid: float | None       # 可选：setup 展示用（fetch_book=True 时填）
    best_ask: float | None

@dataclass
class ResolvedSubMarket:
    market_id: str               # SubMarket.id
    market_slug: str             # SubMarket.slug
    question: str                # 人类可读问题
    condition_id: str            # == book.market
    neg_risk: bool               # 下单必传
    active: bool
    closed: bool
    order_min_size: float | None
    tick_size: float | None
    outcomes: list[ResolvedOutcome]   # 恒为 2 个（Yes/No）

@dataclass
class ResolvedEvent:
    event_id: str
    slug: str
    title: str
    neg_risk: bool | None        # 事件级
    active: bool
    closed: bool
    sub_markets: list[ResolvedSubMarket]
    raw: dict
```

### 2.2.2 异常
`ResolverError`(base) → `SlugNotFoundError` / `MultipleEventsError`（命中多个 Event，带候选）
/ `EventInactiveError` / `GammaRateLimitError` / `GammaApiError`。
（移除 v2 的 `MarketNegRiskError`/`MultipleMarketsError`——negRisk 不再被拒绝，而是被正确处理。）

### 2.2.3 主函数与流程
```python
GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"

def resolve_slug(slug: str, *, timeout=20.0, retries=3, backoff=1.0,
                 fetch_book=False) -> ResolvedEvent:
    """
    1. GET {GAMMA}/events?slug=<slug>（429 退避重试；非 200/网络错 → GammaApiError）。
    2. 空列表 → 回退 GET {GAMMA}/markets?slug=<slug>：命中则包成 1 子市场的 ResolvedEvent；
       仍空 → SlugNotFoundError。
    3. >1 个 Event → MultipleEventsError（带候选 event slug/title）。
    4. 取首个 Event；not active or closed → EventInactiveError。
    5. 遍历 Event.markets[]：
       - json.loads 解析 outcomes / outcomePrices / clobTokenIds（并行，等长=2）。
       - 组装 ResolvedSubMarket（neg_risk=bool(market.get('negRisk', False))，
         order_min_size/tick_size 字符串转 float）。
       - 每个 outcome 组装 ResolvedOutcome（current_price=outcomePrices[i]）。
       - fetch_book=True 时对每个 token_id 调 fetch_book_summary 填 best_bid/ask（N 次请求，
         大事件慎用；CLI 默认对“已选中子市场”再补取盘口，而非全量）。
    """
```

```python
def fetch_book_summary(token_id, *, timeout=10.0) -> tuple[float|None, float|None, bool, float|None, float|None]:
    """raw requests GET {CLOB}/book?token_id=...
    返回 (best_bid, best_ask, neg_risk, tick_size, min_order_size)。失败全 None。"""

def list_outcomes(sub: ResolvedSubMarket) -> list[ResolvedOutcome]: ...   # 便于测试 mock
def pick_outcome(sub: ResolvedSubMarket, outcome: str | int) -> ResolvedOutcome:
    """outcome 为 int 当下标；为 str 时大小写不敏感匹配；失败抛 ValueError。"""
```

### 2.2.4 大事件展示策略
形态 B/C 子市场可达 60~128 个，不能一次性平铺。提供：
- `--filter <关键字>`（CLI）/ `/slug <slug> <关键字>`（Telegram）：按 `question` 子串过滤
  （如 `handicap`、`over`、`game 1`、`exact score`）。
- 分页：CLI 每屏 20 条，回车翻页；Telegram 每条消息 10 条 + `/slugmore <slug> <page>`。
- 每条展示：`[全局序号] <question>  Yes=<price>/bid  No=<price>/ask  negR=<T/F>`。

## 2.3 `Direction` 数据类扩展（`config.py`）

在 v2 基础上**新增下单元信息字段**（从 resolver 写入，下单时读取）：
```python
@dataclass
class Direction:
    # —— 标识 ——
    event_slug: str = ""         # 解析来源 Event slug
    market_slug: str = ""        # SubMarket.slug
    market_id: str = ""          # SubMarket.id
    condition_id: str = ""       # == book.market
    question: str = ""           # 子市场问题（展示用）
    outcome: str = ""            # "Yes"/"No"
    outcome_index: int = 0
    token_id: str = ""
    label: str = ""              # 为空回退 f"{question} :: {outcome}"
    # —— 下单元信息（关键）——
    neg_risk: bool = False       # ★ 形态 C 必须 True
    order_min_size: float | None = None
    tick_size: float | None = None
    # —— 每方向独立参数（同 v2）——
    share_amount: float = 10.0
    entry_price: float = 0.50
    exit_price: float = 0.55
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    conditional_entry: bool = True

    def display(self) -> str:
        return self.label or (f"{self.question} :: {self.outcome}" if self.question
                              else (f"{self.event_slug}::{self.outcome}" if self.event_slug else self.token_id))
```
> `neg_risk` 存入配置是为了展示与兜底；实际下单时仍以 `client.get_order_book(token_id)` 返回的
> `neg_risk` 为准（最新值），二者应一致。

## 2.4 `trading._post_limit_order` 修复（★ 核心 Bug 修复）
```python
def _post_limit_order(client, token_id, side, amount, price, label) -> dict:
    if client is None: raise RuntimeError("Client not initialised")
    print(f"[trading] {label}: {'BUY' if side==Side.BUY else 'SELL'} {amount} @ {price}")
    try:
        book = client.get_order_book(token_id)
        tick_size = str(book.get("tick_size", "0.01"))
        neg_risk = bool(book.get("neg_risk", False))      # ★ 新增：从盘口读 neg_risk
        resp = client.create_and_post_order(
            order_args=OrderArgs(token_id=token_id, price=float(price), side=side, size=float(amount)),
            options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),  # ★ 传入
            order_type=OrderType.GTC)
        if resp is None: raise OrderApiError("下单返回空响应", raw="empty")
        return resp if isinstance(resp, dict) else {"response": str(resp)}
    except OrderError: raise
    except PolyApiException as exc: raise _map_poly_exception(exc) from exc
    except Exception as exc: raise OrderApiError("下单失败(未知错误)", raw=str(exc)) from exc
```
> 即便 `Direction.neg_risk` 未设置，下单时也从盘口实时读取，保证 negRisk 市场可下单。

## 2.5 CLI setup 流程修订（`setup_cli.py`）
四步交互（支持大事件）：
1. `resolve_slug(slug)`；失败按异常中文提示并退出。
2. 打印 Event 标题/状态 + 子市场列表（分页 + `--filter`）。每行：
   `[i] <question>  Yes=<price>  No=<price>  negR=<T/F>  min=<orderMinSize>  tick=<tickSize>`。
3. `input("选择子市场(可多选, 逗号/空格): ")` → 校验下标。
4. 对每个选中子市场：
   - `input("下注边 [Y]es/[N]o/[B]oth: ")` → 决定生成 1 或 2 个方向。
   - 对每边：`_prompt_float` 问 entry_price / exit_price，`_prompt_float` 问 share_amount
     （`--usdc` 模式按 `shares=round(usdc/entry_price)` 反算），`_prompt_pct` 问 take_profit_pct /
     stop_loss_pct（可空），label 可空。
   - 校验：价格∈(0,1)；`share_amount ≥ order_min_size`（否则拒绝并提示最小量）；entry_price 按
     `tick_size` 向下取整并提示；`entry_price ≥ exit_price` 软警告。
   - 构造 `Direction`（含 `neg_risk`/`condition_id`/`order_min_size`/`tick_size`/`question` 等）
     并 `guard.add_direction()`（按 token_id 去重替换）。
5. **negRisk 双向提示**：若在**同一 negRisk 子市场**同时选 Yes+No → 提示“同子市场 Yes+No 价格和≈1，
   属对冲/套利”；若在**同一 negRisk Event 的不同子市场**都选 Yes → 警告“这些选项互斥，至多一个会赢，
   同时做多 Yes 会全部归零除一个外”，需确认。

## 2.6 Telegram 命令修订（`bot/telegram_bot.py`）
| 命令 | 语法 | 作用 |
|---|---|---|
| 预览 | `/slug <slug> [filter] [page]` | 解析 Event，分页列出子市场（含 Yes/No 价、negR、min、tick） |
| 翻页 | `/slugmore <slug> <page> [filter]` | 下一页子市场 |
| 添加 | `/add <slug> <submarket_idx> <Y\|N\|B> [entry] [exit] [amount]` | 选定子市场与边，用默认/传入参数添加（可 Both） |
| 选中 | `/select <dir_idx>` | 选中已配置方向，供简命令使用 |
| 列表 | `/listdirs` | 列出已配置方向及参数 |
| 删除 | `/rmdir <dir_idx>` | 删除一个方向 |
| 清空 | `/cleardirs` | 清空（需确认） |
| 改参数 | `/price /exit_price /takeprofit /stoploss /amount /triggermode [dir_idx] <val>` | 作用于选中或指定方向 |

`/add <slug> <submarket_idx> Y 0.48 0.55 20` 一步加一个方向；`B` 表示同子市场 Yes+No 都加（各自默认参数）。
解析失败在命令函数内 `try/except ResolverError` 返回中文提示。

## 2.7 Runtime 多方向（同 v2，要点重申）
- `_cycle_once`：先全局 `_detect_fills(t)`，再遍历 `t.directions`，每个方向独立 `get_market_data`
  → `should_enter/should_exit`（用各自参数）→ `_safe_enter/_safe_exit`。
- `_safe_enter/_safe_exit`：`except OrderError` → `_notify_order_error` + print，单方向失败不中断。
- 持仓/挂单/状态通知按 `token_id` 映射到 `Direction.display()`，多方向并列。
- negRisk 无需 runtime 特殊处理：`_post_limit_order` 已从盘口读 `neg_risk` 传入。

## 2.8 下单错误分类（`trading_errors.py`，同 v2）
`OrderError`(base) → `InsufficientBalanceError`(余额不足) / `InvalidSideError`(方向错误) /
`MarketClosedError`(市场关闭) / `OrderTooSmallError`(低于 orderMinSize) / `InvalidPriceError`
(tick/价格非法) / `NegRiskMismatchError`(negRisk 参数不符，兜底) / `OrderApiError`(认证/网络/服务端)。
`_map_poly_exception` 按状态码+关键字映射（顺序：401/403→认证；5xx→服务端；balance/insufficient→余额；
min size/ordermin/too small→过小；tick/price increment/invalid price→价格非法；closed/inactive→关闭；
side/sell...position→方向；兜底 OrderApiError）。
`notify_order_error(direction, side, size, price, error)` 经 `NotifierManager._dispatch` 推 Telegram+Webhook。

## 2.9 配置与兼容（同 v2 决策）
- 破坏性替换旧 `config.json` schema；新版 `version:3`，`directions[]` 元素为含上述新字段的 `Direction`。
- 删除旧交易 env（`TOKEN_ID/MARKETS/SHARE_AMOUNT/...`）；新增可选 `DIRECTIONS` env（JSON 数组）供无头部署。
- 首次无 config 且无 `DIRECTIONS` env → 提示运行 `setup` 并退出。
- `ConfigGuard`：`directions()/snapshot()/get_direction/add_direction/add_directions/
  update_direction/remove_direction/clear_directions/reload_from_disk()`（热重载：每轮检查 config.json
  mtime，变化则重载 directions）。

## 2.10 文件变更清单
| 文件 | 动作 | 关键改动 |
|---|---|---|
| `trading_errors.py` | 新增 | 异常类（含 NegRiskMismatchError） |
| `market_resolver.py` | 新增 | **走 `/events?slug=`**，`ResolvedEvent/SubMarket/Outcome`，`fetch_book_summary`，过滤/分页辅助 |
| `config.py` | 修改 | `Direction` 增 neg_risk/condition_id/order_min_size/tick_size/question/market_*；`TradingParams`/`ConfigGuard` 同 v2 |
| `trading.py` | 修改 | **`_post_limit_order` 传 `neg_risk`**（★ Bug 修复）；接入 `trading_errors` |
| `notifications/manager.py` | 修改 | `notify_order_error` + 多方向状态方法 |
| `runtime.py` | 修改 | 多方向循环 + 失败通知 + 热重载 |
| `bot/telegram_bot.py` | 修改 | `/slug /slugmore /add /select /listdirs /rmdir /cleardirs` + 带 idx 参数命令 |
| `setup_cli.py` | 新增 | 四步交互（子市场→边→参数）+ filter/分页 + negRisk 提示 |
| `main.py` | 修改 | argparse `run/setup/list` |
| `strategy.py` | 基本不变 | 单 token，多方向由 runtime 逐方向调用 |

实现顺序：`trading_errors.py` → `config.py` → `market_resolver.py` → `trading.py`（含 neg_risk 修复）
→ `notifications/manager.py` → `runtime.py` → `bot/telegram_bot.py` → `setup_cli.py`+`main.py` → 联调。

## 2.11 验收标准（补充 negRisk/多子市场）
1. `python main.py setup --slug will-jesus-christ-return-before-2027` → 列出 1 子市场 2 边，可选 Yes/No。
2. `--slug netanyahu-out-before-2027`（形态 B）→ 列出 6 子市场，可多选不同阈值子市场各自 Yes/No。
3. `--slug world-cup-winner`（形态 C negRisk）→ 列出 60 子市场，`--filter spain` 过滤；选某队 Yes 后
   `config.json` 中 `neg_risk=true`；运行时下单请求体含 `neg_risk=True`（可由日志/抓包确认）。
4. `--slug lol-t1-drx-2026-05-20` → 列出 60 子市场（胜负/大小局/让分/局内事件），可按 `game 1` 过滤。
5. `--slug fifwc-esp-arg-2026-07-19-exact-score`（形态 C）选多个 Yes → 触发“互斥”警告。
6. 下单失败（余额不足/方向错误/市场关闭/低于 min/tick）→ 控制台+Telegram+Webhook 中文提示，整轮不中断。
7. `--slug <不存在的>` → 中文“未找到市场”。
8. 重复 setup 追加；`--clear`/`/cleardirs` 清空。
9. bot 运行中 CLI 改 config → 下一轮“配置已热重载”。

## 2.12 风险与边界
1. **negRisk 互斥**：形态 C 不同子市场同选 Yes 至多一个赢，其余归零——必须在 setup 给警告。
2. **一场比赛多 Event**：主赛事/More Markets/Exact Score 是不同 slug 不同 Event；用户需分别 setup。
   可选增强：解析后列出“同前缀的兄弟 Event slug”供选择（本期不强制）。
3. **大事件响应**：上百子市场响应较大；`timeout=20`，必要时 stream；`fetch_book` 默认只对已选子市场补取。
4. **`/markets?slug=` 兜底**：仅当 `/events?slug=` 空时回退，把单市场包成 1 子市场 Event。
5. **多 Event 命中**：`/events?slug=` 返回 >1 → `MultipleEventsError` 列候选。
6. **tick/minSize 校验**：每子市场可能不同（0.001/0.01；min=5），按所选子市场字段校验/取整。
7. **Gamma 限流/429**：退避重试，耗尽抛 `GammaRateLimitError`，不崩溃。
8. **CLOB SDK HTTP/2 沙箱失败**：下单/查盘口仍用 SDK；Gamma 解析与 setup 盘口展示用 raw `requests`。
9. **neg_risk 来源一致性**：配置存一份，下单以盘口实时值为准；二者应一致，不一致以盘口为准并告警。
10. **并发编辑**：热重载仅重载 directions（同 v2）。

---

# 附录 A：调研探针
`research/gamma_probe.py`（已落地，仅调研用）：`discover / event / markets / book / clobmarket`。
复现示例：
```
.venv/Scripts/python.exe research/gamma_probe.py event netanyahu-out-before-2027
.venv/Scripts/python.exe research/gamma_probe.py markets netanyahu-out-before-2027   # 返回 0，证明 /markets?slug= 不适用 Event slug
.venv/Scripts/python.exe research/gamma_probe.py event lol-t1-drx-2026-05-20
```

# 附录 B：真实结构速记
- 简单二元：`will-jesus-christ-return-before-2027` → Event(1 market, Yes/No, 2 tokens)。
- 非 negRisk 多子市场：`netanyahu-out-before-2027`(6)、`lol-t1-drx-2026-05-20`(60)、
  `fifwc-fra-eng-2026-07-18-more-markets`(45, Spread)。
- negRisk 多子市场：`world-cup-winner`(60)、`fifwc-esp-arg-2026-07-19-exact-score`(17)、
  `democratic-presidential-nominee-2028`(128)。
- CLOB `/book` 对 negRisk 市场 `neg_risk=True`、`tick_size=0.001`、`min_order_size=5`、
  `market==SubMarket.conditionId`。
