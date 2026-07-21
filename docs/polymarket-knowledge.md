# Polymarket Bot Knowledge

## Knowledge Capture Rule

Product-detail discussions should be saved here automatically when they affect future behavior, especially:

- trading rules and duplicate-order prevention
- buy/sell/cancel workflows
- manual-trade handoff rules
- wallet/API/signature lessons
- user-facing operating instructions
- future product requirements and safety constraints

The goal is to keep product decisions out of fragile chat memory. If a later tool or product version is built from this project, this file should be treated as the baseline knowledge base.

## Operator Guide

The user-facing self-management guide is saved in:

- `docs/SELF_MANAGEMENT_GUIDE.md`
- `docs/self-management-guide.html`

Use it when the user wants to manage the bot manually. It explains which `.env` fields are safe to edit, which wallet fields should not be touched, how to calculate shares, how to run single-layer and layered orders, and which safety checks must happen before trading.

## Current Successful Order Pattern

- A successful terminal result includes `Order placed`, `success: True`, and `status: live`.
- A successful web UI result appears under Portfolio -> Open.
- `0 / 160` means 0 shares filled out of 160 shares total.
- `Until cancelled` means the order is GTC and remains live until filled or cancelled.

## Critical Duplicate-Order Lesson

Never use `tokenId` alone to decide whether the bot already bought, should skip a BUY, or should place an automatic SELL.

Reason: the user can manually trade the same market/outcome in Polymarket. A manual trade and a bot trade can share the same `tokenId`, so token-only logic can confuse a manual position with a bot-managed position.

Priority: highest. Open-order and historical-fill reconciliation is a core safety mechanism, not a convenience feature. It must run before every new BUY, SELL, cancel, replace, retry, or manual-position handoff.

Future tools should identify the intended order by a trade-intent fingerprint:

- `tokenId`
- side (`BUY` or `SELL`)
- configured price
- configured shares and/or configured USDC amount
- known bot order ID when available
- timestamp/session/run ID when available

Duplicate prevention should skip only when an open order or historical fill is similar to the current intended order. Similarity means same token and side, plus matching or very close price and shares/amount.

Auto-sell should manage only:

- a fill created by the bot's own order, preferably verified by order ID; or
- a manual position the user explicitly asks the bot to sell, with the side, shares, and target sell price confirmed.

This rule also applies in reverse: do not auto-buy, auto-sell, cancel, or replace an order only because the token matches. Always match the intended action and size/price.

Before any order action, the bot must:

- check current open orders;
- check recent fills/trades;
- check current positions when available;
- compare token, side, price, and shares/amount against the intended action;
- pause instead of acting if CLOB/API checks fail or return ambiguous data.

## Mandatory Exit-Order Protection

Priority: highest. Any bot-managed BUY must have a corresponding SELL plan before the BUY is allowed.

Required behavior:

- `EXIT_PRICE` must be configured before the bot places a BUY.
- After a bot-managed BUY fill is detected, the bot must place the matching SELL order at `EXIT_PRICE` as the next priority.
- If a managed position exists and no matching SELL order exists, exit protection runs before all new entry logic.
- If open-order checks fail, the bot pauses instead of assuming the SELL exists or placing more BUY orders.
- If `EXIT_PRICE` is missing while a position exists, the bot must report a critical unmanaged-position state and wait for manual instruction.

The bot should never intentionally leave a bot-managed filled BUY without a matching resting SELL order.

Fill detection must not rely on only one source. If CLOB recent trades lag or omit a fresh fill, the bot must also reconcile against Polymarket portfolio positions. A matching portfolio position should trigger the same exit-order protection when:

- the bot attempted an entry for that token in the current run; or
- the position size and average price closely match the configured shares and entry price.

Layered entries on the same token need separate protection. Before placing a new BUY for a token that already has position or SELL orders, record:

- existing portfolio size and average price;
- existing matching SELL quantity at the target exit price.

After the new BUY fills, manage only the incremental position size. The bot must require incremental SELL coverage, not merely any existing SELL at the same price. Example: if 200 shares already have `SELL 200 @ 0.65`, and a second 200-share entry fills, the bot must place another `SELL 200 @ 0.65` or otherwise ensure total SELL coverage reaches 400.

