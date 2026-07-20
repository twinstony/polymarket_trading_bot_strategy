#!/usr/bin/env python3
"""Place layered BUY orders and protect filled layers with SELL orders."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
import trading  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-slug", required=True)
    parser.add_argument("--token-id", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--exit-price", type=float, required=True)
    parser.add_argument(
        "--layer",
        action="append",
        required=True,
        help="ENTRY_PRICE:USDC_NOTIONAL, for example 0.35:50",
    )
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--poll-interval", type=int, default=20)
    parser.add_argument("--place-only", action="store_true")
    parser.add_argument("--monitor-only", action="store_true")
    parser.add_argument(
        "--skip-market-check",
        action="store_true",
        help="Skip Gamma accepting-orders check after verifying market status separately.",
    )
    args = parser.parse_args()

    state_path = Path(args.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    layers = [_parse_layer(raw) for raw in args.layer]
    cfg = Config.load()
    client = trading.init_client(cfg)

    if not args.monitor_only:
        if args.skip_market_check:
            print("[layered] skipped Gamma market check; use only after manual verification")
        else:
            _assert_market_accepting_orders(args.market_slug)
        state = _place_layers(
            client=client,
            cfg=cfg,
            token_id=args.token_id,
            label=args.label,
            market_slug=args.market_slug,
            exit_price=args.exit_price,
            layers=layers,
        )
        _save_state(state_path, state)
        if args.place_only:
            return 0
    else:
        state = _load_state(state_path)

    print("[layered] monitor started")
    while True:
        try:
            state = _load_state(state_path)
            changed = _ensure_exit_coverage(client, cfg, state)
            if changed:
                _save_state(state_path, state)
        except Exception as exc:  # noqa: BLE001
            print(f"[layered] monitor cycle failed; will retry: {exc}")
        time.sleep(max(3, args.poll_interval))


def _parse_layer(raw: str) -> dict[str, float]:
    price_raw, usdc_raw = raw.split(":", 1)
    entry_price = float(price_raw)
    usdc = float(usdc_raw)
    shares = _floor_size(usdc / entry_price)
    return {"entry_price": entry_price, "usdc": usdc, "shares": shares}


def _assert_market_accepting_orders(slug: str) -> None:
    url = "https://gamma-api.polymarket.com/markets"
    markets = _get_json_with_retries(url, params={"slug": slug})
    if not markets:
        raise RuntimeError(f"market slug not found: {slug}")
    market = markets[0]
    if market.get("closed") or not market.get("active") or not market.get("acceptingOrders"):
        raise RuntimeError(
            "market is not accepting orders: "
            f"active={market.get('active')} closed={market.get('closed')} "
            f"acceptingOrders={market.get('acceptingOrders')}"
        )


def _place_layers(
    *,
    client,
    cfg: Config,
    token_id: str,
    label: str,
    market_slug: str,
    exit_price: float,
    layers: list[dict[str, float]],
) -> dict[str, Any]:
    baseline_size, baseline_avg = _get_portfolio_position(cfg, token_id)
    baseline_exit_remaining = _matching_open_order_remaining(
        client, token_id, "SELL", exit_price
    )

    state: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "market_slug": market_slug,
        "token_id": token_id,
        "label": label,
        "exit_price": exit_price,
        "baseline_position_size": baseline_size,
        "baseline_position_avg": baseline_avg,
        "baseline_exit_remaining": baseline_exit_remaining,
        "layers": [],
        "exit_order_ids": [],
    }

    for layer in layers:
        existing = _has_matching_open_order(
            client, token_id, "BUY", layer["entry_price"], layer["shares"]
        )
        if existing is None:
            raise RuntimeError("could not confirm open orders before entry")
        layer_state = dict(layer)
        if existing:
            print(
                "[layered] skip BUY, matching open order already exists "
                f"{layer['shares']} @ {layer['entry_price']}"
            )
            layer_state["status"] = "existing_open_order"
        else:
            print(
                "[layered] placing BUY "
                f"{layer['shares']} @ {layer['entry_price']} ({layer['usdc']} USDC)"
            )
            response = trading.enter_position(
                client, token_id, "BUY", layer["shares"], layer["entry_price"]
            )
            order_id = _order_id(response)
            layer_state["status"] = "submitted"
            layer_state["order_id"] = order_id
        state["layers"].append(layer_state)

    return state


def _ensure_exit_coverage(client, cfg: Config, state: dict[str, Any]) -> bool:
    token_id = state["token_id"]
    exit_price = float(state["exit_price"])
    baseline_exit = float(state.get("baseline_exit_remaining") or 0.0)
    target_size = sum(float(layer["shares"]) for layer in state.get("layers", []))

    position_size, avg_price = _get_portfolio_position(cfg, token_id)
    managed_size = _tracked_buy_fill_size(client, token_id, state, target_size)
    exit_remaining = _matching_open_order_remaining(client, token_id, "SELL", exit_price)
    protected_size = max(0.0, exit_remaining - baseline_exit)
    missing = _floor_size(managed_size - protected_size)

    print(
        "[layered] status "
        f"position={position_size} avg={avg_price} managed={managed_size} "
        f"managed_source=tracked_order_ids "
        f"sell_protected={protected_size} missing_sell={missing}"
    )

    if missing <= 0.05:
        return False

    print(f"[layered] placing protective SELL {missing} @ {exit_price}")
    response = trading.exit_position(client, token_id, "SELL", missing, exit_price)
    order_id = _order_id(response)
    if order_id:
        state.setdefault("exit_order_ids", []).append(order_id)
    return True


def _tracked_buy_fill_size(
    client,
    token_id: str,
    state: dict[str, Any],
    target_size: float,
) -> float:
    """Return filled size from this runner's own BUY order ids only.

    Do not infer managed fills from portfolio position deltas. A manual web UI
    buy can increase the same token position and must not trigger an automatic
    protective sell from an old runner state.
    """
    layer_order_ids = {
        str(layer.get("order_id") or "")
        for layer in state.get("layers", [])
        if layer.get("order_id")
    }
    if not layer_order_ids:
        return 0.0

    open_order_fill = 0.0
    try:
        orders = client.get_open_orders()
    except Exception:
        orders = []
    for order in orders or []:
        if not isinstance(order, dict):
            order = trading._order_to_dict(order)
        if _object_order_id(order) not in layer_order_ids:
            continue
        order_token = str(
            order.get("asset_id") or order.get("token_id") or order.get("market") or ""
        )
        order_side = str(order.get("side") or "").upper()
        if order_token == token_id and order_side == "BUY":
            open_order_fill += _to_float(order.get("size_matched")) or 0.0

    trade_fill = 0.0
    try:
        trades = client.get_trades()
    except Exception:
        trades = []
    for trade in trades or []:
        if not isinstance(trade, dict):
            trade = trading._order_to_dict(trade)
        if not _trade_matches_order_ids(trade, layer_order_ids):
            continue
        trade_token = str(
            trade.get("asset_id")
            or trade.get("token_id")
            or trade.get("asset")
            or trade.get("market")
            or ""
        )
        if trade_token and trade_token != token_id:
            continue
        side = str(trade.get("side") or trade.get("taker_side") or "").upper()
        if side and side != "BUY":
            continue
        trade_fill += _trade_size(trade)

    return _floor_size(min(target_size, max(open_order_fill, trade_fill)))


def _get_portfolio_position(cfg: Config, token_id: str) -> tuple[float, float]:
    if not cfg.funder:
        return (0.0, 0.0)
    data = _get_json_with_retries(
        "https://data-api.polymarket.com/positions",
        params={"user": cfg.funder, "limit": 200, "sizeThreshold": 0},
    )
    items = data if isinstance(data, list) else []
    for item in items:
        item_token_id = str(
            item.get("asset") or item.get("assetId") or item.get("tokenId") or ""
        )
        if item_token_id == token_id:
            return (
                float(item.get("size") or 0.0),
                float(item.get("avgPrice") or 0.0),
            )
    return (0.0, 0.0)


def _get_json_with_retries(
    url: str,
    *,
    params: dict[str, Any],
    attempts: int = 4,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = httpx.get(url, params=params, timeout=20)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(f"[layered] http check failed attempt={attempt}: {exc}")
            time.sleep(2)
    raise RuntimeError(f"http check failed after {attempts} attempts: {last_error}")


def _has_matching_open_order(
    client,
    token_id: str,
    side: str,
    price: float,
    size: float,
) -> bool | None:
    try:
        orders = client.get_open_orders()
    except Exception:
        return None
    for order in orders or []:
        if not isinstance(order, dict):
            order = trading._order_to_dict(order)
        order_token = str(
            order.get("asset_id") or order.get("token_id") or order.get("market") or ""
        )
        order_side = str(order.get("side") or "").upper()
        order_price = _to_float(order.get("price"))
        order_size = _to_float(order.get("original_size") or order.get("size"))
        if order_token != token_id or order_side != side.upper():
            continue
        if _close_enough(order_price, price, 0.005, 0.02) and _close_enough(
            order_size, size, 0.05, 0.02
        ):
            return True
    return False


def _matching_open_order_remaining(
    client,
    token_id: str,
    side: str,
    price: float,
) -> float:
    orders = client.get_open_orders()
    total = 0.0
    for order in orders or []:
        if not isinstance(order, dict):
            order = trading._order_to_dict(order)
        order_token = str(
            order.get("asset_id") or order.get("token_id") or order.get("market") or ""
        )
        order_side = str(order.get("side") or "").upper()
        order_price = _to_float(order.get("price"))
        if order_token != token_id or order_side != side.upper():
            continue
        if not _close_enough(order_price, price, 0.005, 0.02):
            continue
        original_size = _to_float(order.get("original_size") or order.get("size")) or 0.0
        matched_size = _to_float(order.get("size_matched")) or 0.0
        total += max(0.0, original_size - matched_size)
    return total


def _order_id(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    return str(response.get("orderID") or response.get("order_id") or "")


def _object_order_id(obj: dict[str, Any]) -> str:
    return str(obj.get("id") or obj.get("orderID") or obj.get("order_id") or "")


def _trade_matches_order_ids(trade: dict[str, Any], order_ids: set[str]) -> bool:
    for key in ("order_id", "maker_order_id", "taker_order_id", "orderID"):
        if str(trade.get(key) or "") in order_ids:
            return True
    return False


def _trade_size(trade: dict[str, Any]) -> float:
    for key in ("size", "amount", "matched_size", "share_size"):
        value = _to_float(trade.get(key))
        if value is not None:
            return value
    return 0.0


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _close_enough(actual: float | None, expected: float, min_abs: float, rel: float) -> bool:
    if actual is None:
        return False
    return abs(actual - expected) <= max(min_abs, abs(expected) * rel)


def _floor_size(value: float) -> float:
    return math.floor(max(0.0, value) * 100) / 100


if __name__ == "__main__":
    raise SystemExit(main())
