"""
Strategy logic for Polymarket trading (Python port of strategy.js).

Entry / exit decisions are intentionally small and self-contained so they can
be customised without touching the rest of the bot. The defaults below are the
sensible, working interpretation of the original JavaScript stubs:

* ``should_enter``  -> enter when the best ask is at or below our entry price
  (we can buy shares at our desired limit). Mirrors the original
  "Enter if price is below <threshold>" comment, using ``entry_price`` as the
  threshold.
* ``should_exit``   -> exit when any of these hold:
    - take-profit target reached (profit >= take_profit_pct)
    - stop-loss threshold breached (loss >= stop_loss_pct)
    - best bid reaches the configured ``exit_price`` (original EXIT_PRICE rule)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MarketData:
    """Live market snapshot used by the strategy."""

    token_id: str
    best_ask: float | None = None
    best_bid: float | None = None
    mid: float | None = None
    last_price: float | None = None

    @property
    def has_book(self) -> bool:
        return self.best_ask is not None or self.best_bid is not None


@dataclass
class PositionData:
    """Current position in a single outcome token."""

    token_id: str
    size: float = 0.0
    avg_price: float = 0.0  # average entry price of held shares

    @property
    def has_position(self) -> bool:
        return self.size > 0


def should_enter(market_data: MarketData, entry_price: float) -> bool:
    """Return True when the bot should place a BUY limit order.

    Default rule: the best ask is at or below our desired entry price, i.e. we
    can realistically be filled at ``entry_price``. Replace this body with
    custom logic (volume / spread / probability based) if needed.
    """
    print("[strategy] Checking entry logic...")
    if not market_data.has_book or entry_price is None:
        return False
    ask = market_data.best_ask
    if ask is None:
        return False
    # Enter when the market is offering shares at or below our limit.
    return ask <= entry_price


def should_exit(
    position: PositionData,
    market_data: MarketData,
    exit_price: float | None = None,
    take_profit_pct: float | None = None,
    stop_loss_pct: float | None = None,
) -> bool:
    """Return True when the bot should place a SELL limit order to close out.

    Combines the original fixed ``exit_price`` rule with the new optional
    take-profit / stop-loss percentages (expressed as fractions, e.g. 0.10 =
    10%). A rule is only evaluated when its threshold is configured.
    """
    print("[strategy] Checking exit logic...")
    if not position.has_position:
        return False

    bid = market_data.best_bid
    avg = position.avg_price or 0.0

    # Take-profit: realised profit fraction has reached the target.
    if take_profit_pct is not None and avg > 0 and bid is not None:
        profit_pct = (bid - avg) / avg
        if profit_pct >= take_profit_pct:
            print(f"[strategy] take-profit hit ({profit_pct:.2%} >= {take_profit_pct:.2%})")
            return True

    # Stop-loss: realised loss fraction has reached the limit.
    if stop_loss_pct is not None and avg > 0 and bid is not None:
        loss_pct = (avg - bid) / avg
        if loss_pct >= stop_loss_pct:
            print(f"[strategy] stop-loss hit ({loss_pct:.2%} >= {stop_loss_pct:.2%})")
            return True

    # Original rule: exit when the best bid reaches the configured exit price.
    if exit_price is not None and bid is not None and bid >= exit_price:
        print(f"[strategy] exit-price reached ({bid} >= {exit_price})")
        return True

    return False
