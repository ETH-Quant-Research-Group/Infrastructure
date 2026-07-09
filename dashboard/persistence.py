"""SQLite persistence for dashboard state.

Provides durable storage for PnL history, orders, and fills so data
survives dashboard restarts.  All numeric values are stored as TEXT to
preserve Decimal precision — no float conversion occurs in this layer.

The DB file lives at ``~/.qrf/dashboard.db`` by default, keeping data
separate from code (survives ``git pull`` / rsync updates).

Usage::

    from dashboard.persistence import DashboardDB

    db = DashboardDB()          # uses ~/.qrf/dashboard.db
    db.insert_broker_pnl(record)
    history = db.load_broker_pnl(limit=1000)
    db.close()
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dashboard.store import (
        BrokerPnLRecord,
        FillRecord,
        OrderRecord,
        PnLRecord,
    )

log = logging.getLogger(__name__)

_DEFAULT_DB_DIR = Path.home() / ".qrf"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "dashboard.db"


class DashboardDB:
    """Thin SQLite wrapper for dashboard persistence."""

    def __init__(self, db_path: str | None = None) -> None:
        path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        log.info("Dashboard DB opened: %s", path)

    # ------------------------------------------------------------------ schema

    def _migrate(self) -> None:
        c = self._conn
        c.executescript("""
            CREATE TABLE IF NOT EXISTS broker_pnl (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                total_realized  TEXT NOT NULL,
                total_unrealized TEXT NOT NULL,
                total           TEXT NOT NULL,
                timestamp       TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_broker_pnl_ts
                ON broker_pnl(timestamp);

            CREATE TABLE IF NOT EXISTS strategy_pnl (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id     TEXT NOT NULL,
                total_realized  TEXT NOT NULL,
                total_unrealized TEXT NOT NULL,
                total           TEXT NOT NULL,
                timestamp       TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_strategy_pnl_sid
                ON strategy_pnl(strategy_id, timestamp);

            CREATE TABLE IF NOT EXISTS orders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                side            TEXT NOT NULL,
                order_type      TEXT NOT NULL,
                quantity        TEXT NOT NULL,
                price           TEXT NOT NULL,
                reduce_only     INTEGER NOT NULL DEFAULT 0,
                exchange        TEXT NOT NULL DEFAULT '',
                placed_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fills (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id     TEXT NOT NULL,
                symbol          TEXT NOT NULL,
                quantity        TEXT NOT NULL,
                fill_price      TEXT NOT NULL,
                exchange        TEXT NOT NULL DEFAULT '',
                filled_at       TEXT NOT NULL
            );

            -- Generic key/value table for cross-restart persistence of small
            -- scalars (baselines, since-inception markers, manual offsets).
            -- Used by the consolidator to remember its first-ever realized-PnL
            -- baseline so that REALIZED in the dashboard does not reset on
            -- every process restart.
            CREATE TABLE IF NOT EXISTS kv (
                key    TEXT PRIMARY KEY,
                value  TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        self._conn.commit()

    # ------------------------------------------------------------------ kv

    def get_kv(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM kv WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_kv(self, key: str, value: str) -> None:
        from datetime import UTC, datetime
        self._conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, datetime.now(UTC).isoformat()),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ write

    def insert_broker_pnl(self, record: BrokerPnLRecord) -> None:
        self._conn.execute(
            "INSERT INTO broker_pnl (total_realized, total_unrealized, total, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (record["total_realized"], record["total_unrealized"],
             record["total"], record["timestamp"]),
        )
        self._conn.commit()

    def insert_strategy_pnl(self, record: PnLRecord) -> None:
        self._conn.execute(
            "INSERT INTO strategy_pnl (strategy_id, total_realized, total_unrealized, total, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (record["strategy_id"], record["total_realized"],
             record["total_unrealized"], record["total"], record["timestamp"]),
        )
        self._conn.commit()

    def insert_order(self, record: OrderRecord) -> None:
        self._conn.execute(
            "INSERT INTO orders (symbol, side, order_type, quantity, price, reduce_only, exchange, placed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (record["symbol"], record["side"], record["order_type"],
             record["quantity"], record["price"],
             1 if record["reduce_only"] else 0,
             record["exchange"], record["placed_at"]),
        )
        self._conn.commit()

    def insert_fill(self, record: FillRecord) -> None:
        self._conn.execute(
            "INSERT INTO fills (strategy_id, symbol, quantity, fill_price, exchange, filled_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (record["strategy_id"], record["symbol"], record["quantity"],
             record["fill_price"], record["exchange"], record["filled_at"]),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ read

    def load_broker_pnl(self, limit: int = 1000) -> list[BrokerPnLRecord]:
        rows = self._conn.execute(
            "SELECT total_realized, total_unrealized, total, timestamp "
            "FROM broker_pnl ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        # Reverse so oldest is first (matches in-memory store convention).
        return [
            {
                "total_realized": r[0],
                "total_unrealized": r[1],
                "total": r[2],
                "timestamp": r[3],
            }
            for r in reversed(rows)
        ]

    def load_strategy_pnl(self, limit: int = 1000) -> dict[str, list[PnLRecord]]:
        rows = self._conn.execute(
            "SELECT strategy_id, total_realized, total_unrealized, total, timestamp "
            "FROM strategy_pnl ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result: dict[str, list[PnLRecord]] = {}
        for r in reversed(rows):
            record: PnLRecord = {
                "strategy_id": r[0],
                "total_realized": r[1],
                "total_unrealized": r[2],
                "total": r[3],
                "timestamp": r[4],
            }
            result.setdefault(r[0], []).append(record)
        return result

    def load_orders(self, limit: int = 500) -> list[OrderRecord]:
        rows = self._conn.execute(
            "SELECT symbol, side, order_type, quantity, price, reduce_only, exchange, placed_at "
            "FROM orders ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "symbol": r[0],
                "side": r[1],
                "order_type": r[2],
                "quantity": r[3],
                "price": r[4],
                "reduce_only": bool(r[5]),
                "exchange": r[6],
                "placed_at": r[7],
            }
            for r in reversed(rows)
        ]

    def load_fills(self, limit: int = 500) -> dict[str, list[FillRecord]]:
        rows = self._conn.execute(
            "SELECT strategy_id, symbol, quantity, fill_price, exchange, filled_at "
            "FROM fills ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result: dict[str, list[FillRecord]] = {}
        for r in reversed(rows):
            record: FillRecord = {
                "strategy_id": r[0],
                "symbol": r[1],
                "quantity": r[2],
                "fill_price": r[3],
                "exchange": r[4],
                "filled_at": r[5],
            }
            result.setdefault(r[0], []).append(record)
        return result

    # ----------------------------------------------------------------- lifecycle

    def close(self) -> None:
        self._conn.close()
        log.info("Dashboard DB closed")
