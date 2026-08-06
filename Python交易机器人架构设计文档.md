# Polymarket 交易机器人 — Python 架构设计文档

> 配套文档：
> - [浏览器扩展详细设计文档.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/浏览器扩展详细设计文档.md) — Chrome 侧边栏扩展设计（HTTP 全量轮询，无 WebSocket）
> - [docs/polymarket-knowledge.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/docs/polymarket-knowledge.md) — 安全约束与教训知识库
>
> 本文档描述 **Python bot 当前架构** 与 **为浏览器扩展服务的 FastAPI bridge 设计**。bridge 采用 **HTTP API 全量轮询**，不使用 WebSocket / SSE。

---

## 1. 背景与目标

### 1.1 项目背景

本项目是一个 Python 编写的 Polymarket CLOB 自动交易机器人，面向电竞/事件预测市场。核心特性：

- **多策略 asyncio 架构**：每个 outcome token 对应一个独立的 `StrategyRuntime`，共享一把全局 `asyncio.Lock` 串行化下单。
- **纯内存配置**：策略列表每次启动由交互式 CLI 填充，不持久化到磁盘（对齐 [market_setup.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/market_setup.py) 的内存模式）。
- **安全优先**：强制 EXIT_PRICE、防重复下单、不基于仓位差自动卖、双源成交检测（CLOB trades + portfolio API 对账）。
- **多通道通知**：Telegram 多 bot 推送 + Webhook 多端点。
- **浏览器扩展桥接**：通过 FastAPI bridge（同进程）向 Chrome 侧边栏扩展暴露 HTTP API，扩展端全量轮询获取状态。

### 1.2 目标用户与场景

- **用户**：Polymarket 电竞/事件交易玩家，持有钱包与 USDC，运行本 bot。
- **核心场景**：
  1. CLI 启动时输入市场 URL，交互式配置每个 outcome 的 entry/exit/amount/TP/SL，启动多策略监控。
  2. bot 每个周期自动检测成交、对账持仓、维护保护卖单、在条件满足时入场。
  3. 通过 Telegram / Webhook 接收 fill 通知与周期状态心跳。
  4.（可选）启动 FastAPI bridge，用 Chrome 侧边栏扩展可视化操作：查看持仓、一键下单、批量全局指令。

### 1.3 范围与非目标

**范围（本文档覆盖）**：
- Python bot 模块架构、数据流、状态机、并发模型。
- 成交检测与对账机制（双源 + 三层匹配）。
- 安全约束清单（18 条，标注代码实现位置）。
- 配置契约（Config / StrategyConfig / .env 专属约束）。
- 通知系统设计。
- FastAPI bridge 设计（HTTP API 全量轮询，无 WS）。
- 信号处理与优雅停止。
- 已知限制与文档不一致项。

**非目标**：
- **不做行情终端**：bot 是策略执行者，不画 K 线/深度图。
- **bot 自身可独立运行**：FastAPI bridge 是可选横切层，不启用 bridge 时 bot 功能完整。
- **不实现 WS/SSE**：bridge 仅暴露 HTTP 接口，扩展端全量轮询。
- **不修改 Polymarket 安全逻辑**：bridge 只发指令，是否下单/撤单由 bot 决定。

---

## 2. 总体架构

