"""
Configuration loading for the Polymarket CLOB trading bot.

Static secrets (private key, host) come from environment variables / .env.
Mutable trading parameters (active market, prices, exit strategy) come from
``config.json`` when present, falling back to environment defaults. Telegram
commands mutate and persist the mutable part so changes survive restarts.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, asdict
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
class Market:
    """A monitored outcome token (e.g. the YES token of a market)."""

    token_id: str
    label: str = ""

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
class TradingParams:
    """Mutable trading parameters persisted to config.json."""

    markets: list[Market] = field(default_factory=list)
    active_market_index: int = 0
    share_amount: float = 10.0
    entry_price: float = 0.50
    exit_price: float = 0.55
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    conditional_entry: bool = True  # True=best_ask≤entry_price才下单; False=立即挂限价单

    def active_market(self) -> Market | None:
        if not self.markets:
            return None
        idx = self.active_market_index
        if idx < 0 or idx >= len(self.markets):
            return self.markets[0]
        return self.markets[idx]


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

    # Runtime
    trading_enabled: bool = True
    poll_interval: int = 30
    status_every_cycles: int = 20
    config_file: str = "config.json"

    # Notifications
    telegram_bots: list[TelegramBot] = field(default_factory=list)
    telegram_interactive_token: str = ""
    telegram_allowed_user_ids: set[int] = field(default_factory=set)
    webhooks: list[Webhook] = field(default_factory=list)

    # Mutable trading parameters
    trading: TradingParams = field(default_factory=TradingParams)

    # Persistence / recovery
    db_path: str = "data/bot_state.sqlite"
    recovery_timeout_sec: int = 15
    archive_retention_days: int = 30
    seen_trades_retention_days: int = 7
    recon_retention_days: int = 90
    archive_every_cycles: int = 100
    remote_check_interval: int = 5

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
            config_file=_env("CONFIG_FILE", "config.json") or "config.json",
            telegram_interactive_token=_env("TELEGRAM_INTERACTIVE_TOKEN"),
            telegram_allowed_user_ids={
                int(uid.strip())
                for uid in _env("TELEGRAM_ALLOWED_USER_IDS").split(",")
                if uid.strip().isdigit()
            },
            db_path=_env("DB_PATH", "data/bot_state.sqlite") or "data/bot_state.sqlite",
            recovery_timeout_sec=_env_int("RECOVERY_TIMEOUT_SEC", 15),
            archive_retention_days=_env_int("ARCHIVE_RETENTION_DAYS", 30),
            seen_trades_retention_days=_env_int("SEEN_TRADES_RETENTION_DAYS", 7),
            recon_retention_days=_env_int("RECON_RETENTION_DAYS", 90),
            archive_every_cycles=_env_int("ARCHIVE_EVERY_CYCLES", 100),
            remote_check_interval=_env_int("REMOTE_CHECK_INTERVAL", 5),
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

        # Trading parameters: start from env defaults, then overlay config.json.
        cfg.trading = TradingParams(
            share_amount=_env_float("SHARE_AMOUNT", 10.0) or 10.0,
            entry_price=_env_float("ENTRY_PRICE", 0.50) or 0.50,
            exit_price=_env_float("EXIT_PRICE", 0.55) or 0.55,
            take_profit_pct=_env_float("TAKE_PROFIT_PCT", None),
            stop_loss_pct=_env_float("STOP_LOSS_PCT", None),
            conditional_entry=_env_bool("CONDITIONAL_ENTRY", True),
        )
        cfg.trading.markets = _load_markets_from_env()
        cfg._overlay_config_file()
        return cfg

    # --- persistence -------------------------------------------------------
    def save_trading(self) -> None:
        """Persist the mutable trading parameters to config.json."""
        data = {
            "markets": [
                {"token_id": m.token_id, "label": m.label} for m in self.trading.markets
            ],
            "active_market_index": self.trading.active_market_index,
            "share_amount": self.trading.share_amount,
            "entry_price": self.trading.entry_price,
            "exit_price": self.trading.exit_price,
            "take_profit_pct": self.trading.take_profit_pct,
            "stop_loss_pct": self.trading.stop_loss_pct,
            "conditional_entry": self.trading.conditional_entry,
        }
        tmp = self.config_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.config_file)

    def _overlay_config_file(self) -> None:
        if not os.path.exists(self.config_file):
            return
        try:
            with open(self.config_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[config] ignoring unreadable {self.config_file}: {exc}")
            return

        if not isinstance(data, dict):
            return

        markets = data.get("markets")
        if isinstance(markets, list) and markets:
            self.trading.markets = [
                Market(token_id=str(m.get("token_id", "")), label=str(m.get("label", "")))
                for m in markets
                if m.get("token_id")
            ]
        idx = data.get("active_market_index")
        if isinstance(idx, int):
            self.trading.active_market_index = idx

        for key in ("share_amount", "entry_price", "exit_price"):
            val = data.get(key)
            if isinstance(val, (int, float)):
                setattr(self.trading, key, float(val))

        tp = data.get("take_profit_pct")
        if tp is None:
            self.trading.take_profit_pct = None
        elif isinstance(tp, (int, float)):
            self.trading.take_profit_pct = float(tp)

        sl = data.get("stop_loss_pct")
        if sl is None:
            self.trading.stop_loss_pct = None
        elif isinstance(sl, (int, float)):
            self.trading.stop_loss_pct = float(sl)

        tm = data.get("conditional_entry")
        if isinstance(tm, bool):
            self.trading.conditional_entry = tm

    # --- helpers -----------------------------------------------------------
    def has_notifications(self) -> bool:
        return bool(self.telegram_bots) or bool(self.webhooks)

    def has_interactive_bot(self) -> bool:
        return bool(self.telegram_interactive_token)


def _load_markets_from_env() -> list[Market]:
    markets_json = _parse_json_list("MARKETS")
    if markets_json:
        return [
            Market(token_id=str(m.get("token_id", "")), label=str(m.get("label", "")))
            for m in markets_json
            if m.get("token_id")
        ]
    token_id = _env("TOKEN_ID")
    if token_id:
        return [Market(token_id=token_id, label=_env("MARKET_LABEL"))]
    return []


class ConfigGuard:
    """Thread-safe accessor around the mutable trading parameters.

    The runtime loop reads trading params; Telegram commands mutate them.
    All access goes through this guard so updates are atomic and persisted.
    """

    def __init__(self, config: Config):
        self._config = config
        self._lock = threading.RLock()

    @property
    def config(self) -> Config:
        return self._config

    def snapshot(self) -> TradingParams:
        """Return a deep copy of the trading params safe to read off-thread."""
        with self._lock:
            t = self._config.trading
            return TradingParams(
                markets=[Market(m.token_id, m.label) for m in t.markets],
                active_market_index=t.active_market_index,
                share_amount=t.share_amount,
                entry_price=t.entry_price,
                exit_price=t.exit_price,
                take_profit_pct=t.take_profit_pct,
                stop_loss_pct=t.stop_loss_pct,
                conditional_entry=t.conditional_entry,
            )

    def active_market(self) -> Market | None:
        with self._lock:
            return self._config.trading.active_market()

    def set_active_market(self, index: int) -> Market | None:
        with self._lock:
            t = self._config.trading
            if not t.markets:
                return None
            if index < 0 or index >= len(t.markets):
                return None
            t.active_market_index = index
            self._config.save_trading()
            return t.markets[index]

    def add_or_switch_market(self, token_id: str, label: str = "") -> Market:
        with self._lock:
            t = self._config.trading
            for i, m in enumerate(t.markets):
                if m.token_id == token_id:
                    t.active_market_index = i
                    if label:
                        m.label = label
                    self._config.save_trading()
                    return m
            market = Market(token_id=token_id, label=label)
            t.markets.append(market)
            t.active_market_index = len(t.markets) - 1
            self._config.save_trading()
            return market

    def update(
        self,
        *,
        share_amount: float | None = None,
        entry_price: float | None = None,
        exit_price: float | None = None,
        take_profit_pct: float | None = None,
        stop_loss_pct: float | None = None,
        conditional_entry: bool | None = None,
        clear_tp: bool = False,
        clear_sl: bool = False,
    ) -> TradingParams:
        with self._lock:
            t = self._config.trading
            if share_amount is not None:
                t.share_amount = share_amount
            if entry_price is not None:
                t.entry_price = entry_price
            if exit_price is not None:
                t.exit_price = exit_price
            if take_profit_pct is not None:
                t.take_profit_pct = take_profit_pct
            if stop_loss_pct is not None:
                t.stop_loss_pct = stop_loss_pct
            if conditional_entry is not None:
                t.conditional_entry = conditional_entry
            if clear_tp:
                t.take_profit_pct = None
            if clear_sl:
                t.stop_loss_pct = None
            self._config.save_trading()
            return self.snapshot()
