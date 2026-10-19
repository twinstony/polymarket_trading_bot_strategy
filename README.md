# Polymarket Trading Bot — Automated CLOB Strategy (Python)

Python 编写的 Polymarket CLOB 自动交易机器人，配备 **FastAPI Bridge** 与 **Chrome 侧边栏扩展**，支持 CLI 交互式配置、Telegram 命令 bot、多策略并发运行、可视化操作面板。

> ⚠️ 仅供学习研究。预测市场交易存在重大风险，任何财务损失由用户自行承担。请先用小额测试再放大规模。

---

## 整体架构

```
┌───────────────────────┐        ┌──────────────────────┐        ┌──────────────────────┐
│  Chrome Side Panel    │  HTTP  │  FastAPI Bridge      │ in-proc│  Python Bot Core     │
│  (扩展前端)            │ ─────► │  (bridge.py)         │ ──────►│  runtime / trading   │
│                       │ ◄───── │  /api/*              │ ◄──────│  strategy / config   │
│  - 状态区             │ (轮询) │                      │        │  market_setup        │
│  - 下单区             │ 2.5s   │  16 个 REST 接口     │        │                      │
│  - 持仓区             │        │  无 WS/SSE           │        │  ┌────────────────┐  │
│  - 全局指令           │        │  9 个错误码          │        │  │  Polymarket    │  │
└───────────┬───────────┘        └──────────┬───────────┘        │  │  CLOB / Gamma  │  │
            │                               │                    │  │  / data-api    │  │
            │ chrome.tabs.onUpdated         │ RuntimeManager     │  └────────────────┘  │
            ▼                               ▼ singleton           └──────────────────────┘
┌───────────────────────┐        ┌──────────────────────┐
│  Polymarket Web Page  │        │  RuntimeManager      │
│  (用户浏览的页面)      │        │  (单例，进程内共享)  │
└───────────────────────┘        └──────────────────────┘
```

**关键约束**：
- 扩展不直连 Polymarket，所有读写经 FastAPI Bridge，复用 bot 的 CLOB 客户端与安全逻辑
- 策略配置纯内存，扩展端不写 localStorage / indexedDB，刷新即重读 Bridge
- Bridge 与 bot 同进程，持有 `RuntimeManager` 单例引用
- 强制 EXIT_PRICE：下单接口校验 `exit_price` 必填，否则 400
- 防重复下单：bot 端 `_has_matching_open_order` 检查，扩展不绕过

---

## 项目结构

```
polymarket_trading_bot_strategy/
├── main.py                      # 入口：交互式配置 + RuntimeManager + Bridge 启停
├── runtime.py                   # StrategyRuntime / RuntimeManager，5 状态机 + 心跳 + 快照
├── trading.py                   # CLOB 客户端、下单、撤单、盘口查询
├── strategy.py                  # should_enter / should_exit，TP/SL 触发价
├── config.py                    # Config / StrategyConfig，env + bridge 字段
├── market_setup.py              # URL → slug → Gamma API → markets/outcomes/token_ids
├── bridge.py                    # FastAPI Bridge：16 个 REST 接口 + 9 个错误码
├── telegram_bot.py              # Telegram 命令 bot：asyncio 长轮询 + 16 个命令
├── notifications/
│   ├── manager.py               # NotifierManager + message formatters
│   ├── telegram_notifier.py     # multi-bot push delivery
│   └── webhook_notifier.py      # multi-endpoint webhook delivery
├── extension/                   # Chrome 侧边栏扩展（Manifest V3）
│   ├── manifest.json            # V3 配置：sidePanel + tabs + host_permissions
│   ├── background.js            # service worker：tab 监听 + 点图标开关侧边栏
│   ├── sidepanel.html           # 侧边栏主页面
│   ├── sidepanel.css            # 暗色主题样式（420px 宽）
│   ├── sidepanel.js             # 主逻辑：state + render + bindEvents + 快捷键
│   ├── api.js                   # HTTP 客户端 + 2.5s 轮询 + 指数退避 + 只读降级
│   ├── popup.html / popup.js    # 旧 popup（已弃用，manifest 不再引用）
│   └── components/
│       ├── StatusArea.js        # 状态区：在线/策略数/PNL/周期
│       ├── OrderArea.js         # 下单区：market 分组 + outcome 切换 + 表单
│       ├── PositionCard.js      # 持仓卡片：展开/收起 + 操作按钮
│       └── GlobalCommandBar.js  # 全局指令 + ImpactConfirmModal 二次确认
├── docs/                        # 设计文档与知识库
├── requirements.txt
├── .env.example
└── README.md
```

