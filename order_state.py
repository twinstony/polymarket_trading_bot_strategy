"""
Order state machine, intent id generation, and order-related exceptions.

The state machine enforces the lifecycle of an order:

    PENDING -> PLACED -> PARTIAL -> FILLED
    PENDING -> REJECTED / TIMEOUT_UNCONFIRMED
    PLACED/PARTIAL -> CANCELED (remote cancel detected by reconciliation)
    TIMEOUT_UNCONFIRMED -> PLACED/PARTIAL/FILLED (recon found order)
    TIMEOUT_UNCONFIRMED -> CANCELED (recon confirmed no order)

Terminal states (FILLED / CANCELED / REJECTED) cannot transition again.

Intent ids (UUID v4) are written to the ``orders`` table BEFORE the API call
so that a crash between intent recording and API response leaves a recoverable
PENDING/TIMEOUT_UNCONFIRMED row rather than a silent gap.
"""

from __future__ import annotations

import uuid
from enum import Enum


class OrderStatus(str, Enum):
    """Order lifecycle states persisted in ``orders.status``."""

    PENDING = "PENDING"                    # Intent recorded, API call not yet completed
    PLACED = "PLACED"                      # API returned order_id, resting on book
    PARTIAL = "PARTIAL"                    # Partially filled, still resting
    FILLED = "FILLED"                      # Fully filled (terminal)
    CANCELED = "CANCELED"                  # Canceled by exchange or user (terminal)
    REJECTED = "REJECTED"                  # API rejected the order (terminal)
    TIMEOUT_UNCONFIRMED = "TIMEOUT_UNCONFIRMED"  # Network timeout, status unknown


class OrderEvent(str, Enum):
    """Events that drive order state transitions."""

    RECORD_INTENT = "RECORD_INTENT"            # (initial) -> PENDING
    ORDER_PLACED = "ORDER_PLACED"              # PENDING -> PLACED
    API_TIMEOUT = "API_TIMEOUT"                # PENDING -> TIMEOUT_UNCONFIRMED
    API_REJECTED = "API_REJECTED"              # PENDING -> REJECTED
    PARTIAL_FILL = "PARTIAL_FILL"              # PLACED -> PARTIAL
    FULL_FILL = "FULL_FILL"                    # PLACED/PARTIAL -> FILLED
    REMOTE_CANCEL = "REMOTE_CANCEL"            # PLACED/PARTIAL -> CANCELED
    RECON_FOUND_ORDER = "RECON_FOUND_ORDER"    # TIMEOUT_UNCONFIRMED -> PLACED/PARTIAL/FILLED
    RECON_NO_ORDER = "RECON_NO_ORDER"          # TIMEOUT_UNCONFIRMED -> CANCELED


# Terminal states: no further transitions allowed.
_TERMINAL_STATES = frozenset({
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.REJECTED,
})

# Valid transitions: (current_status, event) -> new_status.
# RECON_FOUND_ORDER maps to PLACED by default; caller upgrades to PARTIAL/FILLED
# when reconciliation reports partial/full fills.
_TRANSITIONS: dict[tuple[OrderStatus, OrderEvent], OrderStatus] = {
    (OrderStatus.PENDING, OrderEvent.ORDER_PLACED): OrderStatus.PLACED,
    (OrderStatus.PENDING, OrderEvent.API_TIMEOUT): OrderStatus.TIMEOUT_UNCONFIRMED,
    (OrderStatus.PENDING, OrderEvent.API_REJECTED): OrderStatus.REJECTED,
    (OrderStatus.PLACED, OrderEvent.PARTIAL_FILL): OrderStatus.PARTIAL,
    (OrderStatus.PLACED, OrderEvent.FULL_FILL): OrderStatus.FILLED,
    (OrderStatus.PLACED, OrderEvent.REMOTE_CANCEL): OrderStatus.CANCELED,
    (OrderStatus.PARTIAL, OrderEvent.FULL_FILL): OrderStatus.FILLED,
    (OrderStatus.PARTIAL, OrderEvent.REMOTE_CANCEL): OrderStatus.CANCELED,
    (OrderStatus.TIMEOUT_UNCONFIRMED, OrderEvent.RECON_FOUND_ORDER): OrderStatus.PLACED,
    (OrderStatus.TIMEOUT_UNCONFIRMED, OrderEvent.RECON_NO_ORDER): OrderStatus.CANCELED,
}


class IllegalTransitionError(ValueError):
    """Raised when a state transition is not allowed (e.g. terminal -> anything)."""


def transition(current: OrderStatus, event: OrderEvent) -> OrderStatus:
    """Return the new status after applying ``event`` to ``current``.

    Raises ``IllegalTransitionError`` for unknown or terminal-state transitions.
    """
    if current in _TERMINAL_STATES:
        raise IllegalTransitionError(
            f"cannot transition from terminal state {current.value} via {event.value}"
        )
    key = (current, event)
    new = _TRANSITIONS.get(key)
    if new is None:
        raise IllegalTransitionError(
            f"no transition defined for ({current.value}, {event.value})"
        )
    return new


def is_terminal(status: OrderStatus) -> bool:
    """Return True if ``status`` is a terminal state (no further transitions)."""
    return status in _TERMINAL_STATES


def is_active(status: OrderStatus) -> bool:
    """Return True if ``status`` represents an unfinished order needing monitoring."""
    return status in (
        OrderStatus.PENDING,
        OrderStatus.PLACED,
        OrderStatus.PARTIAL,
        OrderStatus.TIMEOUT_UNCONFIRMED,
    )


class OrderTimeoutError(Exception):
    """Raised when an order placement times out or the server returns 5xx.

    The caller cannot know whether the exchange accepted the order, so the
    intent must be marked TIMEOUT_UNCONFIRMED and reconciled on the next cycle.
    """


class OrderRateLimitError(Exception):
    """Raised when the exchange returns 429 (rate limited).

    The caller should back off (default 60s) and retry the same intent_id.
    """


def new_intent_id() -> str:
    """Generate a fresh intent id (UUID v4 hex string).

    The intent id is written to ``orders.intent_id`` BEFORE the API call and
    serves as an idempotency key: re-using the same id on retry prevents
    duplicate order placement after a crash or timeout.
    """
    return uuid.uuid4().hex

