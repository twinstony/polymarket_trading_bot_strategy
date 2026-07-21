# Polymarket LOL Trading Bot V1 Baseline

Date: 2026-07-18
Status: First working live-trading baseline

## Product Definition

This V1 product is a local command-line Polymarket trading bot for one selected market outcome at a time.

It supports:

- One market outcome token at a time.
- One BUY limit entry order.
- Configurable entry price.
- Configurable share amount, usually calculated from target USDC budget.
- Configurable fixed exit price.
- Optional take-profit percentage and stop-loss percentage.
- Live Polymarket CLOB trading through the configured wallet.
- Basic duplicate-entry protection when an open order already exists for the same token.

It does not yet support:

- Two-sided same-market entry orders.
- Automatically cancelling the opposite side after one side fills.
- Web dashboard market selection.
- Multiple simultaneous matches.
- Hard-coded per-match risk enforcement.
- Explicit post-only semantics.
- Persistent audit database.

## Current Working Pattern

The first verified successful live order was:

- Market: LoL Gen.G vs Dplus KIA - Match Winner
- Side: Buy Gen.G
- Entry price: 0.25
- Size: 160 shares
- Estimated notional: 40 USDC
- Order status observed in Polymarket UI: Open / Until cancelled

This confirms:

- The selected wallet configuration can sign and submit live orders.
- `SIGNATURE_TYPE=3` works with the current funder address.
- The bot can place a live GTC limit order through Polymarket CLOB.

## Configuration Contract

The frequently used request format is:

```text
市场链接：
买哪一边：
买入价：
单次投入金额：
卖出价 / 止盈目标：
是否启动后立刻挂单：
```

The main `.env` fields affected by each request are:

- `TOKEN_ID`: outcome token ID for the chosen side.
- `MARKET_LABEL`: readable label for logs.
- `ENTRY_PRICE`: BUY limit price.
- `SHARE_AMOUNT`: shares to buy.
- `EXIT_PRICE`: fixed SELL trigger/limit price.
- `TAKE_PROFIT_PCT`: optional percentage profit trigger.
- `CONDITIONAL_ENTRY`: `false` means place entry immediately; `true` means wait until best ask is at or below entry price.

Budget formula:

```text
SHARE_AMOUNT = target_usdc / ENTRY_PRICE
```

Examples:

- 40 USDC at 0.25 -> 160 shares.
- 40 USDC at 0.20 -> 200 shares.
- 40 USDC at 0.10 -> 400 shares.

## Runtime Logic

Every `POLL_INTERVAL` seconds, the bot:

1. Loads the active market and trading parameters.
2. Checks recent fills and updates local in-memory position state.
3. Fetches the live order book.
4. If no position exists:
   - checks whether a matching open entry order already exists for this trade intent;
   - if no open entry exists, places or waits for a BUY entry depending on `CONDITIONAL_ENTRY`.
5. If a local position exists:
   - checks exit conditions;
   - if triggered, places a SELL order for the tracked position size.
6. Prints status and order events to the terminal.

## Duplicate-Order Safety Requirement

Do not treat `TOKEN_ID` alone as proof that the bot already bought or should not buy. The user may also place manual orders on Polymarket, and those manual trades can have the same token.

Priority: highest. Automatic open-order, fill, and position checks must happen before every order action. This includes new entries, exits, retries, manual-position handoffs, cancellations, and price changes.

For future versions, duplicate prevention must use a trade-intent fingerprint:

- token / market outcome
- side (`BUY` or `SELL`)
- target entry or exit price
- intended shares and/or intended USDC amount, with a small tolerance
- bot-created order ID when available
- session start time or a persisted strategy run ID

The bot should skip a new BUY only when an existing open order or historical fill looks like the same intended bot order, not merely because the same token has any historical BUY.

The same rule applies to auto-exit. A historical manual BUY should not automatically become a bot-managed position unless the user explicitly asks the bot to manage that manual position, or the trade matches the current bot intent by amount/shares/price/order identity.

If the bot cannot confirm open orders, recent fills, or current position state, it should pause and report the uncertainty instead of placing another order.

## Mandatory Exit Protection

Every automated BUY must have a configured sell plan before entry.

Rules:

- `EXIT_PRICE` is required before automated entry.
- Once a managed BUY fill is detected, placing the matching SELL order is the next priority.
- If a managed position exists without a matching SELL order, the bot must place the SELL before considering any new BUY.
- If open orders cannot be checked, the bot pauses rather than assuming the exit order exists.
- A filled bot-managed position without a resting SELL order is a critical state.

## Stale-Entry Protection

Do not place new BUY orders after the relevant game or market has ended.

Rules:

- Check market status before entry.
- Treat user reports that a live game has ended as a hard stop for new entry.
- Use `TRADING_ENABLED=false` to prevent stale configurations from placing orders if the script is restarted.
- If the bot wakes up late after network/API delays, skip stale BUY orders.
- Existing exit orders for already-held positions can remain active, but new entries must stop.

## Important Behavior Notes

- `GTC` means the order remains open until filled, cancelled, or the market stops trading.
- The current bot uses normal GTC limit orders, not explicit post-only orders.
- A normal BUY GTC limit order can fill immediately if the current best ask is at or below the entry price.
- `CONDITIONAL_ENTRY=false` makes the bot submit the entry order as soon as it starts.
- Duplicate checks must compare the configured price and shares/amount, not only the token.
- The bot does not automatically cancel orders when stopped. Use Polymarket UI if an order should be cancelled.

## Safety Rules

- Keep the working `.env` backed up before changing markets.
- Do not paste private keys into docs, screenshots, or public chats.
- Confirm the chosen outcome token before running.
- Confirm Polymarket UI shows the expected side and price after placing an order.
- Stop the bot with `Ctrl + C`.
- Use the Polymarket UI to cancel live orders if needed.

## Current Backup

The V1 code/config backup was created under:

```text
backups/v1-product-20260718-142112
```

This backup includes `.env` and core source files. Treat it as sensitive because `.env` contains wallet credentials.
