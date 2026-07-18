# Polymarket Bot V1 使用说明

## 1. 这个版本能做什么

当前第一版是一个本地命令行交易脚本。

它一次只交易一个市场的一个方向，例如：

```text
买 Gen.G @ 0.25
投入约 40 USDC
```

它会连接 Polymarket，按 `.env` 里的参数挂一个 BUY 限价单。

## 2. 每次换比赛要提供的信息

以后经常使用这个模板：

```text
市场链接：
买哪一边：
买入价：
单次投入金额：
卖出价 / 止盈目标：
是否启动后立刻挂单：
```

示例：

```text
市场链接：https://polymarket.com/...
买哪一边：Gen.G
买入价：0.25
单次投入金额：40 USDC
卖出价 / 止盈目标：0.40
是否启动后立刻挂单：是
```

## 3. 运行脚本

打开终端，输入：

```bash
cd /Users/ad/Documents/polydata/polymarket_trading_bot_strategy
source .venv/bin/activate
python main.py
```

看到下面内容说明脚本已经启动：

```text
Starting Polymarket Trading Bot (Python)...
[main] bot is running. Press Ctrl-C to stop.
```

## 4. 停止脚本

在终端按：

```text
Ctrl + C
```

停止脚本不会自动取消已经挂在 Polymarket 上的订单。需要取消订单时，去 Polymarket 网页的 Portfolio -> Open 手动取消。

## 5. 怎么判断挂单成功

终端里看到类似：

```text
Order placed: {'status': 'live', 'success': True}
```

网页里看到：

```text
Portfolio -> Open
Buy 某个方向 某个价格
Until cancelled
```

这就表示挂单成功。

## 6. 关键配置解释

```env
TOKEN_ID=
```

要交易的 outcome token。一个市场通常有两个方向，每个方向都有不同 token。

```env
MARKET_LABEL=
```

日志里显示的名字，不影响交易。

```env
ENTRY_PRICE=
```

买入限价。BUY 订单最多愿意支付的价格。

```env
SHARE_AMOUNT=
```

买入 shares 数量，不是 USDC 金额。

计算方式：

```text
SHARE_AMOUNT = 投入金额 / 买入价
```

```env
EXIT_PRICE=
```

固定卖出触发价格。当前 best bid 达到这个价格时，脚本会尝试卖出。

```env
TAKE_PROFIT_PCT=
```

止盈比例。`0.60` 表示盈利 60%。

注意：如果 `EXIT_PRICE` 比止盈目标低，可能会更早触发卖出。

```env
CONDITIONAL_ENTRY=false
```

`false` 表示启动后直接挂买单。

```env
CONDITIONAL_ENTRY=true
```

`true` 表示等待市场 best ask 小于等于 `ENTRY_PRICE` 时才下单。

## 7. GTC 和 Post-only

当前脚本使用普通 GTC 限价单。

GTC：

```text
订单一直有效，直到成交、取消或市场停止交易。
```

普通 BUY GTC：

```text
如果市场最低卖价小于等于你的买入价，会直接成交。
否则就挂在订单簿里等待。
```

Post-only：

```text
只允许挂单，不允许立刻成交。
```

当前第一版没有强制 post-only。

## 8. 当前版本限制

- 不支持同一场比赛两个方向同时挂单。
- 不支持多场比赛同时运行。
- 不支持成交一边后自动撤另一边。
- 没有网页控制台。
- 没有完整审计数据库。
- 没有硬编码的 80 USDC 风控，只靠配置换算。

## 9. 备份

当前第一版可用配置和代码已经备份在：

```text
/Users/ad/Documents/polydata/polymarket_trading_bot_strategy/backups/v1-product-20260718-142112
```

这个备份包含 `.env`，里面有敏感信息，不要外传。
