"""
SQLite persistence layer for the Polymarket trading bot.

Design:
- WAL journal mode for crash consistency (writes are appended to a WAL file
  before being checkpointed into the main DB, so a crash never corrupts
  the main DB).
- Single connection guarded by ``threading.RLock`` (SQLite supports
  concurrent readers but serialised writers; the lock avoids
  ``database is locked`` errors between bot threads).
- ``BEGIN IMMEDIATE`` transactions for multi-statement writes so that a
  crash mid-transaction rolls back the entire group.
- High-level DAO methods return dataclasses; raw rows are not leaked.

Schema (8 tables + 2 archive tables):
- sessions            : one row per process start
- orders              : order lifecycle (event source)
- fills               : immutable fill events
- positions           : position snapshot with baselines
- seen_trade_ids      : trade dedup (truncated aggressively)
- failed_attempts     : SELL backoff counters
- reconciliations     : audit log for startup/live reconciliation
- archived_orders     : cold storage for closed orders
- archived_fills      : cold storage for old fills
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from order_state import OrderStatus, is_active


def _utcnow_iso() -> str:
    """Return current UTC time as ISO8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Dataclasses (row ↔ object mapping)
# --------------------------------------------------------------------------- #
@dataclass
class OrderRecord:
    """One row in the ``orders`` table."""

    intent_id: str
    session_id: str
    token_id: str
    side: str                       # BUY / SELL
    price: float
    size: float
    status: str
    label: str = ""
    order_id: Optional[str] = None
    filled_size: float = 0.0
    pair_intent_id: Optional[str] = None
    created_at: str = field(default_factory=_utcnow_iso)
    placed_at: Optional[str] = None
    closed_at: Optional[str] = None
    raw_response: Optional[str] = None
    notes: Optional[str] = None


@dataclass
class FillRecord:
    """One row in the ``fills`` table (immutable event)."""

    trade_id: str
    token_id: str
    side: str
    size: float
    price: float
    recorded_at: str = field(default_factory=_utcnow_iso)
    order_id: Optional[str] = None
    intent_id: Optional[str] = None
    matched_at: Optional[str] = None
    raw_trade: Optional[str] = None


@dataclass
class PositionRecord:
    """One row in the ``positions`` table (snapshot + baselines)."""

    token_id: str
    label: str = ""
    size: float = 0.0
    avg_price: float = 0.0
    baseline_position_size: float = 0.0
    baseline_position_avg: float = 0.0
    baseline_exit_remaining: float = 0.0
    entry_attempted: int = 0
    updated_at: str = field(default_factory=_utcnow_iso)