Opposite sides of the same market are different outcome tokens. If the user wants to place low-price orders on both sides, run them as separate token monitors. Each side must have its own baseline, BUY order, position reconciliation, and protective SELL order. Do not let a monitor for one outcome manage the opposite outcome.

## Event-End / Stale-Entry Protection

Priority: highest. The bot must not place delayed entry orders after the relevant game or market is already over, resolving, closed, or no longer strategically valid.

Required behavior:

- Before placing a BUY, check whether the market is still open and accepting orders.
- For live esports markets, also respect the user's latest instruction if they say a game has ended.
- If the game has ended, cancel or skip any unfilled entry plan for that game.
- Set `TRADING_ENABLED=false` after a game ends or when an entry plan is stale, so restarting the script cannot accidentally place the old BUY.
- If a script was delayed by API/network issues and wakes up after the game ended, it must pause instead of submitting the stale BUY.
- Exit management for already-held positions may continue, but no new entry should be placed for an ended game.

## Network Timeout Duplicate-Order Lesson

If a create-order request, open-order query, or trade query times out, the bot must not assume the order failed. A timeout can happen after Polymarket accepted the order but before the local script received the response.

Required behavior for future versions:

- record the intended order before sending it;
- if order submission times out, pause entry instead of immediately retrying;
- query open orders and recent trades before any retry;
- match by token, side, price, shares/amount, and order ID when available;
- persist bot-created order IDs and strategy run IDs so restarts can reconcile safely;
- never place another same-intent BUY just because the last API call returned an unknown network error.

This prevents duplicate buys after temporary CLOB/API/network failures.

## Manual-Buy Auto-Sell Incident

Priority: highest. A protective-sell monitor must never infer bot-managed fills
only from total portfolio position increases. If the user manually buys the same
token in the Polymarket web UI, total position increases too, and an old monitor
can wrongly place its configured SELL order against the manual position. Example:
the user manually bought near `0.90`, but an old monitor with `EXIT_PRICE=0.85`
could place an unwanted `SELL @ 0.85`.

Clear incident example:

1. The bot previously ran a Game 3 DK strategy with `EXIT_PRICE=0.85`.
2. The old monitor state recorded a baseline position, then kept checking the
   user's total position for the same DK outcome token.
3. Later, the user manually bought DK shares in the Polymarket web UI near
   `0.90`.
4. The total position for that token increased, even though the fill did not
   come from the bot's own BUY order.
5. The old monitor interpreted this position increase as a bot-managed fill and
   placed a protective `SELL @ 0.85`.
6. Result: a manual `0.90` buy could be automatically listed for sale at `0.85`,
   creating an unwanted loss-making order.

Root cause:

- The monitor used `position_size - baseline_position_size` as the managed fill
  amount.
- That logic cannot distinguish bot-created fills from manual web/UI trades.
- It is especially dangerous when old state files are restarted after the user
  has manually traded the same token.

Required behavior:

- protective SELL placement must be tied to bot-created BUY order IDs;
- if order-ID fill attribution is unavailable or ambiguous, pause and ask/check,
  rather than selling from a position delta;
- manual web/UI trades must remain unmanaged unless the user explicitly asks the
  bot to manage or sell that position;
- old state files must not be restarted blindly, especially when the user has
  traded the same token manually;
- stopping the script does not cancel exchange orders, so always distinguish
  local monitor shutdown from CLOB order cancellation.

Implemented mitigation in `tools/layered_order_runner.py`: `_ensure_exit_coverage`
now computes `managed_size` from tracked BUY order IDs only, instead of using
`position_size - baseline_position_size`.

## Position Rescue / Residual Value Workflow

When the user already holds a position and asks how much value remains, check both:

- portfolio/position API value, which may use a mark/current price;
- live order book best bid, which is the realistic immediate exit price.

Do not treat mark value as guaranteed exit value. For immediate liquidation, estimate residual value as:

```text
shares * current best bid
```

