# Polymarket Trading Bot — Automated CLOB Strategy (Python)

A Python rewrite of
[`codiebyheaart/Polymarket-Trading-Bot-Automated-CLOB-Strategy-in-JavaScript`](https://github.com/codiebyheaart/Polymarket-Trading-Bot-Automated-CLOB-Strategy-in-JavaScript),
preserving all of the original project's core functionality and adding
**multi-bot Telegram notifications**, **multi-endpoint webhook notifications**,
and an **interactive Telegram command bot** for runtime control.

> ⚠️ For educational purposes only. Trading prediction markets involves
> significant risk. You are solely responsible for any financial losses.
> Always test with small amounts before scaling.

---

## What is preserved from the original

The original JavaScript bot is a modular template built on the official
Polymarket CLOB client. This Python port keeps every piece of that logic:

| Original (JS)        | This project (Python) | Responsibility                                               |
| -------------------- | --------------------- | ----------------------------------------------------------- |
| `index.js`           | `main.py` + `runtime.py` | Entry point + the entry/exit strategy loop                  |
| `trading.js`         | `trading.py`          | `init_client`, `enter_position`, `exit_position` (limit orders) |
| `strategy.js`        | `strategy.py`         | `should_enter` / `should_exit` decision logic               |
| `env.example`        | `.env.example`        | Wallet / config                                             |
| `@polymarket/clob-client` | `py-clob-client-v2`  | Official Polymarket CLOB SDK                                |
| `ethers` (wallet)    | handled by `py-clob-client-v2` | Wallet signing + L2 API credential derivation            |

The original `index.js` used **mock** market/position data with comments noting
that a real loop would fetch the live order book. This port makes that intent
real: each cycle fetches the live order book (`get_order_book`), derives
best bid/ask, tracks positions from fills, and runs a real polling loop — while
keeping the strategy functions modular and customisable exactly as the original
intended.

## What is new (and only what was requested)

1. **Telegram notifications — multiple bots.** Push the same message to every
   configured `{token, chat_id}`:
   - the markets the bot currently has resting orders in,
   - buy / sell fill details,
   - runtime status (monitored markets, positions, parameters).
2. **Webhook notifications — multiple endpoints.** Each configured URL receives
   a JSON POST with the same notification content.
3. **Interactive Telegram bot.** Long-polling command bot for:
   - setting / switching the active trading market,
   - configuring the buy (entry) price,
   - configuring the exit strategy (exit price, take-profit, stop-loss, size),
   - querying current status & positions.

No other features were added.

---

## Project structure

```
polymarket_trading_bot_strategy/
├── main.py                      # entry point: wires everything together
├── runtime.py                   # strategy / trading / notification main loop
├── trading.py                   # CLOB client init + enter/exit + market helpers
├── strategy.py                  # should_enter / should_exit (with TP/SL)
├── config.py                    # env + config.json loading, thread-safe guard
├── notifications/
│   ├── manager.py               # NotifierManager + message formatters
│   ├── telegram_notifier.py     # multi-bot push delivery
│   └── webhook_notifier.py      # multi-endpoint webhook delivery
├── bot/
│   └── telegram_bot.py          # interactive command bot (getUpdates polling)
├── requirements.txt
├── .env.example
└── README.md
```

---

## Installation

Requires **Python 3.9+**.

```bash
cd polymarket_trading_bot_strategy
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

A Polygon-compatible wallet (e.g. MetaMask) holding **MATIC** (gas) and
**USDC on Polygon** (capital), connected to a Polymarket account, is required
for trading. For EOA wallets you must also set the USDC / Conditional-Token
allowances once (see the Polymarket docs).

## Configuration

1. Copy `.env.example` to `.env` and fill in your values.
2. At minimum set `PRIVATE_KEY` and one market (`TOKEN_ID` + `MARKET_LABEL`, or
   a `MARKETS` JSON array).

### Key variables

| Variable                      | Description                                                       |
| ----------------------------- | ----------------------------------------------------------------- |
| `PRIVATE_KEY`                 | Wallet private key (required for trading)                         |
| `POLY_HOST`                   | CLOB endpoint (default `https://clob.polymarket.com`)             |
| `CHAIN_ID`                    | Polygon chain id (default `137`)                                  |
| `SIGNATURE_TYPE`              | `0` EOA (default), `1` email/Magic, `2` browser-wallet proxy      |
| `FUNDER`                      | Funder address (required for signature types 1 and 2)             |
| `TOKEN_ID` / `MARKETS`        | One market, or a JSON array of `{"token_id","label"}`             |
| `SHARE_AMOUNT`                | Order size in shares                                              |
| `ENTRY_PRICE` / `EXIT_PRICE`  | Limit prices (0.00 – 1.00)                                        |
| `TAKE_PROFIT_PCT` / `STOP_LOSS_PCT` | Exit thresholds as fractions (e.g. `0.10` = 10%; blank = off) |
| `POLL_INTERVAL`               | Seconds between cycles (default `30`)                             |
| `TELEGRAM_BOTS`               | JSON array of `{"token","chat_id","enabled"}` push bots           |
| `TELEGRAM_INTERACTIVE_TOKEN`  | Token of the bot that listens for commands                        |
| `TELEGRAM_ALLOWED_USER_IDS`   | Comma-separated command whitelist (blank = allow anyone)          |
| `WEBHOOKS`                    | JSON array of `{"url","enabled"}` endpoints                       |

### Mutable config file (`config.json`)

Trading parameters that Telegram commands can change (active market, prices,
exit strategy, size) are persisted to `config.json` (path via `CONFIG_FILE`).
Env values provide the defaults; `config.json` overrides them once created, so
runtime changes survive restarts. You may delete `config.json` to reset to env
defaults.

## Usage

```bash
python main.py
```

The bot initialises the CLOB client, derives L2 API credentials, starts the
notification dispatchers, starts the interactive Telegram bot (if configured),
and runs the strategy loop until you press **Ctrl-C**.

## Telegram commands

Send these to the bot whose token you set as `TELEGRAM_INTERACTIVE_TOKEN`:

| Command                              | Action                                              |
| ------------------------------------ | --------------------------------------------------- |
| `/status`                            | Show current status & positions                     |
| `/market`                            | List monitored markets (active marked `*`)          |
| `/market set <index>`                | Switch the active market                            |
| `/market add <token_id> [label]`     | Add a market and switch to it                       |
| `/price <0-1>`                       | Set the buy (entry) price                           |
| `/exit_price <0-1>`                  | Set the exit price                                  |
| `/takeprofit <pct\|off>`             | Set take-profit (`10` = 10%, `off` disables)        |
| `/stoploss <pct\|off>`               | Set stop-loss (`10` = 10%, `off` disables)          |
| `/amount <n>`                        | Set the order size in shares                        |

Config changes are pushed as notifications to all Telegram bots and webhooks.

## How the strategy works

- **Entry** (`should_enter`): place a BUY limit order when the best ask is at or
  below the configured `ENTRY_PRICE` (i.e. shares are available at our limit).
- **Exit** (`should_exit`): place a SELL limit order when any of these hold:
  - take-profit target reached (`profit >= TAKE_PROFIT_PCT`),
  - stop-loss threshold breached (`loss >= STOP_LOSS_PCT`),
  - best bid reaches the configured `EXIT_PRICE` (the original fixed rule).

Both functions are small and self-contained — edit `strategy.py` to implement
custom logic (volume / spread / probability based) without touching the rest of
the bot.

## Notifications

Three categories are dispatched to every Telegram bot and every webhook:

- `open_orders` — current resting orders, grouped by market.
- `fill` — buy/sell fill details (side, size, price, order id, time).
- `status` — periodic runtime summary (cycle, active market, monitored markets,
  parameters, positions).

Webhook payload shape:

```json
{
  "event": "fill",
  "text": "✅ Fill: Bought ...",
  "timestamp": "2026-07-17T08:47:09Z",
  "unix_ts": 1752748029
}
```

## Notes

- Positions are tracked locally from observed fills during a run (the original
  used mock position data). Entry fills grow the position and set the average
  price; exit fills reduce it. Restarting resets tracked positions to zero, so
  manage open positions across restarts accordingly.
- The official `py-clob-client-v2` uses HTTP/2. In some restricted network
  environments HTTP/2 may be blocked while HTTP/1.1 works; run the bot in an
  environment with normal outbound HTTPS to `clob.polymarket.com`.

## Disclaimer

This software is for educational purposes only. Trading prediction markets
involves significant risk. You are solely responsible for any financial losses
incurred while using this software. Always test with small amounts before
scaling.
