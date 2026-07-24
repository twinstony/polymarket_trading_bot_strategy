"""
Configuration loading for the Polymarket CLOB trading bot.

Static secrets (private key, host) and global runtime parameters come from
environment variables / .env. Per-strategy parameters are created at runtime
via interactive CLI setup and kept in memory only — nothing is persisted to
disk, ensuring a clean state on every startup.

CONDITIONAL_ENTRY is controlled exclusively by .env and applies globally to
all strategies.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

# Load .env once at import time.
load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float | None) -> float | None:
    raw = _env(name)
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _parse_json_list(name: str) -> list[dict[str, Any]]:
    raw = _env(name)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} is not valid JSON: {exc}") from exc
    return []


@dataclass
class StrategyConfig:
    """Per-strategy configuration (memory-only, never persisted).

    One strategy = one outcome token + independent trading parameters.
    Bilateral betting = two StrategyConfig instances (one per outcome).
    """

    token_id: str
    label: str
    entry_price: float
    exit_price: float
    share_amount: float
    take_profit_pct: float | None = None  # e.g. 0.55 = +55% triggers exit
    stop_loss_pct: float | None = None    # e.g. 0.10 = -10% triggers exit
    enabled: bool = True

    def display(self) -> str:
        return self.label or self.token_id


@dataclass
class TelegramBot:
    """A push-only notification bot targeting a single chat."""

    token: str
    chat_id: str
    enabled: bool = True


@dataclass
class Webhook:
    url: str
    enabled: bool = True


@dataclass
class Config:
    # Static / wallet
    private_key: str = ""
    host: str = "https://clob.polymarket.com"
    chain_id: int = 137
    signature_type: int = 0
    funder: str = ""
    clob_api_key: str = ""
    clob_api_secret: str = ""
    clob_api_passphrase: str = ""

    # Global runtime parameters
    trading_enabled: bool = True
    poll_interval: int = 30
    status_every_cycles: int = 20
    # CONDITIONAL_ENTRY is controlled exclusively by .env — config.json does
    # NOT override this. true  = wait until best_ask <= entry_price to place
    # the BUY; false = place the GTC limit BUY immediately.
    conditional_entry: bool = True

    # CLI setup defaults (from .env, used as starting values in prompts)
    default_entry_price: float = 0.50
    default_exit_price: float = 0.55
    default_share_amount: float = 10.0
    default_take_profit_pct: float | None = None
    default_stop_loss_pct: float | None = None

    # Notifications
    telegram_bots: list[TelegramBot] = field(default_factory=list)
    webhooks: list[Webhook] = field(default_factory=list)

    # --- loading -----------------------------------------------------------
    @classmethod
    def load(cls) -> "Config":
        cfg = cls(
            private_key=_env("PRIVATE_KEY"),
            host=_env("POLY_HOST", "https://clob.polymarket.com") or "https://clob.polymarket.com",
            chain_id=_env_int("CHAIN_ID", 137),
            signature_type=_env_int("SIGNATURE_TYPE", 0),
            funder=_env("FUNDER"),
            clob_api_key=_env("CLOB_API_KEY"),
            clob_api_secret=_env("CLOB_SECRET"),
            clob_api_passphrase=_env("CLOB_PASSPHRASE"),
            trading_enabled=_env_bool("TRADING_ENABLED", True),
            poll_interval=_env_int("POLL_INTERVAL", 30),
            status_every_cycles=_env_int("STATUS_EVERY_CYCLES", 20),
            conditional_entry=_env_bool("CONDITIONAL_ENTRY", True),
            default_entry_price=_env_float("ENTRY_PRICE", 0.50) or 0.50,
            default_exit_price=_env_float("EXIT_PRICE", 0.55) or 0.55,
            default_share_amount=_env_float("SHARE_AMOUNT", 10.0) or 10.0,
            default_take_profit_pct=_env_float("TAKE_PROFIT_PCT", None),
            default_stop_loss_pct=_env_float("STOP_LOSS_PCT", None),
        )

        # Telegram notification bots
        cfg.telegram_bots = [
            TelegramBot(
                token=str(b.get("token", "")),
                chat_id=str(b.get("chat_id", "")),
                enabled=bool(b.get("enabled", True)),
            )
            for b in _parse_json_list("TELEGRAM_BOTS")
            if b.get("token")
        ]

        # Webhooks
        cfg.webhooks = [
            Webhook(url=str(w.get("url", "")), enabled=bool(w.get("enabled", True)))
            for w in _parse_json_list("WEBHOOKS")
            if w.get("url")
        ]

        return cfg

    # --- helpers -----------------------------------------------------------
    def has_notifications(self) -> bool:
        return bool(self.telegram_bots) or bool(self.webhooks)
