# Polymarket 脚本自主管理说明

本说明用于你以后自己管理脚本。核心原则很简单：钱包配置基本不动，每次交易只改市场、方向、价格、份数、卖出目标和交易开关。

## 1. 文件位置

项目路径：

```text
/Users/ad/Documents/polydata/polymarket_trading_bot_strategy
```

主要配置文件：

```text
/Users/ad/Documents/polydata/polymarket_trading_bot_strategy/.env
```

知识库：

```text
/Users/ad/Documents/polydata/polymarket_trading_bot_strategy/docs/polymarket-knowledge.md
```

## 2. 不要随便改的内容

这些是钱包/API配置，通常不要改：

```env
PRIVATE_KEY=
SIGNATURE_TYPE=
FUNDER=
CLOB_API_KEY=
CLOB_SECRET=
CLOB_PASSPHRASE=
```

注意：

- `.env` 里面有私钥，不要发给别人。
- 不要上传 `.env` 到 GitHub。
- 如果钱包、Polymarket 账户、API key 没变，这些字段保持不动。

## 3. 每次交易主要改这些

```env
TRADING_ENABLED=
TOKEN_ID=
MARKET_LABEL=
SHARE_AMOUNT=
ENTRY_PRICE=
EXIT_PRICE=
CONDITIONAL_ENTRY=
```

字段解释：

| 字段 | 作用 | 怎么填 |
| --- | --- | --- |
| `TRADING_ENABLED` | 交易总开关 | `true` 允许脚本交易；`false` 禁止自动买卖 |
| `TOKEN_ID` | 具体方向的 outcome token | 必须是你要买的那一边，不是市场链接 |
| `MARKET_LABEL` | 日志显示名 | 写清楚比赛、市场、方向 |
| `SHARE_AMOUNT` | 买入份数 | `投入金额 / 买入价` |
| `ENTRY_PRICE` | 买入限价 | 例如 `0.35` |
| `EXIT_PRICE` | 卖出目标价 | 例如 `0.65` |
| `CONDITIONAL_ENTRY` | 是否等待更好价格 | `false` 启动就挂单；`true` 等盘口达到条件 |

## 4. 份数怎么计算

公式：

```text
SHARE_AMOUNT = 投入 USDC / 买入价格
```

例子：

| 投入 | 买入价 | 份数 |
| --- | --- | --- |
| 50 USDC | 0.25 | 200 |
| 50 USDC | 0.35 | 142.86 |
| 30 USDC | 0.15 | 200 |
| 40 USDC | 0.20 | 200 |

## 5. 单层交易怎么跑

适合一笔买入、一笔卖出目标。

先在 `.env` 填好：

```env
TRADING_ENABLED=true
TOKEN_ID=目标方向的token
MARKET_LABEL=比赛 - 市场 - 方向
SHARE_AMOUNT=份数
ENTRY_PRICE=买入价
EXIT_PRICE=卖出价
CONDITIONAL_ENTRY=false
```

然后终端运行：

```bash
cd /Users/ad/Documents/polydata/polymarket_trading_bot_strategy
source .venv/bin/activate
python main.py
```

停止：

```text
Ctrl + C
```

## 6. 分层交易怎么跑

适合同一个方向分多档买入，比如：

```text
0.35 买 50 USDC，0.65 卖出
0.25 买 50 USDC，0.65 卖出
```

使用分层监控器：

```bash
cd /Users/ad/Documents/polydata/polymarket_trading_bot_strategy
source .venv/bin/activate
python tools/layered_order_runner.py \
  --market-slug 市场slug \
  --token-id 目标方向token \
  --label "日志名称" \
  --exit-price 0.65 \
  --layer 0.35:50 \
  --layer 0.25:50 \
  --state-file .runtime/本次交易.json \
  --poll-interval 20
```

含义：

- `--layer 0.35:50` 表示 0.35 买入 50 USDC。
- `--layer 0.25:50` 表示 0.25 买入 50 USDC。
- 任何一层成交后，监控器会按新增成交份数挂对应 `SELL @ exit-price`。
- 如果同一个 token 之前已经有旧仓位，它会记录基线，只管理本次新增部分。

## 7. 比赛结束后怎么处理

如果小局/比赛已经结束，或者你判断不再适合入场：

1. 停止脚本或后台 screen。
2. 设置：

```env
TRADING_ENABLED=false
```

3. 检查 Polymarket 的 Open 页面，取消不需要的买单。

重要规则：

- 比赛结束后不能再补买单。
- 已经持有的仓位可以继续保留卖单。
- 脚本延迟醒来时不能下过期单。

## 8. 必须检查的安全项

每次运行前确认：

- 市场没有结束。
- 买的是正确市场，不是把整场和 Game 1 / Game 2 搞混。
- 买的是正确方向，比如 DK 不是 Gen.G。
- `TOKEN_ID` 是正确 outcome token。
- `SHARE_AMOUNT = 投入金额 / 买入价`。
- `EXIT_PRICE` 已填写。
- `TRADING_ENABLED=true` 只在准备交易时打开。
- 运行后去 Polymarket `Portfolio -> Open` 看订单是否符合预期。

## 9. 当前最重要的风控机制

脚本必须遵守：

- 不能只按 `TOKEN_ID` 判断是否买过。
- 下单前必须查 open orders、recent trades、positions。
- 网络/API 不确定时暂停，不补单。
- 买入成交后必须同步挂卖单。
- 同 token 分层加仓时，必须给新增份数单独补卖单。
- 比赛结束后禁止延迟买入。

## 10. 最省心的使用方式

你可以继续把交易信息发给 Codex：

```text
市场链接：
买哪个市场：整场 / Game 1 / Game 2 / 其他
买哪一边：
买入价：
投入金额：
卖出价：
是否分层：
```

Codex 应该负责：

- 找 token；
- 算 shares；
- 检查比赛是否结束；
- 检查已有挂单/持仓；
- 备份 `.env`；
- 设置配置；
- 启动或停止脚本；
- 确认买单和卖单保护。