# --------------------------------------------------------------------------- #
# Schema DDL
# --------------------------------------------------------------------------- #
_SCHEMA_SQL = """
-- 1. sessions: process-start sessions
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    recovery_mode   TEXT NOT NULL,
    recovery_status TEXT NOT NULL,
    last_cycle      INTEGER DEFAULT 0,
    notes           TEXT
);

-- 2. orders: order lifecycle (core event-source table)
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id       TEXT NOT NULL UNIQUE,
    order_id        TEXT UNIQUE,
    session_id      TEXT NOT NULL REFERENCES sessions(session_id),
    token_id        TEXT NOT NULL,
    label           TEXT,
    side            TEXT NOT NULL,
    price           REAL NOT NULL,
    size            REAL NOT NULL,
    filled_size     REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    pair_intent_id  TEXT,
    created_at      TEXT NOT NULL,
    placed_at       TEXT,
    closed_at       TEXT,
    raw_response    TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_status   ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_token    ON orders(token_id);
CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id);
CREATE INDEX IF NOT EXISTS idx_orders_intent   ON orders(intent_id);
CREATE INDEX IF NOT EXISTS idx_orders_pair     ON orders(pair_intent_id);

-- 3. fills: immutable fill events
CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        TEXT NOT NULL UNIQUE,
    order_id        TEXT,
    intent_id       TEXT,
    token_id        TEXT NOT NULL,
    side            TEXT NOT NULL,
    size            REAL NOT NULL,
    price           REAL NOT NULL,
    matched_at      TEXT,
    recorded_at     TEXT NOT NULL,
    raw_trade       TEXT
);

CREATE INDEX IF NOT EXISTS idx_fills_token    ON fills(token_id);
CREATE INDEX IF NOT EXISTS idx_fills_order    ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_fills_recorded ON fills(recorded_at);

-- 4. positions: snapshot with baselines
CREATE TABLE IF NOT EXISTS positions (
    token_id                TEXT PRIMARY KEY,
    label                   TEXT,
    size                    REAL NOT NULL DEFAULT 0,
    avg_price               REAL NOT NULL DEFAULT 0,
    baseline_position_size  REAL NOT NULL DEFAULT 0,
    baseline_position_avg   REAL NOT NULL DEFAULT 0,
    baseline_exit_remaining REAL NOT NULL DEFAULT 0,
    entry_attempted         INTEGER NOT NULL DEFAULT 0,
    updated_at              TEXT NOT NULL
);

-- 5. seen_trade_ids: trade dedup (truncated aggressively)
CREATE TABLE IF NOT EXISTS seen_trade_ids (
    trade_id        TEXT PRIMARY KEY,
    first_seen_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_seen_first_seen ON seen_trade_ids(first_seen_at);

-- 6. failed_attempts: SELL backoff counters
CREATE TABLE IF NOT EXISTS failed_attempts (
    token_id        TEXT PRIMARY KEY,
    failure_count   INTEGER NOT NULL DEFAULT 0,
    last_failure_at TEXT,
    last_reason     TEXT
);

-- 7. reconciliations: audit log
CREATE TABLE IF NOT EXISTS reconciliations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(session_id),
    run_at          TEXT NOT NULL,
    source          TEXT NOT NULL,
    token_id        TEXT,
    local_state     TEXT,
    remote_state    TEXT,
    mismatch_type   TEXT,
    resolution      TEXT NOT NULL,
    resolved_at     TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_recon_session ON reconciliations(session_id);
CREATE INDEX IF NOT EXISTS idx_recon_run_at  ON reconciliations(run_at);

-- 8. archived_orders / archived_fills: cold storage (same shape as main tables)
CREATE TABLE IF NOT EXISTS archived_orders (
    id              INTEGER PRIMARY KEY,
    intent_id       TEXT,
    order_id        TEXT,
    session_id      TEXT,
    token_id        TEXT,
    label           TEXT,
    side            TEXT,
    price           REAL,
    size            REAL,
    filled_size     REAL,
    status          TEXT,
    pair_intent_id  TEXT,
    created_at      TEXT,
    placed_at       TEXT,
    closed_at       TEXT,
    raw_response    TEXT,
    notes           TEXT,
    archived_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS archived_fills (
    id              INTEGER PRIMARY KEY,
    trade_id        TEXT,
    order_id        TEXT,
    intent_id       TEXT,
    token_id        TEXT,
    side            TEXT,
    size            REAL,
    price           REAL,
    matched_at      TEXT,
    recorded_at     TEXT,
    raw_trade       TEXT,
    archived_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""


# --------------------------------------------------------------------------- #
# Persistence (DAO)
# --------------------------------------------------------------------------- #
class Persistence:
    """Thread-safe SQLite persistence layer.

    All public methods are safe to call from multiple threads. Writes are
    serialised via ``threading.RLock`` and ``BEGIN IMMEDIATE`` transactions.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._db_unavailable = False
        self._unavailable_since_cycle: int | None = None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        """Open the connection, create the schema, set pragmas."""
        db_dir = os.path.dirname(self._db_path)
        if db_dir and not os.path.isdir(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        # detect corrupt DB before connecting
        self._maybe_isolate_corrupt_db()

        self._open_connection()
        self._db_unavailable = False
        self._unavailable_since_cycle = None

    def _open_connection(self) -> None:
        """Create a new SQLite connection and apply pragmas + schema."""
        conn = sqlite3.connect(
            self._db_path,
            timeout=5.0,           # busy timeout (seconds)
            isolation_level=None,  # autocommit; we manage transactions manually
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")  # WAL + NORMAL = safe + fast
        conn.executescript(_SCHEMA_SQL)
        self._conn = conn

    def _reopen(self) -> None:
        """Reopen the connection after a transient failure."""
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass
        self._conn = None
        self._open_connection()
        print("[persistence] connection reopened")

    def close(self) -> None:
        with self._lock:
            if self._conn:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                self._conn.close()
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Persistence not opened; call open() first")
        return self._conn

    def is_db_available(self) -> bool:
        """Return False if DB writes have been failing (runtime may degrade)."""
        return not self._db_unavailable

    def mark_unavailable(self, cycle: int | None = None) -> None:
        """Mark DB as unavailable (called by runtime on persistent write failures)."""
        if not self._db_unavailable:
            self._db_unavailable = True
            self._unavailable_since_cycle = cycle
            print(f"[persistence] DB marked unavailable at cycle {cycle}")

    def try_recover(self, cycle: int | None = None) -> bool:
        """Attempt to recover DB availability. Returns True if recovered."""
        if not self._db_unavailable:
            return True
        try:
            # Test write
            self._reopen()
            self._conn.execute("SELECT 1")
            self._db_unavailable = False
            self._unavailable_since_cycle = None
            print(f"[persistence] DB recovered at cycle {cycle}")
            return True
        except Exception as exc:
            print(f"[persistence] DB recovery failed: {exc}")
            return False

    def _maybe_isolate_corrupt_db(self) -> None:
        """If the DB file exists but is corrupt, rename it aside."""
        if not os.path.exists(self._db_path):
            return
        try:
            probe = sqlite3.connect(self._db_path)
            probe.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            probe.close()
        except sqlite3.DatabaseError as exc:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            corrupt_path = f"{self._db_path}.corrupt.{ts}"
            try:
                os.rename(self._db_path, corrupt_path)
                print(f"[persistence] DB corrupt, isolated to {corrupt_path}: {exc}")
            except OSError as rename_err:
                print(f"[persistence] DB corrupt but could not rename: {rename_err}")

    # -- internal helpers --------------------------------------------------
    def _execute(self, sql: str, params: Iterable[Any] | None = None) -> sqlite3.Cursor:
        """Execute SQL with 3-attempt retry for locked/closed connection.

        Layer 1 of DB exception handling:
        - ``database is locked``: retry up to 3 times with backoff
        - ``closed database``: reopen once and retry
        - Other errors: raise (caller decides whether to mark DB unavailable)
        """
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                with self._lock:
                    conn = self._conn
                    if conn is None:
                        raise sqlite3.ProgrammingError("connection closed")
                    return conn.execute(sql, params or [])
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if "database is locked" in str(exc) and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if "disk I/O error" in str(exc) or "no space" in str(exc):
                    # Disk full / hardware error: propagate to mark DB unavailable
                    raise
                raise
            except sqlite3.ProgrammingError as exc:
                last_exc = exc
                if "closed" in str(exc).lower() and attempt < 1:
                    try:
                        self._reopen()
                        continue
                    except Exception:
                        raise
                raise
        # Should not reach here, but just in case
        raise last_exc  # type: ignore[misc]

    def _executemany(self, sql: str, params_seq: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                with self._lock:
                    conn = self._conn
                    if conn is None:
                        raise sqlite3.ProgrammingError("connection closed")
                    return conn.executemany(sql, params_seq)
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if "database is locked" in str(exc) and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
            except sqlite3.ProgrammingError as exc:
                last_exc = exc
                if "closed" in str(exc).lower() and attempt < 1:
                    try:
                        self._reopen()
                        continue
                    except Exception:
                        raise
                raise
        raise last_exc  # type: ignore[misc]

    def _transaction(self, statements: list[tuple[str, Iterable[Any]]]) -> None:
        """Execute multiple statements inside a single BEGIN IMMEDIATE transaction.

        Each entry in ``statements`` is ``(sql, params)``. If any statement
        raises, the entire transaction is rolled back.
        """
        with self._lock:
            conn = self.conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                for sql, params in statements:
                    conn.execute(sql, params)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # =================================================================== #
    # Sessions
    # =================================================================== #
    def insert_session(
        self,
        session_id: str,
        recovery_mode: str,
        recovery_status: str = "SUCCESS",
        notes: str | None = None,
    ) -> None:
        self._execute(
            "INSERT INTO sessions (session_id, started_at, recovery_mode, recovery_status, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            [session_id, _utcnow_iso(), recovery_mode, recovery_status, notes],
        )

    def update_session_end(self, session_id: str, recovery_status: str, last_cycle: int) -> None:
        self._execute(
            "UPDATE sessions SET ended_at = ?, recovery_status = ?, last_cycle = ? "
            "WHERE session_id = ?",
            [_utcnow_iso(), recovery_status, last_cycle, session_id],
        )

    def update_session_mode(self, session_id: str, recovery_mode: str) -> None:
        """Update the recovery_mode of an existing session (e.g. RESUME -> FRESH_START)."""
        self._execute(
            "UPDATE sessions SET recovery_mode = ? WHERE session_id = ?",
            [recovery_mode, session_id],
        )

    def get_last_session(self) -> sqlite3.Row | None:
        return self._execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

    # =================================================================== #
    # Orders
    # =================================================================== #
    def insert_order(self, rec: OrderRecord) -> None:
        self._execute(
            """
            INSERT INTO orders
                (intent_id, order_id, session_id, token_id, label, side, price, size,
                 filled_size, status, pair_intent_id, created_at, placed_at, closed_at,
                 raw_response, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                rec.intent_id, rec.order_id, rec.session_id, rec.token_id, rec.label,
                rec.side, rec.price, rec.size, rec.filled_size, rec.status,
                rec.pair_intent_id, rec.created_at, rec.placed_at, rec.closed_at,
                rec.raw_response, rec.notes,
            ],
        )

    def update_order_status(
        self,
        intent_id: str,
        status: str,
        *,
        order_id: str | None = None,
        filled_size: float | None = None,
        raw_response: str | None = None,
        notes: str | None = None,
    ) -> None:
        """Transition an order to ``status``, optionally setting order_id / response."""
        sets = ["status = ?"]
        params: list[Any] = [status]

        if order_id is not None:
            sets.append("order_id = ?")
            params.append(order_id)
        if filled_size is not None:
            sets.append("filled_size = ?")
            params.append(filled_size)
        if raw_response is not None:
            sets.append("raw_response = ?")
            params.append(raw_response)
        if notes is not None:
            sets.append("notes = ?")
            params.append(notes)

        # Set placed_at / closed_at based on the new status.
        now = _utcnow_iso()
        if status == OrderStatus.PLACED.value:
            sets.append("placed_at = ?")
            params.append(now)
        elif status in (
            OrderStatus.FILLED.value,
            OrderStatus.CANCELED.value,
            OrderStatus.REJECTED.value,
        ):
            sets.append("closed_at = ?")
            params.append(now)

        params.append(intent_id)
        self._execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE intent_id = ?",
            params,
        )

    def update_order_pair(self, intent_id: str, pair_intent_id: str) -> None:
        self._execute(
            "UPDATE orders SET pair_intent_id = ? WHERE intent_id = ?",
            [pair_intent_id, intent_id],
        )

    def update_order_filled(
        self,
        intent_id: str,
        filled_size_delta: float,
        new_filled_total: float,
        is_full: bool,
    ) -> None:
        """Update filled_size and transition to FILLED if fully filled."""
        now = _utcnow_iso()
        if is_full:
            self._execute(
                "UPDATE orders SET filled_size = ?, status = ?, closed_at = ? "
                "WHERE intent_id = ?",
                [new_filled_total, OrderStatus.FILLED.value, now, intent_id],
            )
        else:
            self._execute(
                "UPDATE orders SET filled_size = ?, status = ? WHERE intent_id = ?",
                [new_filled_total, OrderStatus.PARTIAL.value, intent_id],
            )

    def query_order_by_intent(self, intent_id: str) -> OrderRecord | None:
        row = self._execute(
            "SELECT * FROM orders WHERE intent_id = ?", [intent_id]
        ).fetchone()
        return _row_to_order(row) if row else None

    def query_order_by_order_id(self, order_id: str) -> OrderRecord | None:
        row = self._execute(
            "SELECT * FROM orders WHERE order_id = ?", [order_id]
        ).fetchone()
        return _row_to_order(row) if row else None

    def query_orders_by_order_ids(self, order_ids: set[str]) -> list[OrderRecord]:
        if not order_ids:
            return []
        placeholders = ",".join("?" * len(order_ids))
        rows = self._execute(
            f"SELECT * FROM orders WHERE order_id IN ({placeholders})",
            list(order_ids),
        ).fetchall()
        return [_row_to_order(r) for r in rows]

    def query_orders(
        self,
        *,
        status_in: list[str] | None = None,
        token_id: str | None = None,
        side: str | None = None,
    ) -> list[OrderRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if status_in:
            placeholders = ",".join("?" * len(status_in))
            clauses.append(f"status IN ({placeholders})")
            params.extend(status_in)
        if token_id:
            clauses.append("token_id = ?")
            params.append(token_id)
        if side:
            clauses.append("side = ?")
            params.append(side)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._execute(
            f"SELECT * FROM orders{where} ORDER BY created_at ASC", params
        ).fetchall()
        return [_row_to_order(r) for r in rows]

    def query_unfinished_orders(
        self,
        *,
        token_id: str | None = None,
        side: str | None = None,
    ) -> list[OrderRecord]:
        """Return all orders in an active (non-terminal) state.

        Optional filters by ``token_id`` and ``side`` support the SELL-symmetry
        check ("does this token already have a working SELL intent?").
        """
        active_statuses = [
            OrderStatus.PENDING.value,
            OrderStatus.PLACED.value,
            OrderStatus.PARTIAL.value,
            OrderStatus.TIMEOUT_UNCONFIRMED.value,
        ]
        return self.query_orders(status_in=active_statuses, token_id=token_id, side=side)

    def query_buy_filled_without_sell(self) -> list[OrderRecord]:
        """Return BUY FILLED orders that have no paired SELL (pair_intent_id IS NULL)."""
        rows = self._execute(
            "SELECT * FROM orders WHERE side = 'BUY' AND status = ? "
            "AND pair_intent_id IS NULL",
            [OrderStatus.FILLED.value],
        ).fetchall()
        return [_row_to_order(r) for r in rows]

    def get_last_buy_filled_intent(self, token_id: str) -> str | None:
        """Return intent_id of the most recent BUY FILLED order for a token.

        Used by the SELL path to set ``pair_intent_id`` linking the protective
        SELL to its originating BUY.
        """
        row = self._execute(
            "SELECT intent_id FROM orders WHERE side = 'BUY' AND status = ? "
            "AND token_id = ? ORDER BY closed_at DESC LIMIT 1",
            [OrderStatus.FILLED.value, token_id],
        ).fetchone()
        return row["intent_id"] if row else None

    # =================================================================== #
    # Fills
    # =================================================================== #
    def insert_fill(self, rec: FillRecord) -> bool:
        """Insert a fill record. Returns False if trade_id already exists (dedup)."""
        try:
            self._execute(
                """
                INSERT INTO fills
                    (trade_id, order_id, intent_id, token_id, side, size, price,
                     matched_at, recorded_at, raw_trade)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    rec.trade_id, rec.order_id, rec.intent_id, rec.token_id,
                    rec.side, rec.size, rec.price, rec.matched_at,
                    rec.recorded_at, rec.raw_trade,
                ],
            )
            return True
        except sqlite3.IntegrityError:
            # trade_id UNIQUE constraint violated → already recorded
            return False

    def query_fills_by_order(self, order_id: str) -> list[FillRecord]:
        rows = self._execute(
            "SELECT * FROM fills WHERE order_id = ? ORDER BY recorded_at ASC",
            [order_id],
        ).fetchall()
        return [_row_to_fill(r) for r in rows]

    def query_fills_by_token(self, token_id: str) -> list[FillRecord]:
        rows = self._execute(
            "SELECT * FROM fills WHERE token_id = ? ORDER BY recorded_at ASC",
            [token_id],
        ).fetchall()
        return [_row_to_fill(r) for r in rows]

    # =================================================================== #
    # Positions
    # =================================================================== #
    def upsert_position(self, rec: PositionRecord) -> None:
        self._execute(
            """
            INSERT INTO positions
                (token_id, label, size, avg_price, baseline_position_size,
                 baseline_position_avg, baseline_exit_remaining, entry_attempted,
                 updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(token_id) DO UPDATE SET
                label = excluded.label,
                size = excluded.size,
                avg_price = excluded.avg_price,
                baseline_position_size = excluded.baseline_position_size,
                baseline_position_avg = excluded.baseline_position_avg,
                baseline_exit_remaining = excluded.baseline_exit_remaining,
                entry_attempted = excluded.entry_attempted,
                updated_at = excluded.updated_at
            """,
            [
                rec.token_id, rec.label, rec.size, rec.avg_price,
                rec.baseline_position_size, rec.baseline_position_avg,
                rec.baseline_exit_remaining, rec.entry_attempted, rec.updated_at,
            ],
        )

    def get_position(self, token_id: str) -> PositionRecord | None:
        row = self._execute(
            "SELECT * FROM positions WHERE token_id = ?", [token_id]
        ).fetchone()
        return _row_to_position(row) if row else None

    def get_all_positions(self) -> list[PositionRecord]:
        rows = self._execute(
            "SELECT * FROM positions WHERE size > 0 ORDER BY updated_at DESC"
        ).fetchall()
        return [_row_to_position(r) for r in rows]

    def update_position_size(
        self,
        token_id: str,
        size: float,
        avg_price: float,
        label: str = "",
    ) -> None:
        self._execute(
            "UPDATE positions SET size = ?, avg_price = ?, label = ?, updated_at = ? "
            "WHERE token_id = ?",
            [size, avg_price, label, _utcnow_iso(), token_id],
        )

    def set_entry_attempted(self, token_id: str, attempted: bool) -> None:
        val = 1 if attempted else 0
        # Upsert: if position row doesn't exist, create a minimal one.
        existing = self.get_position(token_id)
        if existing is None:
            self.upsert_position(
                PositionRecord(token_id=token_id, entry_attempted=val)
            )
        else:
            self._execute(
                "UPDATE positions SET entry_attempted = ?, updated_at = ? WHERE token_id = ?",
                [val, _utcnow_iso(), token_id],
            )

    def is_entry_attempted(self, token_id: str) -> bool:
        pos = self.get_position(token_id)
        return pos is not None and pos.entry_attempted == 1

    def set_baselines(
        self,
        token_id: str,
        baseline_size: float,
        baseline_avg: float,
        baseline_exit_remaining: float,
    ) -> None:
        existing = self.get_position(token_id)
        if existing is None:
            self.upsert_position(
                PositionRecord(
                    token_id=token_id,
                    baseline_position_size=baseline_size,
                    baseline_position_avg=baseline_avg,
                    baseline_exit_remaining=baseline_exit_remaining,
                )
            )
        else:
            self._execute(
                """
                UPDATE positions SET
                    baseline_position_size = ?,
                    baseline_position_avg = ?,
                    baseline_exit_remaining = ?,
                    updated_at = ?
                WHERE token_id = ?
                """,
                [baseline_size, baseline_avg, baseline_exit_remaining, _utcnow_iso(), token_id],
            )

    def clear_position(self, token_id: str) -> None:
        """Reset position size to 0 (e.g. after full SELL). Keeps baselines."""
        self._execute(
            "UPDATE positions SET size = 0, avg_price = 0, updated_at = ? WHERE token_id = ?",
            [_utcnow_iso(), token_id],
        )

    def reset_position_state(self, token_id: str) -> None:
        """Fully reset a token's position state after SELL completion.

        Clears size/avg_price/baselines/entry_attempted in the positions table
        AND deletes the token's row from failed_attempts. This allows the bot
        to re-enter the same token in a new trading cycle.
        """
        with self._lock:
            conn = self.conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE positions SET size = 0, avg_price = 0, "
                    "baseline_position_size = 0, baseline_position_avg = 0, "
                    "baseline_exit_remaining = 0, entry_attempted = 0, "
                    "updated_at = ? WHERE token_id = ?",
                    [_utcnow_iso(), token_id],
                )
                conn.execute(
                    "DELETE FROM failed_attempts WHERE token_id = ?", [token_id]
                )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    # =================================================================== #
    # Seen trade ids (dedup)
    # =================================================================== #
    def is_trade_seen(self, trade_id: str) -> bool:
        row = self._execute(
            "SELECT 1 FROM seen_trade_ids WHERE trade_id = ?", [trade_id]
        ).fetchone()
        return row is not None

    def mark_trade_seen(self, trade_id: str) -> None:
        self._execute(
            "INSERT OR IGNORE INTO seen_trade_ids (trade_id, first_seen_at) VALUES (?, ?)",
            [trade_id, _utcnow_iso()],
        )

    def mark_trades_seen_batch(self, trade_ids: Iterable[str]) -> None:
        now = _utcnow_iso()
        rows = [(tid, now) for tid in trade_ids if tid]
        if rows:
            self._executemany(
                "INSERT OR IGNORE INTO seen_trade_ids (trade_id, first_seen_at) VALUES (?, ?)",
                rows,
            )

    # =================================================================== #
    # Failed attempts (SELL backoff)
    # =================================================================== #
    def get_failed_count(self, token_id: str) -> int:
        row = self._execute(
            "SELECT failure_count FROM failed_attempts WHERE token_id = ?", [token_id]
        ).fetchone()
        return int(row["failure_count"]) if row else 0

    def incr_failed(self, token_id: str, reason: str = "") -> int:
        """Increment failure count, return new count."""
        now = _utcnow_iso()
        self._execute(
            """
            INSERT INTO failed_attempts (token_id, failure_count, last_failure_at, last_reason)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(token_id) DO UPDATE SET
                failure_count = failure_count + 1,
                last_failure_at = excluded.last_failure_at,
                last_reason = excluded.last_reason
            """,
            [token_id, now, reason],
        )
        return self.get_failed_count(token_id)

    def reset_failed(self, token_id: str) -> None:
        self._execute(
            "UPDATE failed_attempts SET failure_count = 0 WHERE token_id = ?",
            [token_id],
        )

    # =================================================================== #
    # Reconciliations (audit log)
    # =================================================================== #
    def insert_reconciliation(
        self,
        session_id: str,
        source: str,
        token_id: str | None,
        local_state: Any,
        remote_state: Any,
        mismatch_type: str,
        resolution: str,
        notes: str | None = None,
    ) -> None:
        self._execute(
            """
            INSERT INTO reconciliations
                (session_id, run_at, source, token_id, local_state, remote_state,
                 mismatch_type, resolution, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                session_id, _utcnow_iso(), source, token_id,
                json.dumps(local_state, default=str) if local_state is not None else None,
                json.dumps(remote_state, default=str) if remote_state is not None else None,
                mismatch_type, resolution, notes,
            ],
        )

    def query_reconciliations(self, session_id: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        if session_id:
            return self._execute(
                "SELECT * FROM reconciliations WHERE session_id = ? ORDER BY run_at DESC LIMIT ?",
                [session_id, limit],
            ).fetchall()
        return self._execute(
            "SELECT * FROM reconciliations ORDER BY run_at DESC LIMIT ?", [limit]
        ).fetchall()

    # =================================================================== #
    # Archival
    # =================================================================== #
    def archive_old_data(
        self,
        order_retention_days: int = 30,
        seen_trades_retention_days: int = 7,
        recon_retention_days: int = 90,
    ) -> dict[str, int]:
        """Archive closed orders and old fills/trades/recons. Returns counts."""
        now = datetime.now(timezone.utc)
        order_cutoff = (now - timedelta(days=order_retention_days)).isoformat()
        seen_cutoff = (now - timedelta(days=seen_trades_retention_days)).isoformat()
        recon_cutoff = (now - timedelta(days=recon_retention_days)).isoformat()

        terminal_statuses = [
            OrderStatus.FILLED.value,
            OrderStatus.CANCELED.value,
            OrderStatus.REJECTED.value,
        ]
        placeholders = ",".join("?" * len(terminal_statuses))

        # Column lists matching the main tables (excluding archived_at which uses DEFAULT)
        order_cols = (
            "id, intent_id, order_id, session_id, token_id, label, side, price, size, "
            "filled_size, status, pair_intent_id, created_at, placed_at, closed_at, "
            "raw_response, notes"
        )
        fill_cols = (
            "id, trade_id, order_id, intent_id, token_id, side, size, price, "
            "matched_at, recorded_at, raw_trade"
        )

        with self._lock:
            conn = self.conn
            try:
                conn.execute("BEGIN IMMEDIATE")

                # 1. Archive closed orders older than retention
                cur = conn.execute(
                    f"INSERT INTO archived_orders ({order_cols}) "
                    f"SELECT {order_cols} FROM orders "
                    f"WHERE status IN ({placeholders}) AND closed_at < ?",
                    terminal_statuses + [order_cutoff],
                )
                archived_orders = cur.rowcount
                conn.execute(
                    f"DELETE FROM orders WHERE status IN ({placeholders}) AND closed_at < ?",
                    terminal_statuses + [order_cutoff],
                )

                # 2. Archive old fills
                cur = conn.execute(
                    f"INSERT INTO archived_fills ({fill_cols}) "
                    f"SELECT {fill_cols} FROM fills WHERE recorded_at < ?",
                    [order_cutoff],
                )
                archived_fills = cur.rowcount
                conn.execute("DELETE FROM fills WHERE recorded_at < ?", [order_cutoff])

                # 3. Truncate seen_trade_ids
                cur = conn.execute(
                    "DELETE FROM seen_trade_ids WHERE first_seen_at < ?", [seen_cutoff]
                )
                truncated_seen = cur.rowcount

                # 4. Truncate reconciliations
                cur = conn.execute(
                    "DELETE FROM reconciliations WHERE run_at < ?", [recon_cutoff]
                )
                truncated_recons = cur.rowcount

                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return {
            "archived_orders": archived_orders,
            "archived_fills": archived_fills,
            "truncated_seen_trades": truncated_seen,
            "truncated_reconciliations": truncated_recons,
        }

    def fresh_start(self) -> dict[str, int]:
        """Archive ALL current data and clear main tables.

        Used when the user chooses FRESH_START recovery mode. Sessions and
        reconciliations history is preserved for audit.
        """
        order_cols = (
            "id, intent_id, order_id, session_id, token_id, label, side, price, size, "
            "filled_size, status, pair_intent_id, created_at, placed_at, closed_at, "
            "raw_response, notes"
        )
        fill_cols = (
            "id, trade_id, order_id, intent_id, token_id, side, size, price, "
            "matched_at, recorded_at, raw_trade"
        )
        with self._lock:
            conn = self.conn
            try:
                conn.execute("BEGIN IMMEDIATE")

                cur = conn.execute(
                    f"INSERT INTO archived_orders ({order_cols}) SELECT {order_cols} FROM orders"
                )
                archived_orders = cur.rowcount
                cur = conn.execute(
                    f"INSERT INTO archived_fills ({fill_cols}) SELECT {fill_cols} FROM fills"
                )
                archived_fills = cur.rowcount

                conn.execute("DELETE FROM orders")
                conn.execute("DELETE FROM fills")
                conn.execute("DELETE FROM positions")
                conn.execute("DELETE FROM seen_trade_ids")
                conn.execute("DELETE FROM failed_attempts")

                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        # VACUUM outside the transaction (VACUUM cannot run inside a tx)
        self._execute("VACUUM")
        return {
            "archived_orders": archived_orders,
            "archived_fills": archived_fills,
        }

    def vacuum(self) -> None:
        self._execute("VACUUM")


# --------------------------------------------------------------------------- #
# Row → dataclass converters
# --------------------------------------------------------------------------- #
def _row_to_order(row: sqlite3.Row) -> OrderRecord:
    return OrderRecord(
        intent_id=row["intent_id"],
        session_id=row["session_id"],
        token_id=row["token_id"],
        side=row["side"],
        price=row["price"],
        size=row["size"],
        status=row["status"],
        label=row["label"] or "",
        order_id=row["order_id"],
        filled_size=row["filled_size"],
        pair_intent_id=row["pair_intent_id"],
        created_at=row["created_at"],
        placed_at=row["placed_at"],
        closed_at=row["closed_at"],
        raw_response=row["raw_response"],
        notes=row["notes"],
    )


def _row_to_fill(row: sqlite3.Row) -> FillRecord:
    return FillRecord(
        trade_id=row["trade_id"],
        token_id=row["token_id"],
        side=row["side"],
        size=row["size"],
        price=row["price"],
        order_id=row["order_id"],
        intent_id=row["intent_id"],
        matched_at=row["matched_at"],
        recorded_at=row["recorded_at"],
        raw_trade=row["raw_trade"],
    )


def _row_to_position(row: sqlite3.Row) -> PositionRecord:
    return PositionRecord(
        token_id=row["token_id"],
        label=row["label"] or "",
        size=row["size"],
        avg_price=row["avg_price"],
        baseline_position_size=row["baseline_position_size"],
        baseline_position_avg=row["baseline_position_avg"],
        baseline_exit_remaining=row["baseline_exit_remaining"],
        entry_attempted=row["entry_attempted"],
        updated_at=row["updated_at"],
    )
