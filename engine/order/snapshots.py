"""Per-strategy snapshots, for recovery.

A strategy's state is copied from the ledger into its own SQLite file —
order counter, holdings, realized PnL, fees, trade counts and any trades
still unfinished (see :class:`~engine.order.ledger.StrategySnapshot`) —
by two writers:

- the consolidator, after an order, once ``every`` orders (default 50)
  have passed since its last snapshot (:meth:`SnapshotDB.due`);
- ``workers/snapshot_all.py``, run once a day, for every strategy with new
  orders since its last snapshot (:func:`take_all`).

The daily run keeps the orders since a strategy's last snapshot within
Bybit's order history retention (7 days on demo), which a rebuild of a lost
ledger reads them back from — even for a strategy that trades rarely.

Both writers can save the same state (same strategy and counter); the
second save is then skipped.  Each process caches only the snapshots *it*
saved, so the consolidator may snapshot a little more often than needed
after a daily run — harmless.

Kept in a separate file from ``ledger.db`` on purpose: snapshots are what's
left if the ledger is lost or damaged.  That includes each strategy's order
counter, which must never go backwards, or Bybit rejects new orders as
duplicate tags.

Read back by the recovery program (``engine/order/recovery.py``).

The DB file lives at ``~/.qrf/snapshots.db`` by default.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from engine.order.ledger import Holding, StrategySnapshot

if TYPE_CHECKING:
    from engine.order.ledger import LedgerDB

log = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path.home() / ".qrf" / "snapshots.db"


class SnapshotDB:
    """Append-only store of :class:`StrategySnapshot` rows.

    The counter of each strategy's latest snapshot is cached in memory
    (loaded once at startup), so :meth:`due` is a dict lookup.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        every: int = 50,
        *,
        readonly: bool = False,
    ) -> None:
        if every < 1:
            raise ValueError(f"every must be at least 1, got {every}")
        path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._every = every

        if readonly:
            self._conn = sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._create_table()

        # strategy name → counter of its latest snapshot
        self._last: dict[str, int] = dict(
            self._conn.execute(
                "SELECT strategy, MAX(counter) FROM snapshots GROUP BY strategy"
            ).fetchall()
        )
        log.info("Snapshot DB opened: %s (every %d orders)", path, every)

    def _create_table(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy          TEXT NOT NULL,
                strategy_num      INTEGER NOT NULL,
                counter           INTEGER NOT NULL,
                taken_at          TEXT NOT NULL,
                realized_pnl      TEXT NOT NULL,
                fees_paid         TEXT NOT NULL,
                net_pnl           TEXT NOT NULL,
                trades_by_status  TEXT NOT NULL,
                holdings          TEXT NOT NULL,
                unfinished        TEXT NOT NULL,
                UNIQUE (strategy, counter)
            );
        """)
        self._conn.commit()

    def due(self, strategy: str, counter: int) -> bool:
        """Whether *strategy*, now at order *counter*, has placed ``every``
        orders since its last snapshot (counting from 0 if it has none)."""
        return counter - self._last.get(strategy, 0) >= self._every

    def last_counter(self, strategy: str) -> int | None:
        """Counter of *strategy*'s latest snapshot, or ``None`` if it has none."""
        return self._last.get(strategy)

    def save(self, snap: StrategySnapshot) -> bool:
        """Store *snap*.  Returns ``False`` (and stores nothing) if a snapshot
        of the same strategy at the same counter exists — the same state."""
        holdings = [
            {
                "exchange": h.exchange,
                "symbol": h.symbol,
                "quantity": str(h.quantity),
                "avg_entry_price": str(h.avg_entry_price),
                "realized_pnl": str(h.realized_pnl),
                "fees_paid": str(h.fees_paid),
            }
            for h in snap.holdings
        ]
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO snapshots (strategy, strategy_num, counter, taken_at,"
                " realized_pnl, fees_paid, net_pnl, trades_by_status, holdings,"
                " unfinished) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (strategy, counter) DO NOTHING",
                (
                    snap.strategy,
                    snap.strategy_num,
                    snap.counter,
                    snap.taken_at.isoformat(),
                    str(snap.realized_pnl),
                    str(snap.fees_paid),
                    str(snap.net_pnl),
                    json.dumps(snap.trades_by_status),
                    json.dumps(holdings),
                    json.dumps(snap.unfinished),
                ),
            )
        self._last[snap.strategy] = max(self._last.get(snap.strategy, 0), snap.counter)
        return cur.rowcount > 0

    def latest(self, strategy: str) -> StrategySnapshot | None:
        """The most recent snapshot of *strategy*, or ``None``."""
        row = self._conn.execute(
            "SELECT strategy_num, counter, taken_at, realized_pnl, fees_paid,"
            " trades_by_status, holdings, unfinished FROM snapshots"
            " WHERE strategy = ? ORDER BY counter DESC LIMIT 1",
            (strategy,),
        ).fetchone()
        if row is None:
            return None
        return StrategySnapshot(
            strategy=strategy,
            strategy_num=row[0],
            counter=row[1],
            taken_at=datetime.fromisoformat(row[2]),
            realized_pnl=Decimal(row[3]),
            fees_paid=Decimal(row[4]),
            trades_by_status=json.loads(row[5]),
            holdings=[
                Holding(
                    exchange=h["exchange"],
                    symbol=h["symbol"],
                    quantity=Decimal(h["quantity"]),
                    avg_entry_price=Decimal(h["avg_entry_price"]),
                    realized_pnl=Decimal(h["realized_pnl"]),
                    fees_paid=Decimal(h["fees_paid"]),
                )
                for h in json.loads(row[6])
            ],
            unfinished=json.loads(row[7]),
        )

    def latest_all(self) -> list[StrategySnapshot]:
        """The most recent snapshot of every strategy."""
        return [
            snap
            for strategy in sorted(self._last)
            if (snap := self.latest(strategy)) is not None
        ]

    def close(self) -> None:
        self._conn.close()


def take_all(ledger: LedgerDB, snapshots: SnapshotDB) -> list[tuple[str, int, bool]]:
    """Snapshot every strategy in *ledger* that has orders since its last
    snapshot (the daily run).

    Safe while the consolidator is trading: :meth:`LedgerDB.snapshot` reads
    in one transaction.  Returns ``(strategy, counter, saved)`` for each
    strategy; ``saved`` is ``False`` when nothing changed since its last one.
    """
    results: list[tuple[str, int, bool]] = []
    for strategy in sorted(ledger.strategies()):
        snap = ledger.snapshot(strategy)
        last = snapshots.last_counter(strategy)
        if snap.counter == 0 or (last is not None and snap.counter <= last):
            results.append((strategy, snap.counter, False))
            continue
        results.append((strategy, snap.counter, snapshots.save(snap)))
    return results