---

## 安装

需要 **Python 3.9+** 与 **Chrome 114+**（侧边栏扩展）。

```bash
cd polymarket_trading_bot_strategy
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

需要 Polygon 兼容钱包（如 MetaMask）持有 **MATIC**（gas）与 **Polygon 上的 USDC**（本金），并已连接到 Polymarket 账户。EOA 钱包还需一次性设置 USDC / Conditional-Token 授权（见 Polymarket 文档）。

---

## 配置

1. 复制 `.env.example` 到 `.env` 并填入你的值
2. 至少设置 `PRIVATE_KEY` 与 `FUNDER`

### 核心变量

| Variable                      | Description                                                       |
| ----------------------------- | ----------------------------------------------------------------- |
| `PRIVATE_KEY`                 | 钱包私钥（交易必填）                                              |
| `POLY_HOST`                   | CLOB 端点（默认 `https://clob.polymarket.com`）                   |
| `CHAIN_ID`                    | Polygon chain id（默认 `137`）                                    |
| `SIGNATURE_TYPE`              | `0` EOA（默认），`1` email/Magic，`2` 浏览器钱包代理              |
| `FUNDER`                      | Funder 地址（签名类型 1/2 必填，也用于 data-api 查持仓）          |
| `SHARE_AMOUNT`                | 默认下单份额                                                      |
| `ENTRY_PRICE` / `EXIT_PRICE`  | 默认限价（0.00 – 1.00），EXIT_PRICE 必填                          |
| `TAKE_PROFIT_PCT` / `STOP_LOSS_PCT` | 止盈止损比例（如 `0.10` = 10%，留空关闭）                  |
| `POLL_INTERVAL`               | cycle 间隔秒数（默认 `30`）                                       |
| `CONDITIONAL_ENTRY`           | 仅 .env 控制，config.json 不覆盖                                  |
| `TELEGRAM_BOTS`               | JSON 数组 `[{"token","chat_id","enabled"}]` 推送 bot              |
| `TELEGRAM_INTERACTIVE_TOKEN`  | 命令 bot token（长轮询 getUpdates，执行交易命令）                 |
| `TELEGRAM_ALLOWED_USER_IDS`   | 命令白名单（逗号分隔，留空=允许任何人，不推荐）                   |
| `WEBHOOKS`                    | JSON 数组 `[{"url","enabled"}]` webhook 端点                      |

### Bridge 配置（新增）

| Variable          | Default       | Description                                          |
| ----------------- | ------------- | ---------------------------------------------------- |
| `BRIDGE_ENABLED`  | `false`       | 是否启动 FastAPI Bridge（启用扩展必填）              |
| `BRIDGE_HOST`     | `127.0.0.1`   | 监听地址（默认仅本机，**勿改 0.0.0.0 暴露公网**）    |
| `BRIDGE_PORT`     | `8787`        | 监听端口                                             |

> Bridge 默认仅监听 127.0.0.1，扩展与 bot 必须在同一台机器。如需远程访问请自行加反向代理 + 鉴权。

---

## 启动

### 1. 仅 CLI 模式（不带扩展）

```bash
python main.py
```

按提示输入 Polymarket 市场 URL 或 slug，交互式配置策略参数后开始交易。

### 2. Bridge + 扩展模式

`.env` 设 `BRIDGE_ENABLED=true`，然后：

```bash
python main.py
```

启动后看到提示输入市场 URL 或 slug：
- 输入 URL/slug → 正常配置策略后启动 bridge
- 输入 `b` → **跳过市场配置，以空策略启动 bridge**（仅供扩展连接测试）
- 输入 `q` → 退出

启动成功后日志会打印 `[bridge] serving on http://127.0.0.1:8787`。

### 3. 加载 Chrome 扩展

1. 打开 `chrome://extensions`
2. 开启右上角「开发者模式」
3. 点「加载已解压的扩展程序」→ 选择项目下的 `extension/` 目录
4. 点工具栏扩展图标 → 侧边栏自动从右侧滑出（再次点击关闭）
5. 侧边栏顶栏右侧点齿轮 ⚙ → 输入 Bridge 地址（默认 `http://127.0.0.1:8787`）→ 保存 / 测试连接