If an old high-price SELL order is already open, it may lock part of the shares. To sell all immediately, first cancel the old SELL order, then place a new SELL order at or near the best bid. To attempt a better exit, place a SELL near the best ask or another chosen price, but warn that it may not fill.

## Address Roles

- Rabby / EOA signer address: derived from `PRIVATE_KEY`; signs wallet prompts.
- Polymarket API / funder address: stored in `FUNDER`; for `SIGNATURE_TYPE=3`, order `maker`, `signer`, and `verifyingContract` should match this address.
- Deposit address: used only for adding funds; do not use it as `FUNDER`.

## Rabby TypedDataSign Fields

- `Operation=TypedDataSign`: EIP-712 typed-data signature.
- `contents.salt`: unique order salt / replay protection.
- `maker`: address whose funds/positions back the order.
- `signer`: address the CLOB expects to match the API key identity for the order.
- `tokenId`: outcome token being bought or sold.
- `makerAmount`: BUY side payment amount, usually USDC scaled by 1e6.
- `takerAmount`: BUY side shares amount, usually scaled by 1e6.
- `side`: `0` is BUY, `1` is SELL.
- `signatureType`: `3` is POLY_1271 / deposit wallet.
- `timestamp`: order signing timestamp, usually milliseconds.
- `metadata`: extra order metadata; all zero means none.
- `builder`: builder/referral field; all zero means none.

## Domain Fields

- `name=DepositWallet`: deposit-wallet signing domain.
- `version=1`: signing-domain version.
- `chainId=137`: Polygon mainnet.
- `verifyingContract`: deposit wallet / funder contract that validates the signature.
- `salt`: domain salt; all zero can be normal.

---

# Polymarket Bot Knowledge - 中文说明

## 知识沉淀规则

凡是会影响后续产品、脚本、安全机制的讨论，都要保存到这里，尤其是：

- 交易规则和重复下单防护；
- 买入、卖出、取消、替换挂单流程；
- 手动交易和脚本托管之间的边界；
- 钱包、API、签名方式相关经验；
- 给用户看的运行说明；
- 未来版本必须遵守的安全约束。

目标是不要只依赖聊天记录。以后如果基于这个项目做新工具或新版本，这个文件应该作为基础知识库。

## 操作说明文档位置

面向用户的自助管理文档保存在：

- `docs/SELF_MANAGEMENT_GUIDE.md`
- `docs/self-management-guide.html`

这两份文档用于用户自己管理脚本。里面说明了哪些 `.env` 字段可以改、哪些钱包字段不要动、如何计算份数、如何运行单层和分层挂单，以及交易前必须做哪些安全检查。

## 成功挂单的判断方式

- 终端里出现 `Order placed`、`success: True`、`status: live`，通常表示挂单成功。
- 网页端可以在 Portfolio -> Open 里看到开放挂单。
- `0 / 160` 表示总共 160 份，目前成交 0 份。
- `Until cancelled` 表示 GTC 挂单，会一直保留，直到成交或被取消。

## 关键重复下单教训

不能只用 `tokenId` 判断脚本是否已经买入、是否应该跳过买入、是否应该自动卖出。

原因：用户可能会在 Polymarket 网页端手动交易同一个市场、同一个方向。手动交易和脚本交易会共享同一个 `tokenId`，如果只看 token，脚本会把用户手动仓位误判成脚本管理仓位。

优先级：最高。每次新的 BUY、SELL、取消、替换、重试、接管手动仓位之前，都必须检查开放挂单、历史成交和当前仓位。

未来工具应该用一组交易意图指纹来识别订单：

- `tokenId`
- 方向，`BUY` 或 `SELL`
- 配置价格
- 配置份数或 USDC 金额
- 已知的脚本订单 ID
- 时间戳、会话 ID、运行 ID

只有当开放挂单或历史成交与当前意图高度相似时，才跳过重复下单。相似的含义是：token 和方向一致，价格和份数/金额也接近。

自动卖出只能管理：

- 脚本自己创建的买单成交，最好通过订单 ID 确认；
- 或用户明确要求脚本管理/卖出的手动仓位，并确认方向、份数和目标卖价。

