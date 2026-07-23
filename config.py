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
class Strategy:
    """A single trading strategy bound to one outcome token.

    Each strategy is an independent unit: it has its own token_id, prices,
    order size, and runtime state. Multiple strategies run concurrently as
    separate StrategyRuntime instances. A "dual-sided bet" is simply two
    Strategy entries (one per outcome token).
    """

    token_id: str
    label: str = ""
    outcome_name: str = ""  # "Yes" / "No" / team name
    entry_price: float = 0.50
    exit_price: float = 0.55
    share_amount: float = 10.0
    enabled: bool = True
    strategy_id: str = ""  # unique id; auto-generated from token_id if empty
    # 每策略独立的止盈止损（None=关闭）。全局 TradingParams 的同名字段仅作为
    # 交互式 setup 时的默认值 fallback。
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None

    def display(self) -> str:
        return self.label or self.token_id

    def __post_init__(self):
        if not self.strategy_id:
            # Use first 12 chars of token_id as a readable unique id
            self.strategy_id = self.token_id[:12] if self.token_id else ""


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

    # Unified strategy list: each entry is an independent trading unit.
    # Replaces the old markets/dual_markets split. A single-sided bet is one
    # Strategy; a dual-sided bet is two Strategy entries.
    strategies: list[Strategy] = field(default_factory=list)
    # Global defaults (used as fallback when a Strategy field is not set, and
    # for take_profit/stop_loss which are still global).
    share_amount: float = 10.0
    entry_price: float = 0.50
    exit_price: float = 0.55
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    conditional_entry: bool = True  # True=best_ask≤entry_price才下单; False=立即挂限价单

    def get_strategy(self, strategy_id: str) -> Strategy | None:
        for s in self.strategies:
            if s.strategy_id == strategy_id:
                return s
        return None

    def list_enabled(self) -> list[Strategy]:
        return [s for s in self.strategies if s.enabled and s.token_id]


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
        # Strategies are in-memory only: each process start begins with a
        # clean strategy list, populated via interactive setup or Telegram
        # /strategy add. No env-var or config.json loading for strategies.
        cfg._overlay_config_file()
        return cfg

    # --- persistence -------------------------------------------------------
    def save_trading(self) -> None:
        """Persist global trading parameters to config.json.

        NOTE: strategies list is intentionally NOT persisted — it lives in
        memory only. Each process start begins with a clean strategy list
        (from .env or interactive setup). This prevents stale/duplicate
        strategies from accumulating across restarts.

        NOTE: conditional_entry is intentionally NOT persisted — it is
        controlled solely by .env CONDITIONAL_ENTRY so that the mode is
        deterministic on each restart. /triggermode changes are in-memory
        only and reset on restart.
        """
        data = {
            "share_amount": self.trading.share_amount,
            "entry_price": self.trading.entry_price,
            "exit_price": self.trading.exit_price,
            "take_profit_pct": self.trading.take_profit_pct,
            "stop_loss_pct": self.trading.stop_loss_pct,
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

        # Global params
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

        # NOTE: conditional_entry is intentionally NOT loaded from config.json
        # — it is controlled solely by .env CONDITIONAL_ENTRY so that the mode
        # is deterministic on each restart. /triggermode changes are in-memory
        # only and reset on restart.

        # NOTE: strategies are NOT loaded from config.json — they are purely
        # in-memory. Each process start begins with a clean strategy list
        # (from .env TOKEN_ID/MARKETS or interactive setup). This prevents
        # stale/duplicate strategies from accumulating across restarts.

    # --- helpers -----------------------------------------------------------
    def has_notifications(self) -> bool:
        return bool(self.telegram_bots) or bool(self.webhooks)

    def has_interactive_bot(self) -> bool:
        return bool(self.telegram_interactive_token)


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
                strategies=[
                    Strategy(
                        token_id=s.token_id,
                        label=s.label,
                        outcome_name=s.outcome_name,
                        entry_price=s.entry_price,
                        exit_price=s.exit_price,
                        share_amount=s.share_amount,
                        enabled=s.enabled,
                        strategy_id=s.strategy_id,
                        take_profit_pct=s.take_profit_pct,
                        stop_loss_pct=s.stop_loss_pct,
                    )
                    for s in t.strategies
                ],
                share_amount=t.share_amount,
                entry_price=t.entry_price,
                exit_price=t.exit_price,
                take_profit_pct=t.take_profit_pct,
                stop_loss_pct=t.stop_loss_pct,
                conditional_entry=t.conditional_entry,
            )

    # --- global params ----------------------------------------------------
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

    # --- strategy management ----------------------------------------------
    def get_strategy(self, strategy_id: str) -> Strategy | None:
        with self._lock:
            return self._config.trading.get_strategy(strategy_id)

    def list_strategies(self) -> list[Strategy]:
        with self._lock:
            return list(self._config.trading.strategies)

    def list_enabled(self) -> list[Strategy]:
        with self._lock:
            return self._config.trading.list_enabled()

    def add_strategy(self, strategy: Strategy) -> Strategy:
        """Add a new strategy. If token_id already exists, update it instead.

        NOTE: strategies are in-memory only, not persisted to config.json.
        """
        with self._lock:
            t = self._config.trading
            for i, s in enumerate(t.strategies):
                if s.token_id == strategy.token_id:
                    t.strategies[i] = strategy
                    return strategy
            t.strategies.append(strategy)
            return strategy

    def remove_strategy(self, strategy_id: str) -> bool:
        """Remove a strategy by id. In-memory only, not persisted."""
        with self._lock:
            t = self._config.trading
            before = len(t.strategies)
            t.strategies = [s for s in t.strategies if s.strategy_id != strategy_id]
            return len(t.strategies) < before

    def update_strategy(
        self,
        strategy_id: str,
        *,
        entry_price: float | None = None,
        exit_price: float | None = None,
        share_amount: float | None = None,
        enabled: bool | None = None,
        label: str | None = None,
        take_profit_pct: float | None = None,
        stop_loss_pct: float | None = None,
        clear_tp: bool = False,
        clear_sl: bool = False,
    ) -> Strategy | None:
        """Update a strategy's params. In-memory only, not persisted."""
        with self._lock:
            t = self._config.trading
            s = t.get_strategy(strategy_id)
            if s is None:
                return None
            if entry_price is not None:
                s.entry_price = entry_price
            if exit_price is not None:
                s.exit_price = exit_price
            if share_amount is not None:
                s.share_amount = share_amount
            if enabled is not None:
                s.enabled = enabled
            if label is not None:
                s.label = label
            if take_profit_pct is not None:
                s.take_profit_pct = take_profit_pct
            if stop_loss_pct is not None:
                s.stop_loss_pct = stop_loss_pct
            if clear_tp:
                s.take_profit_pct = None
            if clear_sl:
                s.stop_loss_pct = None
            return s
