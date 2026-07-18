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
   - checks whether an open entry order already exists for the token;
   - if no open entry exists, places or waits for a BUY entry depending on `CONDITIONAL_ENTRY`.
5. If a local position exists:
   - checks exit conditions;
   - if triggered, places a SELL order for the tracked position size.
6. Prints status and order events to the terminal.

## Important Behavior Notes

- `GTC` means the order remains open until filled, cancelled, or the market stops trading.
- The current bot uses normal GTC limit orders, not explicit post-only orders.
- A normal BUY GTC limit order can fill immediately if the current best ask is at or below the entry price.
- `CONDITIONAL_ENTRY=false` makes the bot submit the entry order as soon as it starts.
- The bot checks for existing open orders on the same token to avoid repeatedly submitting the same entry order.
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