反过来也一样：不能只因为 token 一样就自动买入、自动卖出、取消或替换订单。必须匹配动作、价格和数量。

任何下单动作前，脚本必须：

- 检查当前开放挂单；
- 检查最近成交记录；
- 能查到时检查当前仓位；
- 对比 token、方向、价格、份数/金额；
- 如果 CLOB/API 检查失败或结果模糊，暂停，不要猜。

## 强制卖单保护

优先级：最高。任何由脚本管理的 BUY，在允许买入前必须有对应的 SELL 计划。

必须满足：

- 买入前必须配置 `EXIT_PRICE`。
- 脚本管理的 BUY 成交后，下一优先级就是按 `EXIT_PRICE` 挂对应 SELL。
- 如果已经有脚本管理仓位但没有匹配卖单，卖单保护优先于新的买入逻辑。
- 如果开放挂单检查失败，脚本暂停，不能假设卖单已经存在，也不能继续加买单。
- 如果有仓位但缺少 `EXIT_PRICE`，脚本必须报告“未受保护仓位”，等待人工指令。

脚本不应该故意留下“已成交买单但没有对应卖单”的状态。

成交检测不能只依赖一个数据源。如果 CLOB 最近成交记录延迟或遗漏，脚本还要用 Polymarket 的 portfolio positions 做补充校验。只有在以下情况下，portfolio 仓位才可以触发保护卖单：

- 当前运行里脚本确实尝试过这个 token 的入场；
- 或仓位大小、平均价格与配置的份数和买入价高度匹配。

同一个 token 的分层买入需要分层保护。新买单下单前要记录：

- 已有仓位大小和均价；
- 当前目标卖价下已经存在的 SELL 数量。

新买单成交后，只管理新增部分。不能因为已经有一张同价 SELL 就认为全部仓位都有保护。比如原来有 200 份和 `SELL 200 @ 0.65`，第二笔 200 份成交后，总卖单保护应该达到 400 份。

同一个市场的两个方向是不同 outcome token。如果用户想同时买两个方向，要分别运行 token 监控。每个方向都要有自己的基线、买单、仓位校验和保护卖单。

## 比赛结束 / 过期入场保护

优先级：最高。相关小局、比赛或市场已经结束、结算、关闭，或策略上已经不适合时，脚本不能再延迟提交旧买单。

必须满足：

- BUY 前检查市场是否仍 open 且 accepting orders。
- 对直播电竞市场，要尊重用户最新指令。如果用户说这一局结束了，就不能再入场。
- 比赛结束后，取消或跳过未成交的入场计划。
- 比赛结束或入场计划过期后，设置 `TRADING_ENABLED=false`，防止重启脚本时误下旧单。
- 如果脚本因为 API/网络延迟醒来时比赛已经结束，必须暂停，而不是提交旧 BUY。
- 已持仓的退出管理可以继续，但不能再为结束的比赛新增入场。

## 网络超时与重复下单教训

创建订单、查询开放挂单、查询成交时发生超时，不能直接认为订单失败。超时可能发生在 Polymarket 已经接受订单之后，但本地脚本没收到返回之前。

未来版本必须：

- 发送订单前先记录意图；
- 如果提交订单超时，暂停，不要立刻重试；
- 重试前查询开放挂单和最近成交；
- 按 token、方向、价格、份数/金额、订单 ID 匹配；
- 持久化脚本创建的订单 ID 和策略运行 ID，方便重启后对账；
- 不能因为上一次 API 调用返回未知网络错误，就再发一张相同 BUY。

这样可以避免 CLOB/API 网络抖动后重复买入。

## 手动买入被自动卖出事故

优先级：最高。保护卖单监控绝不能只通过“总仓位增加”推断脚本买单成交。用户在 Polymarket 网页端手动买入同一个 token，也会让总仓位增加，旧监控可能错误地用自己的 `EXIT_PRICE` 去卖用户手动仓位。例子：用户在接近 `0.90` 手动买入，但旧监控配置了 `EXIT_PRICE=0.85`，结果可能错误挂出 `SELL @ 0.85`。