---

## Chrome 侧边栏使用

### 状态区（顶部）
显示在线状态、策略数、PNL、周期，每 2.5s 自动刷新。下方是全局指令栏（停止全部 / 平仓全部 / 删除全部，需二次确认 + 影响汇总）。

### 下单区（中间）
- **默认态**：打开 `polymarket.com/event/<slug>` 页面，侧边栏自动解析当前 URL，按 market 分组列出（顺序与命令行一致，Game 1 / Game 2 / BO3 天然在前）
- **手动输入**：点 URL 栏右侧 `⇄` 切换到输入态，手动输入 slug 或完整 URL → 点"解析"
- **解析中**：显示骨架屏 + "⏳ 正在解析市场…"
- 点 market → 展开 Layer 2 价格区：
  - 顶部 outcome 切换器（如 Team WE / LGD Gaming），切 outcome 自动预填该方向 ask 价
  - 填买入价 / 卖出价 / 数量 / 止盈% / 止损% → 点"下单"或按 **Ctrl+Enter**（macOS **Cmd+Enter**）
  - 不想下单按 **Esc** 或点 Layer 2 右上角 ✕ 收起价格区

### 持仓区（下方）
- 每张卡片可展开/收起，收起态显示一行摘要（ENTRY/EXIT/SIZE/AVG/PNL）
- 展开态有操作按钮（按状态启用/禁用）：

| 按钮 | BUY_PENDING（已挂买单） | HOLDING_WITH_SELL（持仓+已挂卖单） | WAITING_ENTRY（待入场） |
|---|---|---|---|
| 撤单 / 撤卖单 / 停止 | **撤单**：撤买单 + 删除策略 | **撤卖单**：撤卖单但保留持仓 | **停止**：禁用策略监控 |
| 平仓 | 禁用 | 平仓（按 best_bid 市价卖） | 禁用 |
| 重新开单 | 禁用 | 禁用 | 把参数注入下单区等确认 |
| 删除 | 删除策略记录 | 删除策略记录（不撤单不平仓） | 删除策略记录 |

**操作语义**：
- **撤单（BUY_PENDING）** = 撤买单 + 删除策略（未成交买单撤掉后策略无意义）
- **停止** = 撤所有挂单 + 禁用策略监控（`trading_enabled=false`）+ 保留记录看 PNL
- **平仓** = 撤卖单 + 按 best_bid 市价卖
- **删除** = 纯记录操作，不撤单不平仓

### 全局指令栏
三个批量按钮：停止全部 / 平仓全部 / 删除全部。点任意一个 → 弹出 ImpactConfirmModal 显示影响汇总（影响持仓数、预估回收 USDC、滑点风险）→ 确认后执行。

### 快捷键
- **Ctrl+Enter**（Win/Linux）/ **Cmd+Enter**（macOS）：下单
- **Esc**：收起价格区（焦点不在手动 slug 输入框时）

---

## 状态机与生命周期

每个 `StrategyRuntime` 有 5 个状态：

```
WAITING_ENTRY ──(ask<=ENTRY_PRICE 且无同意图挂单)──► BUY_PENDING
BUY_PENDING ──(买单成交)──► HOLDING_WITH_SELL（自动挂保护卖单）
HOLDING_WITH_SELL ──(卖单成交 / TP / SL / 触达 EXIT_PRICE)──► CLOSED
任意状态 ──(撤单 / 停止)──► 回到 WAITING_ENTRY 或退出运行循环
```

- **fill 检测**：每 cycle 调 `get_trades`，按 taker_order_id + maker_orders[].order_id 识别 bot 管理的成交，失败时回退启发式匹配（price/size/side）
- **持仓对账**：通过 `data-api.polymarket.com/positions` 用 funder 地址查持仓，发现 bot 漏检的成交时自动补保护卖单
- **心跳日志**：每 N 个 cycle 输出完整策略状态（label、cycle、state、entry/exit、tp/sl、size、持仓时含触发价）

---

## Telegram 命令 bot

通过 `TELEGRAM_INTERACTIVE_TOKEN` 配置的 bot 完成全部交易操作，与 Chrome 扩展是并列的操作入口，共享同一 `RuntimeManager`。

