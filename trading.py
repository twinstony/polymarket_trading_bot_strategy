"""
CLOB trading client — migrated to Polymarket CLOB V2 SDK.

Provides:
* ``init_client``         - initialise V2 ClobClient and derive L2 API credentials.
* ``enter_position``      - place a BUY limit order to open a position.
* ``exit_position``       - place a SELL limit order to close a position.
* ``get_market_data``     - fetch the live order book and derive best bid/ask.
* ``get_open_orders``     - list currently resting orders.
* ``get_recent_trades``   - list recent fills (for fill notifications).
* ``get_last_trade_price``- last traded price for a token.

V1→V2 migration (Apr 2026):
  py-clob-client (0.34.x) is archived; use py-clob-client-v2 instead.
  - create_and_post_order replaces create_order + post_order
  - get_open_orders() replaces get_orders(OpenOrderParams())
  - Order book returned as dict, not object
  - Side.BUY / Side.SELL are int constants
  - tick_size is required when placing orders
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from py_clob_client_v2 import (
    ApiCreds,
    ClobClient,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)
from py_clob_client_v2.http_helpers import helpers as clob_http_helpers

from strategy import MarketData

clob_http_helpers._http_client = httpx.Client(http2=False, timeout=30)


# --------------------------------------------------------------------------- #
# Client initialisation
# --------------------------------------------------------------------------- #
def init_client(config) -> ClobClient:
    """Create an authenticated V2 ClobClient.

    Prefer explicit CLOB API credentials from .env. This matters for
    POLY_1271/deposit-wallet accounts because auto-derived credentials can bind
    to the owner EOA while orders are signed for the deposit wallet.
    """
    if not config.private_key:
        raise RuntimeError("PRIVATE_KEY not configured; cannot initialise CLOB client")

    kwargs: dict[str, Any] = {
        "host": config.host,
        "key": config.private_key,
        "chain_id": config.chain_id,
    }
    if config.signature_type:
        kwargs["signature_type"] = config.signature_type
    if config.funder:
        kwargs["funder"] = config.funder

    has_manual_creds = all(
        (config.clob_api_key, config.clob_api_secret, config.clob_api_passphrase)
    )
    if has_manual_creds:
        creds = ApiCreds(
            api_key=config.clob_api_key,
            api_secret=config.clob_api_secret,
            api_passphrase=config.clob_api_passphrase,
        )
    else:
        unauth = ClobClient(**kwargs)
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                creds = unauth.create_or_derive_api_key()
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                print(f"[trading] API key derive failed ({attempt}/3): {exc}")
                if attempt < 3:
                    time.sleep(attempt * 3)
        else:
            raise last_error or RuntimeError("could not derive CLOB API credentials")

    client = ClobClient(**kwargs, creds=creds)
    try:
        address = client.get_address()
    except Exception:
        address = "?"
    print(f"[trading] CLOB V2 Client initialised for address: {address}")
    return client


# --------------------------------------------------------------------------- #
# Side resolution (V2 uses int constants Side.BUY=0 / Side.SELL=1)
# --------------------------------------------------------------------------- #
def _resolve_side(side) -> int:
    if isinstance(side, int):
        if side in (Side.BUY, Side.SELL):
            return side
        raise ValueError(f"Unknown order side int: {side}")
    if isinstance(side, str):
        s = side.strip().upper()
        if s == "BUY":
            return Side.BUY
        if s == "SELL":
            return Side.SELL
    raise ValueError(f"Unknown order side: {side!r}")


# --------------------------------------------------------------------------- #
# Order placement
# --------------------------------------------------------------------------- #
def _post_limit_order(
    client: ClobClient,
    token_id: str,
    side: int,
    amount: float,
    price: float,
    label: str,
) -> dict[str, Any] | None:
    """Shared helper for enter / exit limit orders."""
    if client is None:
        raise RuntimeError("Client not initialised")
    side_str = "BUY" if side == Side.BUY else "SELL"
    print(f"[trading] {label}: {side_str} {amount} @ {price}")

    try:
        # Fetch tick_size from the order book (required by V2 API).
        book = client.get_order_book(token_id)
        tick_size = str(book.get("tick_size", "0.01"))

        resp = client.create_and_post_order(
            order_args=OrderArgs(
                token_id=token_id,
                price=float(price),
                side=side,
                size=float(amount),
            ),
            options=PartialCreateOrderOptions(tick_size=tick_size),
            order_type=OrderType.GTC,
        )
        print(f"[trading] Order placed: {resp}")
        return resp if isinstance(resp, dict) else {"response": str(resp)}
    except Exception as exc:
        print(f"[trading] Failed to {label.lower()}: {exc}")
        return None


def enter_position(
    client: ClobClient, token_id: str, side, amount: float, price: float
) -> dict[str, Any] | None:
    """Place a BUY limit order to open a position."""
    return _post_limit_order(
        client, token_id, _resolve_side(side), amount, price, "ENTER"
    )


def exit_position(
    client: ClobClient, token_id: str, side, amount: float, price: float
) -> dict[str, Any] | None:
    """Place a SELL limit order to close a position."""
    return _post_limit_order(
        client, token_id, _resolve_side(side), amount, price, "EXIT"
    )


# --------------------------------------------------------------------------- #
# Live market / order helpers
# --------------------------------------------------------------------------- #
def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _book_levels(book: dict, key: str) -> list[tuple[float, float]]:
    """Extract ``(price, size)`` tuples from one side of an order book dict.

    V2 returns the book as a dict with ``asks`` / ``bids`` keys, each a list of
    ``{"price": str, "size": str}`` dicts.
    """
    levels_raw = book.get(key, [])
    if not levels_raw:
        return []
    out: list[tuple[float, float]] = []
    for lvl in levels_raw:
        price = _to_float(lvl.get("price")) if isinstance(lvl, dict) else None
        size = _to_float(lvl.get("size")) if isinstance(lvl, dict) else None
        if price is not None and size is not None:
            out.append((price, size))
    return out


def get_market_data(client: ClobClient, token_id: str) -> MarketData:
    """Fetch the order book and return best bid / ask / mid for a token."""
    md = MarketData(token_id=token_id)
    try:
        book = client.get_order_book(token_id)
    except Exception as exc:
        print(f"[trading] get_order_book failed for {token_id}: {exc}")
        return md

    asks = _book_levels(book, "asks")
    bids = _book_levels(book, "bids")
    if asks:
        md.best_ask = min(p for p, _ in asks)
    if bids:
        md.best_bid = max(p for p, _ in bids)
    if md.best_ask is not None and md.best_bid is not None:
        md.mid = (md.best_ask + md.best_bid) / 2

    try:
        raw = client.get_last_trade_price(token_id)
        # V2 returns {"price": "0.67", "side": "SELL"}
        if isinstance(raw, dict):
            md.last_price = _to_float(raw.get("price"))
        else:
            md.last_price = _to_float(raw)
    except Exception:
        pass
    return md


def get_open_orders(client: ClobClient) -> list[dict[str, Any]]:
    """Return the bot's currently open (resting) orders."""
    try:
        orders = client.get_open_orders()
    except Exception as exc:
        print(f"[trading] get_open_orders failed: {exc}")
        return []
    if not isinstance(orders, list):
        return []
    return [_order_to_dict(o) for o in orders]


def get_recent_trades(client: ClobClient) -> list[dict[str, Any]]:
    """Return the bot's recent trades (fills)."""
    try:
        trades = client.get_trades()
    except Exception as exc:
        print(f"[trading] get_trades failed: {exc}")
        return []
    if not isinstance(trades, list):
        return []
    return [_order_to_dict(t) for t in trades]


def get_last_trade_price(client: ClobClient, token_id: str) -> float | None:
    try:
        raw = client.get_last_trade_price(token_id)
        if isinstance(raw, dict):
            return _to_float(raw.get("price"))
        return _to_float(raw)
    except Exception:
        return None


def _order_to_dict(obj: Any) -> dict[str, Any]:
    """Best-effort conversion of an order/trade object to a plain dict."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return {"value": str(obj)}