### 2.1 架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                       Python Bot Process (asyncio)                  │
│                                                                     │
│  ┌──────────┐   ┌──────────────────┐   ┌────────────────────────┐  │
│  │ main.py  │──▶│ RuntimeManager   │──▶│ N × StrategyRuntime    │  │
│  │ (entry)  │   │  - strategies[]  │   │  - 状态机 + _cycle_once │  │
│  └────┬─────┘   │  - _order_lock   │   │  - fill 检测 + 对账    │  │
│       │         │  - _status_loop  │   └───────────┬────────────┘  │
│       │         └────────▲─────────┘               │               │
│       │                  │                         ▼               │
│       │         ┌────────┴─────────┐   ┌────────────────────────┐  │
│       │         │ market_setup.py  │   │ trading.py             │  │
│       │         │ (启动期填充策略)  │   │ (CLOB V2 SDK 包装)     │  │
│       │         └──────────────────┘   └───────────┬────────────┘  │
│       │                                            │               │
│       │         ┌──────────────────────────────────┴────────────┐  │
│       │         │  NotifierManager (横切推送)                    │  │
│       │         │  → TelegramNotifier × N + WebhookNotifier × N │  │
│       │         └───────────────────────────────────────────────┘  │
│       │                                                             │
│       │         ┌───────────────────────────────────────────────┐  │
│       └────────▶│ FastAPI Bridge (可选横切层, 同进程)            │  │
│                 │  - 持有 RuntimeManager 单例引用                │  │
│                 │  - HTTP /api/* 接口 (无 WS/SSE)                │  │
│                 │  - 读: status_snapshot() / get_open_orders()   │  │
│                 │  - 写: 注入 StrategyConfig / 触发下单(持锁)    │  │
│                 └───────────────────┬───────────────────────────┘  │
└─────────────────────────────────────┼───────────────────────────────┘
                                      │ HTTP (全量轮询, 2-3s)
                                      ▼
                        ┌──────────────────────────┐
                        │  Chrome Side Panel 扩展   │
                        │  (见浏览器扩展设计文档)    │
                        └──────────────────────────┘

                            ┌────────────────────────┐
                            │  Polymarket 远端        │
                            │  - CLOB (下单/盘口)     │
                            │  - Gamma (市场元数据)   │
                            │  - data-api (持仓查询)  │
                            └────────────────────────┘
```

### 2.2 关键约束

| 约束 | 说明 | 依据 |
|---|---|---|
| 策略配置纯内存 | 不持久化到磁盘，每次启动清空，新 URL 输入清空已有列表 | [market_setup.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/market_setup.py) 内存模式 |
| 强制 EXIT_PRICE | 下单前校验 exit_price 必填，否则禁止入场 | [docs/polymarket-knowledge.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/docs/polymarket-knowledge.md) 强制卖单保护 |
| 防重复下单 | 下单前查 `_has_matching_open_order`（token+side+price+size 四元匹配） | 重复下单教训 |
| 不基于仓位差自动卖 | 保护 SELL 绑定 bot 自己的 BUY order ID；order_id 不可靠时启发式回退 | 手动买入被自动卖出事故 |
| 全局锁串行化下单 | 全进程一把 `asyncio.Lock _order_lock`，所有下单操作持锁 | CLOB SDK 签名竞争 + rate limit |
| bridge 不绕过 bot 检查 | bridge 只发指令，`_has_matching_open_order` / `TRADING_ENABLED` 等检查不绕过 | bot 仍是策略执行唯一决策者 |
| bridge 不持有业务状态 | bridge 仅转发，不缓存策略/仓位，所有数据实时读 RuntimeManager | 避免双写 |
| 不使用 WS/SSE | bridge 仅 HTTP，扩展端全量轮询 | 用户决策 |

---

## 3. 模块职责清单

| 模块 | 文件 | 职责 | 关键 API |
|---|---|---|---|
| 入口 | [main.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/main.py) | asyncio 入口，组装各组件并启动 | `main()` |
| 运行时 | [runtime.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/runtime.py) | `StrategyRuntime`（单策略状态机+主循环）+ `RuntimeManager`（多实例协调+全局锁+状态推送） | `StrategyRuntime.run/_cycle_once/snapshot`、`RuntimeManager.add_strategy/start_all/stop_all` |
| 交易 | [trading.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/trading.py) | CLOB V2 SDK 包装层 | `init_client/enter_position/exit_position/get_market_data/get_open_orders/get_recent_trades/cancel_open_orders_for_token` |
| 策略 | [strategy.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/strategy.py) | 决策纯函数 | `should_enter/should_exit/tp_trigger_price/sl_trigger_price`、`MarketData/PositionData` |
| 配置 | [config.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/config.py) | env 加载 + StrategyConfig（纯内存） | `Config.load`、`StrategyConfig`、`TelegramBot/Webhook` |
| 市场解析 | [market_setup.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/market_setup.py) | 交互式 CLI 配置 | `extract_slug/fetch_markets/_normalize_market/run_interactive_setup` |
| 通知 | [notifications/manager.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/notifications/manager.py) | 路由 + 格式化 | `NotifierManager.notify_fill/notify_status/notify_started/notify_stopped` |
| 通知-Telegram | [notifications/telegram_notifier.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/notifications/telegram_notifier.py) | 多 bot 推送 | `TelegramNotifier.send` |
| 通知-Webhook | [notifications/webhook_notifier.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/notifications/webhook_notifier.py) | 多端点 webhook | `WebhookNotifier.send` |
| 工具脚本 | [tools/layered_order_runner.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/tools/layered_order_runner.py) | 独立 CLI 分层下单（不经 RuntimeManager） | `_ensure_exit_coverage`、state file 持久化 |
| **FastAPI bridge** | （尚未实现，设计契约见第 11 章） | 横切层，HTTP API 暴露给扩展 | `GET /api/*` + `POST /api/*` |

---

## 4. 数据流

### 4.1 启动流

```
.env
  │
  ▼
Config.load()                          # config.py:140
  │
  ▼
trading.init_client(config)            # trading.py:47  → ClobClient
  │
  ▼
NotifierManager(telegram_bots, webhooks)
  │
  ▼
market_setup.run_interactive_setup(config, client)
  │  └─ 用户输入 URL → extract_slug → fetch_markets → _normalize_market
  │  └─ 选择方向（单边/双边）→ 配置 entry/exit/amount/TP/SL
  │  └─ 输入新 URL 时 strategies.clear()
  │  └─ 返回 list[StrategyConfig]
  ▼
RuntimeManager.add_strategy(cfg) × N   # runtime.py:711
  │
  ▼
RuntimeManager.start_all()             # runtime.py:723
  │  └─ asyncio.create_task(rt.run()) × N
  │  └─ asyncio.create_task(_status_loop())
  ▼
等待 SIGINT/SIGTERM                     # main.py 信号处理
```

### 4.2 单 cycle 流（`_cycle_once`，runtime.py）

```
每个 StrategyRuntime 独立循环，每 poll_interval 秒一轮：

  ┌─────────────────────────────────────────────────────┐
  │ 1. _detect_fills (CLOB get_trades)                 │
  │    - order_id 精确匹配 _entry_order_ids/_exit_order_ids │
  │    - 启发式回退 (_trade_matches_intent)            │
  │    - _apply_fill 更新 _position                    │
  ├─────────────────────────────────────────────────────┤
  │ 2. _reconcile_position (portfolio API 对账)        │
  │    - data-api.polymarket.com/positions?user=funder │
  │    - baseline 减法避免误接管手动仓位               │
  │    - 差异时向上/向下修正 _position                 │
  ├─────────────────────────────────────────────────────┤
  │ 3. 若持仓 (_position.has_position):                │
  │    a. should_exit (TP/SL/EXIT_PRICE 触发)          │
  │    b. 维护保护卖单 (缺则挂, 撤则补)                │
  │    c. SELL 成交把 size 减到 ≤0 → _closed=True      │
  ├─────────────────────────────────────────────────────┤
  │ 4. 若未持仓:                                       │
  │    a. _has_matching_open_order 检查                │
  │    b. should_enter (best_ask ≤ entry_price)        │
  │    c. enter_position (持 _order_lock)              │
  │    d. _remember_order_id + _entry_attempted=True   │
  ├─────────────────────────────────────────────────────┤
  │ 5. cycle % status_every == 0 → _heartbeat (stdout) │
  └─────────────────────────────────────────────────────┘
```

### 4.3 通知流

```
runtime 事件
  │
  ▼
NotifierManager._dispatch
  │
  ├──▶ TelegramNotifier.send × N  (HTTP POST api.telegram.org)
  └──▶ WebhookNotifier.send × N   (HTTP POST webhook url)

事件分类:
  - fill: 单 fill 详情 (side/size/price/order_id)
  - status: 聚合所有 runtime.snapshot() (周期性, _status_loop)
  - started: 启动时 (notify_started)
  - stopped: 停止时 (notify_stopped)
  - config_change: 配置变更
```

### 4.4 bridge 读流（扩展 → bridge → bot）

```
扩展端每 2-3s 轮询:
  GET /api/positions
    │
    ▼
  bridge 调 RuntimeManager (遍历 _runtimes)
    │
    ▼
  每个 StrategyRuntime.snapshot()   # runtime.py:681
    │
    ▼
  bridge 聚合为 {positions: [StrategySnapshot, ...]} 返回

  GET /api/status
    │
    ▼
  bridge 聚合 (单数/总额/PNL), PNL 用 data-api mark price
```

### 4.5 bridge 写流（扩展 → bridge → bot）

```
扩展端 POST /api/order
  │
  ▼
bridge 构造 StrategyConfig
  │
  ▼
RuntimeManager.add_strategy(cfg)      # 加入 _runtimes
  │
  ▼
asyncio.create_task(rt.run())         # 启动新策略循环
  │
  ▼
首次 cycle 内:
  async with _order_lock:             # runtime.py:190
    enter_position(client, token, BUY, amount, price)
  │
  ▼
bridge 返回 {position_id, order_id, state}
```

---

## 5. StrategyRuntime 状态机

### 5.1 状态枚举（runtime.py:38-42）

```python
S_WAITING_ENTRY     = "待入场"
S_BUY_PENDING       = "已挂买单（待成交）"
S_HOLDING_NO_SELL   = "持仓中（待挂卖单）"
S_HOLDING_WITH_SELL = "持仓中（已挂保护卖单）"
S_CLOSED            = "已平仓"
```

### 5.2 状态派生规则（runtime.py:647-654）

状态**不是显式赋值**，而是由运行时字段派生：

```python
def _current_state(self) -> str:
    if self._closed:
        return self.S_CLOSED
    if self._position.has_position:
        return self.S_HOLDING_WITH_SELL if self._has_open_sell_order else self.S_HOLDING_NO_SELL
    if self._entry_attempted or self._has_open_buy_order:
        return self.S_BUY_PENDING
    return self.S_WAITING_ENTRY
```

### 5.3 驱动字段

| 字段 | 类型 | 作用 |
|---|---|---|
| `_closed` | bool | SELL 成交把 size 减到 ≤0 时置 True；对账发现 portfolio 归零时置 True |
| `_position.has_position` | bool | size > 0 |
| `_has_open_sell_order` | bool | 缓存的开放卖单标志 |
| `_has_open_buy_order` | bool | 缓存的开放买单标志 |
| `_entry_attempted` | bool | 入场尝试过即置 True，防重复下单 |
| `_entry_order_ids` | set[str] | bot 创建的 BUY order ID 集合 |
| `_exit_order_ids` | set[str] | bot 创建的 SELL order ID 集合 |
| `_entry_position_baseline` | tuple[float,float] \| None | 入场前 portfolio (size, avg)，用于 baseline 减法 |
| `_exit_order_baseline` | float \| None | 入场前同价 SELL 数量，用于分层保护 |
| `_seen_trade_ids` | set[str] | 已处理的 trade ID，避免重复应用 |
| `_first_fill_check_done` | bool | 独立标志，修复 first_run bug |

### 5.4 状态机图

```
                     ┌─────────────────┐
                     │  待入场          │
        ┌───────────▶│  WAITING_ENTRY  │◀───────────┐
        │            └────────┬────────┘            │
        │                     │ enter_position      │ _entry_attempted=False
        │                     │ _entry_attempted=True _has_open_buy_order=False
        │                     ▼                      │
        │            ┌─────────────────┐            │
        │            │ 已挂买单         │            │
        │            │  BUY_PENDING    │────────────┘
        │            └────────┬────────┘ 撤单 (cancel_open_orders_for_token)
        │                     │ BUY fill 检测到
        │                     │ _position.has_position=True
        │                     ▼
        │            ┌─────────────────┐
        │            │ 持仓中·待挂卖单  │
        │            │ HOLDING_NO_SELL │
        │            └────────┬────────┘
        │                     │ 挂保护 SELL 成功
        │                     │ _has_open_sell_order=True
        │                     ▼
        │            ┌─────────────────┐
        │            │ 持仓中·已挂卖单  │
        │            │HOLDING_WITH_SELL│
        │            └────────┬────────┘
        │                     │ SELL fill 检测到
        │                     │ size 减到 ≤0 → _closed=True
        │                     ▼
        │            ┌─────────────────┐
        │            │  已平仓          │
        └────────────│  CLOSED         │ (终态, 不再循环)
                     └─────────────────┘
```

### 5.5 `_cycle_once` 在各状态下的分支行为

| 当前状态 | cycle 内主要动作 |
|---|---|
| 待入场 | `_has_matching_open_order` 检查 → `should_enter` → `enter_position`（持锁）→ 记录 order_id + `_entry_attempted=True` |
| 已挂买单 | `_detect_fills` 检测 BUY 成交 → `_apply_fill` 更新 _position → 若 has_position 则进入持仓分支 |
| 持仓中·待挂卖单 | `_reconcile_position` 对账 → `should_exit` 检查 → 挂保护 SELL（持锁）→ `_has_open_sell_order=True` |
| 持仓中·已挂卖单 | `_detect_fills` 检测 SELL 成交 → `_apply_fill` 减少 _position → 若 size≤0 则 `_closed=True` |
| 已平仓 | 不再进入 cycle（`run()` 的 `while not self._closed` 退出） |

---

## 6. 并发模型

### 6.1 asyncio 事件循环 + to_thread

- bot 主循环跑在 asyncio 事件循环中。
- 同步 SDK 调用（`py_clob_client_v2` 的 `get_order_book` / `create_and_post_order` 等）用 `asyncio.to_thread` 包装，避免阻塞事件循环。
- 每个 `StrategyRuntime.run()` 是一个独立的 `asyncio.Task`，由 `RuntimeManager.start_all()` 创建（runtime.py:732，`name=f"strategy:{rt.label}"`）。

### 6.2 全局 `asyncio.Lock _order_lock`

- 全进程**一把**锁，所有 runtime 共享（runtime.py:705）。
- 下单操作（`enter_position` / `exit_position` / emergency SELL）在 `async with self._order_lock:` 块内执行（runtime.py:190, 239, 285）。
- **理由**：CLOB SDK 签名竞争 + rate limit；串行化下单避免 nonce 冲突与 429。
- bridge 的写接口也需 `async with _order_lock`（bridge 与 bot 同进程，共享同一把锁）。

### 6.3 每 runtime 独立停止机制

- 每个 `StrategyRuntime` 持有独立的 `asyncio.Event _stop`（runtime.py:75）。
- `run()` 的循环条件：`while not self._stop.is_set() and not self._closed`（runtime.py:96）。
- 可中断睡眠：`await asyncio.wait_for(self._stop.wait(), timeout=interval)` —— 收到停止信号立即退出，否则等到下个 cycle。

### 6.4 `RuntimeManager._status_loop`

- 独立 `asyncio.Task`（runtime.py:738，`name="status-push"`）。
- 每 `poll_interval * status_every_cycles` 秒推一次 `notify_status`（聚合所有 runtime.snapshot()）。
- `stop_all()` 时取消该 task（runtime.py:748-753）。

### 6.5 bridge 与 bot 共进程

- FastAPI 跑在**同一个 asyncio 事件循环**中（用 `uvicorn.Server.serve()` 嵌入主循环，或 `asyncio.create_task`）。
- bridge 持有 `RuntimeManager` 单例引用，直接调其方法，**无 IPC / 无文件 / 无队列**。
- bridge 的写接口需 `async with RuntimeManager._order_lock`，与 bot 的下单逻辑互斥。

### 6.6 心跳日志字段（runtime.py:656-676）

每 `status_every_cycles` 个 cycle 输出一次到 stdout（不发 Telegram）：

```
[label] cycle={N} {state} | entry={entry} exit={exit} tp={tp%|off} sl={sl%|off} size={size}
       | 持仓={size} @ {avg_price:.4f} tp_trigger={...} sl_trigger={...}
```

持仓时追加 `持仓={size} @ {avg_price}` + `tp_trigger` + `sl_trigger`（由 `strategy.tp_trigger_price` / `sl_trigger_price` 计算）。

---

## 7. 成交检测与对账机制（双源 + 三层匹配）

### 7.1 第一层：CLOB trades 精确匹配（`_detect_fills`）

- 调 `trading.get_recent_trades(client)` 获取最近成交。
- V2 Trade 对象无顶层 `order_id`，从 `taker_order_id` 和 `maker_orders[].order_id` 提取。
- 精确匹配 `_entry_order_ids` / `_exit_order_ids` 集合，识别 bot-managed 成交。
- 用 `_seen_trade_ids` 去重，避免重复应用。
- `_first_fill_check_done` 独立标志修复 first_run bug（首次运行不静默应用历史成交）。

### 7.2 第二层：启发式回退（`_apply_fill` + `_trade_matches_intent`）

当 order_id 精确匹配失败时，用 `_trade_matches_intent` 启发式匹配：
- 比对 token + side + price + size/amount 三元近似
- 匹配则视为 bot-managed fill，更新 _position

### 7.3 第三层：portfolio API 对账（`_reconcile_position`）

- 调 `https://data-api.polymarket.com/positions?user={funder}`（runtime.py:526-563）。
- **CLOB V2 SDK 无 `get_balances` 方法**，必须用 data-api。
- baseline 减法：`managed_size = portfolio_size - _entry_position_baseline.size`，避免误接管手动仓位。
- 差异时向上/向下修正 `_position`（size / avg_price）。
- 若发现 portfolio 有仓但 bot 无记录，且 `_entry_attempted=True`，触发保护 SELL（补救漏检的 BUY fill）。
- 若发现 portfolio 归零但 bot 记录有仓，置 `_closed=True`（外部平仓）。

### 7.4 诊断日志（project_memory Engineering Conventions）

- fill 检测记录 total/new/skipped trade 数。
- token mismatch、order_id 格式问题、启发式回退尝试均打印诊断日志。
- 仓位对账记录 detected managed positions 与是否触发保护 SELL。

---

## 8. 安全约束清单

合并 [docs/polymarket-knowledge.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/docs/polymarket-knowledge.md) + project_memory.md，共 18 条：

| # | 硬约束 | 代码实现位置 | 状态 |
|---|---|---|---|
| 1 | EXIT_PRICE 必填，否则禁止入场；持仓缺 EXIT_PRICE 时报告 CRITICAL | runtime.py:156-158（entry blocked）、205-207（CRITICAL） | ✅ |
| 2 | 防重复下单：下单前 `_has_matching_open_order`（token+side+price+size 四元匹配） | runtime.py:161-169, 209-217 | ✅ |
| 3 | 不基于仓位差自动卖：保护 SELL 绑定 bot BUY order ID；不可靠时启发式回退 | `_extract_bot_fills` runtime.py:341-375；`_apply_fill` 启发式 runtime.py:430-438 | ✅ |
| 4 | 待结算市场（ask=None 且 bid≈0.999）跳过入场 | strategy.py:58-59（`has_book` False 或 best_ask None 时 should_enter 返回 False） | ✅ |
| 5 | 已结算市场移除并算 PNL | **runtime.py 未找到对应实现** | ❌ 未实现（见已知限制） |
| 6 | 启动时取消价格不匹配的 BUY 单 | **main.py 启动流程未做** | ❌ 未实现（见已知限制） |
| 7 | 跟踪已尝试入场 token，防重复下单 | `_entry_attempted` bool runtime.py:62, 171-172, 187 | ✅ |
| 8 | managed order vs manual order 区分（managed 由 order ID 跟踪，manual 不托管） | `_entry_order_ids`/`_exit_order_ids` runtime.py:60-61 + `_remember_order_id` | ✅ |
| 9 | TRADING_ENABLED 在市场开始/结束后置 false | 运行时检查 `_config.trading_enabled` runtime.py:122-123 | ✅ |
| 10 | 策略配置纯内存，每次启动清空；新 URL 清空已有列表 | config.py docstring + market_setup.py:61-63（`strategies.clear()`） | ✅ |
| 11 | CONDITIONAL_ENTRY 由 .env 独占控制，config.json 不覆盖 | config.py:122-125, 153 | ✅ |
| 12 | 仓位查询走 data-api（`positions?user={funder}`），CLOB V2 SDK 无 get_balances | runtime.py:31, 526-563 | ✅ |
| 13 | 双边下注 = 两个独立 StrategyRuntime（不共享状态、互不阻塞） | multi-strategy spec + market_setup.py:133-147 双边分支 | ✅ |
| 14 | 同 token 分层买入需分层保护：记录 baseline，只管理新增部分 | `_remember_baseline` runtime.py:565-580 + `_reconcile_position` baseline 减法 runtime.py:494-508 | ✅ |
| 15 | Fill 检测双源：CLOB trades + portfolio positions 对账 | `_detect_fills` + `_reconcile_position` 每 cycle 都跑 | ✅ |
| 16 | 网络/API 不确定时暂停，不补单 | `_has_matching_open_order` 返回 None 时 return 等下个 cycle runtime.py:164-165, 215-217 | ✅ |
| 17 | 心跳日志包含完整策略标签、cycle、状态、entry/exit、tp/sl、size、持仓时 tp/sl 触发价 | `_heartbeat` runtime.py:656-676 | ✅ |
| 18 | fill 检测记录 total/new/skipped trade 数；token mismatch / order_id 格式 / 启发式回退诊断日志 | `_detect_fills` + `_apply_fill` 多处 print runtime.py:325-339, 404-407, 424-427, 432-436 | ✅ |

### 8.1 bridge 不越权约束（新增）

| # | 约束 | 说明 |
|---|---|---|
| 19 | bridge 只发指令，是否下单/撤单由 bot 决定 | `_has_matching_open_order` / `TRADING_ENABLED` / `should_enter` / `should_exit` 等检查不绕过；若 `trading_enabled=false`，bridge 下单请求返回 `409 TRADING_DISABLED` |
| 20 | bridge 不持有业务状态 | 仅转发，所有数据实时读 RuntimeManager；不缓存策略/仓位 |

---

## 9. 配置契约

### 9.1 Config 字段表（config.py:107-136）

| 字段 | env 来源 | 默认值 | 含义 |
|---|---|---|---|
| `private_key` | `PRIVATE_KEY` | "" | 钱包私钥（必填） |
| `host` | `POLY_HOST` | `https://clob.polymarket.com` | CLOB 端点 |
| `chain_id` | `CHAIN_ID` | 137 | Polygon mainnet |
| `signature_type` | `SIGNATURE_TYPE` | 0 | 0=EOA, 1=email/Magic, 2=browser-wallet proxy, 3=POLY_1271 deposit |
| `funder` | `FUNDER` | "" | Funder 地址（signature_type 1/2/3 必填） |
| `clob_api_key/secret/passphrase` | `CLOB_API_KEY/SECRET/PASSPHRASE` | "" | 显式 L2 凭据（可选，否则自动派生） |
| `trading_enabled` | `TRADING_ENABLED` | True | 全局交易开关 |
| `poll_interval` | `POLL_INTERVAL` | 30 | cycle 间隔（秒） |
| `status_every_cycles` | `STATUS_EVERY_CYCLES` | 20 | 每 N cycle 推一次状态心跳 |
| `conditional_entry` | `CONDITIONAL_ENTRY` | True | **.env 独占控制**，config.json 不覆盖；True=等 best_ask≤entry_price 才下 BUY，False=立即下 GTC BUY |
| `default_entry_price` | `ENTRY_PRICE` | 0.50 | CLI 默认买入价 |
| `default_exit_price` | `EXIT_PRICE` | 0.55 | CLI 默认卖出价 |
| `default_share_amount` | `SHARE_AMOUNT` | 10.0 | CLI 默认份数 |
| `default_take_profit_pct` | `TAKE_PROFIT_PCT` | None | CLI 默认 TP%（None=关闭） |
| `default_stop_loss_pct` | `STOP_LOSS_PCT` | None | CLI 默认 SL%（None=关闭） |
| `telegram_bots` | `TELEGRAM_BOTS` | [] | JSON 数组 `[{token,chat_id,enabled}]` |
| `webhooks` | `WEBHOOKS` | [] | JSON 数组 `[{url,enabled}]` |

### 9.2 StrategyConfig 字段（纯内存）

| 字段 | 类型 | 含义 |
|---|---|---|
| `token_id` | str | CLOB outcome token id |
| `label` | str | 显示标签（`{event_title} [{outcome}]`） |
| `entry_price` | float | 买入限价 (0,1) |
| `exit_price` | float | 卖出限价 (0,1)，**强制必填** |
| `share_amount` | float | 下单份数 |
| `take_profit_pct` | float \| None | 止盈比例（如 0.10=10%），None=关闭 |
| `stop_loss_pct` | float \| None | 止损比例（如 0.05=5%），None=关闭 |
| `enabled` | bool | 策略启用开关 |

> Per-strategy 参数**纯内存**，不持久化到磁盘（config.py docstring）。

### 9.3 CONDITIONAL_ENTRY 的 .env 专属约束

- `conditional_entry` 由 .env **独占控制**（config.py:122-125）。
- config.json（历史遗留，实际代码已无加载逻辑）**不覆盖**此字段。
- True：等 `best_ask ≤ entry_price` 才下 BUY（条件入场）。
- False：立即下 GTC BUY 限价单（无条件入场）。

### 9.4 纯内存模式

- 策略列表每次启动由 `run_interactive_setup` 填充，进程重启即丢失。
- 输入新 URL 时 `strategies.clear()` 清空已有列表（market_setup.py:61-63）。
- 仓位/订单 ID 同样纯内存，重启即丢（见已知限制）。

### 9.5 份数计算公式

```
SHARE_AMOUNT = target_usdc / ENTRY_PRICE
```

例：目标投入 50 USDC，ENTRY_PRICE=0.42 → SHARE_AMOUNT ≈ 119 份。

---

## 10. 通知系统设计

### 10.1 NotifierManager 事件分类

| 事件 | 触发时机 | 格式化内容 |
|---|---|---|
| `fill` | 单 fill 检测到 | side/size/price/order_id/time |
| `status` | 周期性（`_status_loop`） | 聚合所有 runtime.snapshot() |
| `started` | `start_all` 时 | 监控的市场列表 |
| `stopped` | `stop_all` 时 | 停止摘要 |
| `config_change` | 配置变更 | 变更字段 |

### 10.2 Telegram 多 bot 推送

- `TelegramNotifier` 直接 HTTP POST 到 `https://api.telegram.org/bot{token}/sendMessage`，`parse_mode=HTML`，`disable_web_page_preview=True`。
- 无 SDK 依赖。
- 支持多个 bot 同时推送同一消息（`TELEGRAM_BOTS` JSON 数组）。

### 10.3 Webhook 多端点

- `WebhookNotifier` POST JSON `{"event","text","timestamp","unix_ts"}`。
- best-effort 非阻塞，单端点失败不影响其他。

### 10.4 现状声明：交互式 Telegram bot 已移除

- [README.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/README.md) 仍提及 `bot/telegram_bot.py` / `TELEGRAM_INTERACTIVE_TOKEN` / `/status` `/market` 等命令，但**项目根无 bot/ 文件夹**，[main.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/main.py) 也未 import 任何交互式 bot 模块。
- 当前 `notifications/` 目录只有推送模块（manager / telegram_notifier / webhook_notifier）。
- 这是一处文档与代码不一致（见已知限制），本架构文档如实记录现状。

---

## 11. FastAPI Bridge 设计（API-only，无 WS）

### 11.1 定位

- bridge 与 bot **同进程**，持有 `RuntimeManager` 单例引用。
- 跑在同一 asyncio 事件循环中，直接调 RuntimeManager 方法，**无 IPC / 无文件 / 无队列**。
- 仅暴露 HTTP 接口，**不提供 WebSocket / SSE**。
- 不持有业务状态，所有数据实时读 RuntimeManager。
- bot 自身可独立运行，bridge 是可选横切层。

### 11.2 REST 接口列表

| Method | Path | 请求 | 响应 | 说明 |
|---|---|---|---|---|
| GET | `/api/health` | — | `{status, bot_running, strategies_count, latency_ms, funder}` | 健康检查 |
| GET | `/api/config` | — | `GlobalConfig` | 全局默认配置 |
| GET | `/api/url/parse` | `?url=<polymarket_url>` 或 `?slug=<slug>` | `{slug, event_title, markets: [MarketInfo]}` | 解析 URL 或 slug（支持手动输入） |
| GET | `/api/markets/{slug}` | — | `MarketInfo` | 市场详情 |
| GET | `/api/positions` | — | `{positions: [StrategySnapshot]}` | 所有策略快照 |
| GET | `/api/orders` | — | `{orders: [OrderInfo]}` | 所有挂单 |
| GET | `/api/status` | — | `StatusAggregate` | 聚合状态（单数/总额/PNL） |
| POST | `/api/order` | `OrderRequest` | `{position_id, order_id, state}` | 下单（构造 StrategyConfig 注入 RuntimeManager） |
| POST | `/api/order/cancel` | `{order_id? \| token_id+side}` | `{cancelled: [order_id]}` | 撤单 |
| POST | `/api/position/close` | `{token_id, mode}` | `{order_id, exit_price, size}` | 平仓（撤卖单+按 best_bid 市价卖） |
| POST | `/api/position/stop` | `{token_id}` | `{cancelled_orders, state}` | 停止策略 |
| POST | `/api/position/delete` | `{token_id}` | `{removed: true}` | 删除策略记录 |
| POST | `/api/global/preview` | `{action: stop_all\|close_all\|delete_all}` | `ImpactPreview` | 预览影响（不下发） |
| POST | `/api/global/stop_all` | — | `ImpactPreview` + 执行结果 | 全部停止 |
| POST | `/api/global/close_all` | — | `ImpactPreview` + 执行结果 | 全部平仓 |
| POST | `/api/global/delete_all` | — | `ImpactPreview` + 执行结果 | 全部删除 |

### 11.3 轮询策略（替代 WebSocket 推送）

**设计原则**：扩展端定时调 GET 接口全量同步，bridge 不维护事件队列、不推送。

| 轮询项 | 接口 | 建议频率 | 说明 |
|---|---|---|---|
| 持仓列表 | `GET /api/positions` | 每 2-3s | 全量 StrategySnapshot[]，扩展端 diff 后更新 UI |
| 聚合状态 | `GET /api/status` | 每 2-3s（或更低） | 单数/总额/PNL |
| 挂单列表 | `GET /api/orders` | 每 5s（或按需） | 全量 OrderInfo[] |
| 健康检查 | `GET /api/health` | 每 10s | 连接状态徽章 |

- **首次加载**：扩展端打开侧边栏立即调 `GET /api/positions` 全量同步。
- **不引入服务端事件队列/序号**：全量轮询无需增量机制。
- **频率依据**：bot 默认 `POLL_INTERVAL=30s`，fill 检测每 cycle 一次；扩展端轮询 2-3s 可在 bot cycle 间隔内及时反映变化，同时避免压垮 bridge。
- **性能边界**：策略数 <20 时全量轮询无压力（见已知限制）。

### 11.4 数据模型对齐

bridge 返回的 `StrategySnapshot` 严格对齐 [runtime.py:681-695](file:///d:/workspace/python/polymarket_trading_bot_strategy/runtime.py#L681-L695) 的 `snapshot()` 输出：

```typescript
interface StrategySnapshot {
  label: string;              // "{event_title} [{outcome}]"
  token_id: string;
  state: StrategyState;       // 5 状态枚举
  cycle: number;
  entry_price: number;
  exit_price: number;         // 强制必填
  share_amount: number;
  take_profit_pct: number | null;   // null=关闭
  stop_loss_pct: number | null;     // null=关闭
  position_size: number;
  avg_price: number;
  closed: boolean;
}

type StrategyState =
  | "待入场"
  | "已挂买单（待成交）"
  | "持仓中（待挂卖单）"
  | "持仓中（已挂保护卖单）"
  | "已平仓";
```

其他数据模型（`MarketInfo` / `OrderInfo` / `PositionInfo` / `GlobalConfig` / `ImpactPreview`）详见[浏览器扩展详细设计文档.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/浏览器扩展详细设计文档.md) 第 4 章。

### 11.5 错误码

| HTTP | code | 含义 | 扩展端处理 |
|---|---|---|---|
| 400 | `EXIT_PRICE_REQUIRED` | 缺少 exit_price | 表单标红 + toast |
| 400 | `INVALID_PRICE` | 价格越界 (0,1) | 表单标红 |
| 409 | `DUPLICATE_ORDER` | 已有同意图挂单 | 提示"已有挂单" + 跳转该卡片 |
| 409 | `MARKET_CLOSED` | 市场已关闭/结算中 | 禁用下单按钮 |
| 409 | `INSUFFICIENT_BALANCE` | 余额不足 | 提示 + 禁用下单 |
| 409 | `TRADING_DISABLED` | `trading_enabled=false` | 提示 + 禁用下单 |
| 422 | `TOKEN_NOT_FOUND` | token_id 无效 | 提示重新解析 URL |
| 500 | `BOT_ERROR` | bot 内部异常 | 显示错误 + 建议查看 bot 日志 |
| 503 | `CLOB_UNAVAILABLE` | CLOB 不可达 | 顶部显示离线徽章 |

### 11.6 重试策略

- **GET 请求**：失败时指数退避重试 3 次（200ms / 600ms / 1.8s）。
- **POST 请求**：不自动重试，由用户决定。

### 11.7 降级

- bridge 不可达时，扩展端进入**只读模式**：仍可查看上次缓存的持仓，但所有写按钮禁用。
- 顶部显示红色徽章"已断开 · [重试]"。
- 恢复后立即 `GET /api/positions` 全量同步。

### 11.8 不实现的内容

- ❌ 不提供 WebSocket
- ❌ 不提供 SSE（Server-Sent Events）
- ❌ 不提供服务端事件缓冲队列
- ❌ 不提供增量事件序号（全量轮询无需）

---

## 12. 工具脚本与主程序的关系

### 12.1 `tools/layered_order_runner.py`

- **独立 CLI 脚本**，实现"分层 BUY + 成交后分层 SELL 保护"。
- 命令行参数：`--market-slug` `--token-id` `--label` `--exit-price` `--layer ENTRY_PRICE:USDC_NOTIONAL`（可多次）`--state-file` `--poll-interval` `--place-only` `--monitor-only` `--skip-market-check`。
- **共享** [config.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/config.py)（`Config.load()`）和 [trading.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/trading.py)（`init_client` 等同步函数）。
- **不经** `RuntimeManager` / `StrategyRuntime` / `NotifierManager` —— 单进程单策略独立运行器。
- 有自己的 state file 持久化（`--state-file`，JSON），与主程序的"纯内存"策略相反。

### 12.2 `_ensure_exit_coverage` 的 managed_size 计算

- 已修复手动仓位误卖事故（见 [docs/polymarket-knowledge.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/docs/polymarket-knowledge.md) "手动买入被自动卖出事故"）。
- 现状：`managed_size` 只根据已追踪的 BUY order ID 计算，不再用 `position_size - baseline_position_size`。
- 使用方式详见 [docs/SELF_MANAGEMENT_GUIDE.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/docs/SELF_MANAGEMENT_GUIDE.md) 第 6 节。

---

## 13. 信号处理与优雅停止

### 13.1 Windows 兼容信号处理

- 优先用 `loop.add_signal_handler(SIGINT, ...)` / `loop.add_signal_handler(SIGTERM, ...)`。
- Windows 不支持 `loop.add_signal_handler`，回退用 `signal.signal(SIGINT, handler)` + `loop.call_soon_threadsafe` 把回调 marshal 到事件循环线程。
- 信号触发后调 `RuntimeManager.stop_all()`。

### 13.2 停止语义

- `stop_all()`：`_stop.set()` → 每个 `rt.stop()` → `await asyncio.gather(*tasks, return_exceptions=True)` → 取消 `_status_task`（runtime.py:742-753）。
- **停止不取消交易所挂单**：bot 退出后，CLOB 上的 GTC 挂单仍然存活。这是刻意设计——避免停止时意外撤掉保护卖单导致持仓裸露。
- 若需撤单，用户应显式调 `trading.cancel_open_orders_for_token` 或通过 bridge `POST /api/position/stop`。

### 13.3 bridge 与 bot 的停止协同

- bot 收到 SIGINT 后：先停 `RuntimeManager`（`stop_all`），再停 bridge（`uvicorn.Server.shutdown()`，若 bridge 启用）。
- bridge 停止后，扩展端 GET 请求失败，进入只读模式（见 11.7）。
- 重启 bot 后，扩展端首次 GET 恢复，全量同步。

---

## 14. 已知限制与文档不一致

### 14.1 持久化缺失

- 仓位 `_position`、订单 ID 集合 `_entry_order_ids`/`_exit_order_ids`、已尝试入场标志 `_entry_attempted`、seen trade IDs `_seen_trade_ids`、baseline 等全部纯内存。
- 进程重启即丢失，可能导致：重复下 BUY、漏检成交、误接管手动仓位。
- 缓解：启动时 `_reconcile_position` 用 portfolio API 对账修正 `_position`；但 order ID 集合无法恢复，启发式回退是唯一兜底。
- 详见 `.trae/documents/持久化改造方案.md`（已有方案但未实现）。

### 14.2 无交互式 Telegram bot

- [README.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/README.md) 仍提及 `bot/telegram_bot.py` / `TELEGRAM_INTERACTIVE_TOKEN` / `/status` `/market` `/price` 等命令，但**实际已移除**。
- 当前只有推送型 `NotifierManager`，无命令型交互 bot。
- 这是一处文档与代码不一致，本架构文档如实记录现状，未修改 README。

### 14.3 约束 #5、#6 未实现

- **约束 #5（已结算市场移除并算 PNL）**：runtime.py 未找到对应实现。
- **约束 #6（启动时取消价格不匹配的 BUY 单）**：main.py 启动流程未做。
- 这两条在 project_memory.md 中被列为硬约束，但主程序未实现。本架构文档如实记录，未修复代码。

### 14.4 FastAPI bridge 尚未实现

- 本文档第 11 章是**设计契约**，未写实现代码。
- [浏览器扩展详细设计文档.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/浏览器扩展详细设计文档.md) 已同步改为 API-only 全量轮询（无 WS）。
- bridge 实现后，本文档第 11 章可作为开发依据。

### 14.5 全量轮询性能边界

- 全量轮询在策略数 >20 时可能有性能压力（每 2-3s 全量序列化所有 snapshot）。
- 当前场景 <20，可接受。
- 若未来策略数增长，可考虑增量事件轮询（`GET /api/events?since=<seq>`）或分页。

---

## 15. 附录

### 15.1 状态机图（完整版）

见第 5.4 节。

### 15.2 模块依赖图（含 bridge）

```
                    ┌──────────┐
                    │  .env    │
                    └────┬─────┘
                         │
                    ┌────▼─────┐
                    │ config.py│◀──── StrategyConfig (纯内存)
                    └────┬─────┘
                         │
            ┌────────────┼────────────┐
            │            │            │
       ┌────▼────┐  ┌────▼─────┐  ┌───▼────────┐
       │ main.py │  │trading.py│  │market_setup│
       └────┬────┘  └────┬─────┘  └───┬────────┘
            │            │            │
            │       ┌────▼─────┐      │
            │       │ClobClient│◀─────┘
            │       └────┬─────┘
            │            │
       ┌────▼────────────▼────┐
       │   RuntimeManager     │
       │  ┌─────────────────┐ │
       │  │ StrategyRuntime │ │ ◀─── asyncio.Lock _order_lock (全局共享)
       │  │  × N            │ │
       │  └─────────────────┘ │
       └────┬─────────────────┘
            │
       ┌────▼─────┐
       │Notifier  │──▶ TelegramNotifier × N
       │Manager   │──▶ WebhookNotifier × N
       └────┬─────┘
            │
       ┌────▼─────────────┐
       │ FastAPI Bridge   │ (可选, 同进程)
       │  HTTP /api/*     │
       └────┬─────────────┘
            │ HTTP (全量轮询)
            ▼
       Chrome 扩展
```

### 15.3 关键文件路径清单

| 文件 | 职责 |
|---|---|
| [main.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/main.py) | asyncio 入口 |
| [runtime.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/runtime.py) | StrategyRuntime + RuntimeManager |
| [trading.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/trading.py) | CLOB V2 SDK 包装 |
| [strategy.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/strategy.py) | 决策纯函数 |
| [config.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/config.py) | Config + StrategyConfig |
| [market_setup.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/market_setup.py) | 交互式 CLI 配置 |
| [notifications/manager.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/notifications/manager.py) | NotifierManager |
| [notifications/telegram_notifier.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/notifications/telegram_notifier.py) | Telegram 推送 |
| [notifications/webhook_notifier.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/notifications/webhook_notifier.py) | Webhook 推送 |
| [tools/layered_order_runner.py](file:///d:/workspace/python/polymarket_trading_bot_strategy/tools/layered_order_runner.py) | 独立分层下单脚本 |
| [docs/polymarket-knowledge.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/docs/polymarket-knowledge.md) | 安全约束知识库 |
| [docs/PRODUCT_V1_BASELINE.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/docs/PRODUCT_V1_BASELINE.md) | V1 基线（pre-asyncio，已过时） |
| [docs/USER_GUIDE_V1.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/docs/USER_GUIDE_V1.md) | V1 用户指南 |
| [docs/SELF_MANAGEMENT_GUIDE.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/docs/SELF_MANAGEMENT_GUIDE.md) | 自助管理指南 |
| [浏览器扩展详细设计文档.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/浏览器扩展详细设计文档.md) | Chrome 扩展设计（API-only） |
| [.trae/specs/multi-strategy-asyncio-architecture/spec.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/.trae/specs/multi-strategy-asyncio-architecture/spec.md) | 多策略 asyncio 重构 spec（已落地） |
| [.trae/documents/持久化改造方案.md](file:///d:/workspace/python/polymarket_trading_bot_strategy/.trae/documents/持久化改造方案.md) | 持久化改造方案（未实现） |

---

> 文档版本：v1.0
> 最后更新：2026-07-24
> 基于：当前 runtime.py / trading.py / config.py / market_setup.py / main.py 实际代码