清晰事故示例：

1. 脚本之前跑过一套 Game 3 DK 策略，配置的 `EXIT_PRICE=0.85`。
2. 旧监控状态记录了一个基线仓位，然后持续检查同一个 DK outcome token 的总仓位。
3. 后来用户自己在 Polymarket 网页端手动买入 DK，买入价格接近 `0.90`。
4. 这个 token 的总仓位增加了，但这次成交并不是脚本自己的 BUY 订单产生的。
5. 旧监控把“总仓位增加”误判成“脚本管理的买单成交”，于是自动挂出 `SELL @ 0.85`。
6. 结果：用户手动 `0.90` 买入的仓位，可能被脚本自动挂到 `0.85` 卖出，形成不想要的亏损卖单。

根本原因：

- 旧监控用 `position_size - baseline_position_size` 当作脚本管理的成交份数。
- 这个逻辑无法区分脚本自己的成交和用户网页端手动交易。
- 如果用户手动交易过同一个 token，又重启旧 state 文件，这个问题尤其危险。

必须满足：

- 保护 SELL 必须绑定脚本自己创建的 BUY 订单 ID；
- 如果无法确认 order ID 成交归属，或者结果模糊，必须暂停并检查/询问，而不是根据仓位差卖出；
- 用户网页端手动交易的仓位默认不归脚本管理，除非用户明确要求脚本接管或卖出；
- 不能盲目重启旧 state 文件，尤其是用户已经手动交易过同一个 token 时；
- 停止脚本不等于取消交易所挂单，必须区分本地监控停止和 CLOB 挂单取消。

已经在 `tools/layered_order_runner.py` 做了缓解：`_ensure_exit_coverage` 现在只根据已追踪的 BUY order ID 计算 `managed_size`，不再使用 `position_size - baseline_position_size`。

## 仓位救援 / 残值处理流程

当用户已经持有仓位并询问还剩多少价值时，需要同时检查：

- portfolio/position API 的价值，它可能是标记价或当前价；
- 实时订单簿 best bid，这才是更接近“现在立刻卖出”的价格。

不能把 mark value 当成一定能成交的退出价值。若要估算立刻卖出的残值，应使用：

```text
shares * current best bid
```

如果已经有高价 SELL 挂单，它可能锁住一部分份额。要立刻全部卖出，需要先取消旧 SELL，再按 best bid 附近挂新的 SELL。若想争取更高价，可以挂在 best ask 或用户指定价格，但要提醒可能不会成交。

## 地址角色

- Rabby / EOA signer address：由 `PRIVATE_KEY` 推导，用于钱包签名。
- Polymarket API / funder address：存放在 `FUNDER`。对于 `SIGNATURE_TYPE=3`，订单里的 `maker`、`signer`、`verifyingContract` 应该匹配这个地址。
- Deposit address：只用于充值，不要当作 `FUNDER`。

## Rabby TypedDataSign 字段

- `Operation=TypedDataSign`：EIP-712 typed-data 签名。
- `contents.salt`：订单唯一 salt，用于防重放。
- `maker`：提供资金或仓位的地址。
- `signer`：CLOB 期望与 API key 身份匹配的签名地址。
- `tokenId`：正在买入或卖出的 outcome token。
- `makerAmount`：BUY 侧支付金额，通常是 USDC 按 1e6 缩放。
- `takerAmount`：BUY 侧获得份数，通常也按 1e6 缩放。
- `side`：`0` 是 BUY，`1` 是 SELL。
- `signatureType`：`3` 是 POLY_1271 / deposit wallet。
- `timestamp`：订单签名时间，通常是毫秒。
- `metadata`：额外订单元数据，全 0 通常表示没有。
- `builder`：builder/referral 字段，全 0 通常表示没有。

## Domain 字段

- `name=DepositWallet`：deposit-wallet 签名域。
- `version=1`：签名域版本。
- `chainId=137`：Polygon 主网。
- `verifyingContract`：用于验证签名的 deposit wallet / funder 合约。
- `salt`：domain salt，全 0 可能是正常情况。
