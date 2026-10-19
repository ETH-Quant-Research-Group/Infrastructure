"""Perp funding payments, split across strategies.

Bybit pays (or charges) funding every 8 h, per symbol, for the **whole
account** — not per order, so a payment carries no strategy tag.  Each one
is split by the strategies' positions at settlement time::

    share = amount × strategy position / account position    (8 decimals)

The formula is linear, so the shares add up to what Bybit paid even when
strategies hold opposite sides.  Whatever no strategy accounts for goes to
:data:`UNATTRIBUTED` — the "breadcrumbs":

- positions no strategy owns (manual or untagged orders, liquidations,
  lot-size leftovers);
- fills recorded on the other side of the settlement time;
- rounding of the shares.

The account position comes from Bybit's own settlement record, so
``sum(shares) == amount`` always.  If Bybit doesn't report it, the
strategies' positions are taken as the whole account.

Written by the daily run (``workers/snapshot_all.py --only funding``) into
its own file, ``~/.qrf/funding.db`` — not into the ledger, which only the
consolidator writes.  Added when PnL is computed:
``net = realized + unrealized + funding − fees``.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from execution.types import FundingSettlement

if TYPE_CHECKING:
    from engine.order.ledger import LedgerDB

log = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path.home() / ".qrf" / "funding.db"

# Holder of everything no strategy accounts for.  Deployed strategy names
# must start with a letter or digit (manager NAME_RE), so it can't clash.
UNATTRIBUTED = "_unattributed"

# The exchange (broker name) whose fills hold the perp positions funding is
# paid on — see workers.consolidator_worker.build_brokers.
PERP_EXCHANGE = "bybit_demo"

_PRECISION = Decimal("0.00000001")  # Bybit's 8 decimals


def _plain(value: Decimal) -> Decimal:
    """Drop trailing zeros (``1.00000000`` → ``1``) without exponent notation."""
    return Decimal(format(value.normalize(), "f"))


def split(
    settlement: FundingSettlement, positions: dict[str, Decimal]
) -> dict[str, Decimal]:
    """Each strategy's share of *settlement*, given their *positions* at
    settlement time; the rest under :data:`UNATTRIBUTED`.  Shares of 0 are
    left out."""
    account = settlement.position
    if account is None:
        account = sum(positions.values(), Decimal(0))
    shares: dict[str, Decimal] = {}
    if account != 0:
        per_unit = settlement.amount / account
        for name, qty in sorted(positions.items()):
            share = (per_unit * qty).quantize(_PRECISION)
            if share != 0:
                shares[name] = _plain(share)
    rest = settlement.amount - sum(shares.values(), Decimal(0))
    if rest != 0:
        shares[UNATTRIBUTED] = _plain(rest)
    return shares


class FundingDB:
    """Funding settlements and each strategy's share of them."""

    def __init__(self, db_path: str | Path | None = None, *, readonly: bool = False) -> None:
        path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._totals: dict[str, dict[str, Decimal]] = {}
        self._seen_rowid = 0
        if readonly:
            self._conn = sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False
            )
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS settlements (
                id           TEXT PRIMARY KEY,   -- Bybit's transaction id
                symbol       TEXT NOT NULL,
                settled_at   TEXT NOT NULL,
                amount       TEXT NOT NULL,      -- positive = received
                position     TEXT,               -- account position, if reported
                recorded_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shares (
                settlement_id  TEXT NOT NULL REFERENCES settlements(id),
                strategy       TEXT NOT NULL,
                symbol         TEXT NOT NULL,
                amount         TEXT NOT NULL,
                PRIMARY KEY (settlement_id, strategy)
            );
            CREATE INDEX IF NOT EXISTS idx_shares_strategy ON shares(strategy);
        """)
        self._conn.commit()

    def _catch_up(self) -> None:
        rows = self._conn.execute(
            "SELECT rowid, strategy, symbol, amount FROM shares WHERE rowid > ? ORDER BY rowid",
            (self._seen_rowid,),
        ).fetchall()
        for rowid, strategy, symbol, amount in rows:
            totals = self._totals.setdefault(strategy, {})
            totals[symbol] = totals.get(symbol, Decimal(0)) + Decimal(amount)
            self._seen_rowid = rowid

    def has(self, settlement_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM settlements WHERE id = ?", (settlement_id,)
        ).fetchone() is not None

    def record(self, settlement: FundingSettlement, shares: dict[str, Decimal]) -> bool:
        """Store a settlement and its shares in one transaction.  Returns
        ``False`` (and stores nothing) if it was recorded before."""
        if self.has(settlement.settlement_id):
            return False
        with self._conn:
            self._conn.execute(
                "INSERT INTO settlements (id, symbol, settled_at, amount, position,"
                " recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    settlement.settlement_id,
                    settlement.symbol,
                    settlement.settled_at.isoformat(),
                    str(settlement.amount),
                    None if settlement.position is None else str(settlement.position),
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._conn.executemany(
                "INSERT INTO shares (settlement_id, strategy, symbol, amount)"
                " VALUES (?, ?, ?, ?)",
                [
                    (settlement.settlement_id, name, settlement.symbol, str(amount))
                    for name, amount in shares.items()
                ],
            )
        return True

    def last_settled_at(self) -> datetime | None:
        (last,) = self._conn.execute("SELECT MAX(settled_at) FROM settlements").fetchone()
        return datetime.fromisoformat(last) if last else None

    def by_symbol(self, strategy: str) -> dict[str, Decimal]:
        """*strategy*'s total funding per symbol (positive = received).

        Kept as running totals in memory: each call only reads the shares
        added since the last one (shares are insert-only), so the guard can
        ask on every check.  Also correct for read-only copies in other
        processes — they pick up the writer's new rows the same way."""
        self._catch_up()
        return dict(sorted(self._totals.get(strategy, {}).items()))

    def total(self, strategy: str) -> Decimal:
        return sum(self.by_symbol(strategy).values(), Decimal(0))

    def close(self) -> None:
        self._conn.close()


async def collect_funding(
    ledger: LedgerDB,
    db: FundingDB,
    broker: Any,
    now: datetime,
    lookback: timedelta = timedelta(days=7),
) -> list[tuple[FundingSettlement, dict[str, Decimal]]]:
    """Fetch the settlements since the last one recorded (or *lookback* on
    the first run), split each and store it.  Already-recorded ones are
    skipped, so running it twice never counts a payment twice.  Returns
    what was newly recorded."""
    last = db.last_settled_at()
    start = last - timedelta(hours=1) if last else now - lookback
    settlements = await broker.funding_settlements(start, now)
    recorded = []
    for settlement in sorted(settlements, key=lambda s: s.settled_at):
        if db.has(settlement.settlement_id):
            continue
        positions = ledger.positions_at(PERP_EXCHANGE, settlement.symbol, settlement.settled_at)
        shares = split(settlement, positions)
        db.record(settlement, shares)
        recorded.append((settlement, shares))
    return recorded