### 配置

```bash
# .env
TELEGRAM_INTERACTIVE_TOKEN=123456:ABC-DEF...     # 命令 bot token（与推送 bot 可相同可不同）
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321    # 白名单（留空=允许任何人，不推荐）
```

> 查自己的 user_id：在 Telegram 给 [@userinfobot](https://t.me/userinfobot) 发消息。

### 命令一览

**查询类**

| Command | Action |
|---|---|
| `/help` | 显示帮助 |
| `/status` | 所有策略快照（label/状态/cycle/entry/exit/size/持仓/TP/SL） |
| `/list` | 列出所有策略的 token_id（点击复制） |
| `/parse <url\|slug>` | 解析市场，返回 market + outcome + token_id 列表 |
| `/markets <url\|slug>` | 同 `/parse` |

**下单与管理**

| Command | Action |
|---|---|
| `/order <token_id> <entry> <exit> <size> [tp] [sl]` | 下单并启动策略（tp/sl 可选，`off` 关闭） |
| `/cancel <token_id>` | 撤该 token 的所有挂单（策略继续运行，下个 cycle 重新评估） |
| `/close <token_id>` | 平仓（撤卖单 + 按 best_bid 市价卖） |
| `/stop <token_id>` | 停止策略（撤单 + 禁用监控，记录保留） |
| `/delete <token_id>` | 删除策略记录（不撤单不平仓） |

**参数修改**（实时改，下个 cycle 生效）

| Command | Action |
|---|---|
| `/price <token_id> <0-1>` | 改买入价 |
| `/exit_price <token_id> <0-1>` | 改卖出价 |
| `/amount <token_id> <n>` | 改下单份额 |
| `/takeprofit <token_id> <pct\|off>` | 改止盈（`0.10` = 10%，`off` 关闭） |
| `/stoploss <token_id> <pct\|off>` | 改止损 |

### 使用示例

```
# 1. 解析市场
/parse https://polymarket.com/event/lpl-s13-finals

# 2. 从返回的 token_id 列表里选一个下单
/order 72123...abc 0.42 0.55 100 0.10 0.05

# 3. 查看状态
/status

# 4. 改买入价
/price 72123...abc 0.40

# 5. 撤单（策略继续，等下个 cycle 重新评估）
/cancel 72123...abc

# 6. 平仓
/close 72123...abc

# 7. 停止策略（保留记录）
/stop 72123...abc

# 8. 彻底删除
/delete 72123...abc
```

### 安全
- `TELEGRAM_ALLOWED_USER_IDS` 白名单控制，未授权用户收到"⛔ 未授权"
- 所有写操作走 `RuntimeManager._order_lock` 串行化，与扩展/CLI 共享同一锁
- 下单前检查 token 重复（同 token 已有策略则拒绝）
- label 自动从 Gamma API 反查 market question + outcome 构造

---

## FastAPI Bridge 接口

16 个 REST 接口（详见 [Python交易机器人架构设计文档.md](Python交易机器人架构设计文档.md)）：

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/config` | 全局默认配置 |
| GET | `/api/url/parse?url=` 或 `?slug=` | 解析 URL 或 slug（并发查 book） |
| GET | `/api/markets/{slug}` | 市场详情 |
| GET | `/api/positions` | 所有策略快照 |
| GET | `/api/orders` | 所有挂单 |
| GET | `/api/status` | 聚合状态（PNL 等） |
| POST | `/api/order` | 下单 |
| POST | `/api/order/cancel` | 撤单（清 buy/sell 状态标志） |
| POST | `/api/position/close` | 平仓（market / limit） |
| POST | `/api/position/stop` | 停止策略（禁用 + 清状态） |
| POST | `/api/position/delete` | 删除策略记录 |
| POST | `/api/global/preview` | 预览全局指令影响 |
| POST | `/api/global/stop_all` | 全部停止 |
| POST | `/api/global/close_all` | 全部平仓 |
| POST | `/api/global/delete_all` | 全部删除 |

**错误码**：`EXIT_PRICE_REQUIRED` / `INVALID_PRICE` / `DUPLICATE_ORDER` / `MARKET_CLOSED` / `INSUFFICIENT_BALANCE` / `TRADING_DISABLED` / `TOKEN_NOT_FOUND` / `BOT_ERROR` / `CLOB_UNAVAILABLE`

**轮询策略**：扩展端每 2.5s 全量拉取 `GET /api/positions` + `GET /api/status`，无 WebSocket/SSE。GET 失败时指数退避重试 3 次（200ms / 600ms / 1.8s），POST 不自动重试。Bridge 不可达时扩展进入只读模式，10s 后自动恢复。

---

## 策略逻辑

- **入场**（`should_enter`）：best ask ≤ `ENTRY_PRICE` 且无同意图挂单时下 BUY 限价单
- **出场**（`should_exit`）：以下任一触发下 SELL 限价单
  - 止盈目标达成（`profit >= TAKE_PROFIT_PCT`）
  - 止损阈值突破（`loss >= STOP_LOSS_PCT`）
  - best bid 触达 `EXIT_PRICE`（固定规则）

编辑 `strategy.py` 可实现自定义逻辑（基于成交量/价差/概率），无需改动其他模块。

---

## 通知

三类通知推送到所有 Telegram bot 与 webhook：

- `open_orders` — 当前挂单，按市场分组
- `fill` — 成交详情（方向、数量、价格、订单号、时间）
- `status` — 周期性运行时摘要（cycle、活跃市场、监控市场、参数、持仓）

Webhook payload：

```json
{
  "event": "fill",
  "text": "✅ Fill: Bought ...",
  "timestamp": "2026-07-17T08:47:09Z",
  "unix_ts": 1752748029
}
```

---

## 常见问题

### 启动时报 `Could not create api key`
`trading.init_client` 阶段 CLOB auth/api-key 端点返回 400。常见原因：funder 地址未在 Polymarket 网页端首次签名创建过 API key，或本地已缓存 API key 再次创建冲突。下一行 `CLOB V2 Client initialised` 表示用已有 key 初始化成功，**不影响后续下单**，可忽略。

### 扩展显示"离线 · Failed to fetch"
Manifest V3 默认禁止跨域 fetch。确认 `extension/manifest.json` 已声明 `host_permissions: ["http://127.0.0.1:*/*", "http://localhost:*/*"]`，并在 `chrome://extensions` 点扩展卡片的**刷新图标**重新加载。

### 撤单后持仓区仍显示
BUY_PENDING 状态下点"撤单"会撤买单 + 删除策略记录，策略应立即从持仓区消失。其他状态点"停止"是禁用监控但保留记录，持仓区会继续显示该策略（状态变为"已停止"）。如需彻底移除请点"删除"。

### 输入价格时焦点丢失
轮询回调已改为只刷新对应区域（持仓区/状态区），不触碰下单区。下单区交互（选 market / 切 outcome / 输入价格）只触发 `renderOrderArea()` 局部渲染，且 render 时会保护焦点（记录 activeElement id + selectionStart/End，render 后恢复）。

### market 列表顺序与命令行不一致
前端按 Gamma API 返回的 `markets` 数组原顺序排列，与命令行 `market_setup.py select_market` 完全一致，不做任何 priority 重排。Game 1 / Game 2 / BO3 的顺序是 API 本身的返回顺序。

### Telegram 命令 bot 与 Chrome 扩展的关系
两者是**并列的操作入口**，共享同一 `RuntimeManager`：
- 命令 bot 通过 `TELEGRAM_INTERACTIVE_TOKEN` 启用，长轮询 `getUpdates`
- 扩展通过 `BRIDGE_ENABLED=true` 启用，HTTP 轮询 bridge
- 两者可同时启用，也可只启用一个
- 所有写操作（下单/撤单/平仓/停止/删除）共享 `RuntimeManager._order_lock`，避免并发冲突
- 通过任一入口下的单都会出现在另一个入口的持仓区

### 撤单与停止的区别
- **撤单（`/cancel`）**：撤掉当前挂单，策略 `_entry_attempted` 清除，下个 cycle 重新评估 `should_enter`，可能重新下单。适合"改价位重新挂"
- **停止（`/stop`）**：撤单 + `trading_enabled=false`，策略不再重新下单，但记录保留看 PNL。适合"这个策略不要了但留历史"
- **删除（`/delete`）**：纯记录操作，从 RuntimeManager 移除策略，不撤单不平仓

---

## 免责声明

本软件仅供学习研究。预测市场交易存在重大风险，任何财务损失由用户自行承担。请先用小额测试再放大规模。
