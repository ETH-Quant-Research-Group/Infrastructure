"""SQLite ledger owned by the consolidator.

Main tables (plus ``guard_trips``, the persisted guard state — see
``engine/order/guard_registry.py``):

``strategies``
    Strategy registry used to build exchange order tags: every strategy
    name gets a permanent integer ``id`` and a per-strategy order
    ``counter``.  Each order placed for a strategy is tagged
    ``"{id}_{counter}"`` (e.g. ``"3_1042"``), which Bybit stores as the
    order's ``orderLinkId`` — so every execution on the exchange can be
    traced back to the strategy that caused it.

``trades``
    The trade log: one row per order sent to an exchange.  Written as
    ``pending`` *before* the order goes out, given Bybit's ``order_id``
    once accepted, and updated with the real fill once the order is final.
    A crash mid-order therefore always leaves a row behind, which
    :meth:`~engine.order.consolidator.OrderConsolidator.recover_unfinished`
    finishes on the next start.

``holdings``
    Per strategy + exchange + symbol: quantity held, average entry price,
    realized PnL and fees paid, using the average-cost method (see
    :func:`apply_fill`).  Updated in the same transaction as the trade row,
    so the two never disagree.

Kept separate from ``dashboard.db``: losing this file means order tags can
no longer be mapped back to strategy names, and counters restarting at 1
would make Bybit reject new orders as duplicates.  Only the consolidator
writes to it.

All amounts are stored as TEXT to keep ``Decimal`` precision.

The DB file lives at ``~/.qrf/ledger.db`` by default.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from execution.types import FillStatus, OrderSide

if TYPE_CHECKING:
    from collections.abc import Callable

    from execution.types import FillConfirmation

log = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path.home() / ".qrf" / "ledger.db"

# Quote coins: fees charged in these never change the base-coin quantity held.
_QUOTE_COINS = frozenset({"USDT", "USDC"})

PENDING = "pending"
# Trades still worth asking the exchange about (on restart).
_UNFINISHED = (PENDING, FillStatus.UNKNOWN.value)


def base_coin_fee(symbol: str, fees: dict[str, Decimal]) -> Decimal:
    """Fee charged in *symbol*'s base coin (e.g. ETH on a Bybit spot buy)."""
    return sum(
        (
            amount
            for coin, amount in fees.items()
            if coin not in _QUOTE_COINS and symbol != coin and symbol.startswith(coin)
        ),
        Decimal(0),
    )


def fees_in_quote(symbol: str, fees: dict[str, Decimal], price: Decimal) -> Decimal:
    """Total fees in the quote coin; base-coin fees are converted at *price*.

    Fees in any other coin are not counted (and logged), since there is no
    price to convert them with.
    """
    total = Decimal(0)
    for coin, amount in fees.items():
        if coin in _QUOTE_COINS:
            total += amount
        elif symbol != coin and symbol.startswith(coin):
            total += amount * price
        else:
            log.warning("fee of %s %s on %s not counted in fees_paid", amount, coin, symbol)
    return total


def _already_applied(
    fill: FillConfirmation,
    qty: str | None,
    price: str | None,
    fees: str | None,
) -> tuple[Decimal, Decimal, dict[str, Decimal]] | None:
    """What an ``unknown`` trade row already put into the holdings (signed
    quantity, its average price, its fees) — ``None`` if nothing."""
    if qty is None or Decimal(qty) == 0:
        return None
    return (
        Decimal(qty),
        Decimal(price or 0),
        {coin: Decimal(a) for coin, a in json.loads(fees or "{}").items()},
    )


def _rest_of(
    fill: FillConfirmation, applied: tuple[Decimal, Decimal, dict[str, Decimal]]
) -> FillConfirmation | None:
    """The part of *fill* not yet in the holdings: the extra quantity at the
    average price it implies, and the extra fees.  ``None`` if nothing new."""
    qty, price, fees = applied
    extra_qty = fill.quantity - qty
    extra_fees = {
        coin: amount - fees.get(coin, Decimal(0))
        for coin, amount in fill.fees.items()
        if amount != fees.get(coin, Decimal(0))
    }
    if extra_qty == 0 and not extra_fees:
        return None
    extra_price = (
        (fill.quantity * fill.fill_price - qty * price) / extra_qty
        if extra_qty != 0
        else fill.fill_price
    )
    return replace(fill, quantity=extra_qty, fill_price=extra_price, fees=extra_fees)


def apply_fill(
    qty: Decimal,
    avg_entry: Decimal,
    realized: Decimal,
    delta: Decimal,
    price: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Average-cost update of one holding for a fill of signed *delta* at *price*.

    - opening / adding: the average entry becomes the weighted average,
      realized PnL is unchanged;
    - reducing / closing: realized PnL grows by the closed quantity times
      the gap between *price* and the average entry, which is unchanged;
    - flipping: the old side is fully closed, the rest opens at *price*.

    Returns the new ``(qty, avg_entry, realized)``.
    """
    if delta == 0:
        return qty, avg_entry, realized
    new_qty = qty + delta
    if qty == 0 or (qty > 0) == (delta > 0):
        return new_qty, (qty * avg_entry + delta * price) / new_qty, realized

    closed = min(abs(delta), abs(qty))
    if qty > 0:
        realized += (price - avg_entry) * closed
    else:
        realized += (avg_entry - price) * closed
    if new_qty == 0:
        avg_entry = Decimal(0)
    elif (qty > 0) != (new_qty > 0):
        avg_entry = price
    return new_qty, avg_entry, realized


@dataclass(frozen=True, kw_only=True)
class Holding:
    """One strategy's position in one symbol on one exchange.

    ``quantity`` is signed (positive = long).  ``fees_paid`` is in the
    quote coin; ``realized_pnl`` is before fees.
    """

    exchange: str
    symbol: str
    quantity: Decimal
    avg_entry_price: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal


@dataclass(frozen=True, kw_only=True)
class StrategySnapshot:
    """Point-in-time state of one strategy, built from the ledger.

    ``counter`` is the strategy's order counter when it was taken: every
    order up to it is either finished or listed in ``unfinished`` (a
    strategy's orders are handled one at a time).  ``trades_by_status``
    counts all of its trades so far.  Amounts are in the quote coin;
    ``realized_pnl`` is before fees.  No unrealized PnL — that needs a live
    price, which the ledger doesn't have.
    """

    strategy: str
    strategy_num: int
    counter: int
    taken_at: datetime
    holdings: list[Holding]
    realized_pnl: Decimal
    fees_paid: Decimal
    trades_by_status: dict[str, int]
    unfinished: list[str]

    @property
    def net_pnl(self) -> Decimal:
        return self.realized_pnl - self.fees_paid


@dataclass(frozen=True, kw_only=True)
class GuardTrip:
    """A strategy's guard tripping: its PnL then, and the limit it broke."""

    strategy: str
    tripped_at: datetime
    pnl: Decimal
    max_loss: Decimal
    include_unrealized: bool


@dataclass(frozen=True, kw_only=True)
class Trade:
    """One row of the trade log.

    ``requested_qty`` is what was sent (unsigned, after lot rounding);
    ``filled_qty`` what the strategy actually received (signed, net of
    base-coin fees).  ``status`` is ``"pending"`` or a
    :class:`~execution.types.FillStatus` value.
    """

    order_link_id: str
    strategy: str
    exchange: str
    symbol: str
    side: OrderSide
    requested_qty: Decimal
    status: str
    filled_qty: Decimal
    avg_price: Decimal | None
    fees: dict[str, Decimal]
    order_id: str | None
    reason: str | None
    created_at: datetime
    updated_at: datetime


_TRADE_COLUMNS = (
    "t.order_link_id, s.name, t.exchange, t.symbol, t.side, t.requested_qty,"
    " t.status, t.filled_qty, t.avg_price, t.fees, t.order_id, t.reason,"
    " t.created_at, t.updated_at"
)


def _trade(row: tuple[str, ...]) -> Trade:
    return Trade(
        order_link_id=row[0],
        strategy=row[1],
        exchange=row[2],
        symbol=row[3],
        side=OrderSide(row[4]),
        requested_qty=Decimal(row[5]),
        status=row[6],
        filled_qty=Decimal(row[7]),
        avg_price=Decimal(row[8]) if row[8] is not None else None,
        fees={coin: Decimal(amount) for coin, amount in json.loads(row[9]).items()},
        order_id=row[10],
        reason=row[11],
        created_at=datetime.fromisoformat(row[12]),
        updated_at=datetime.fromisoformat(row[13]),
    )


class LedgerDB:
    """Strategy registry, order counters, trade log and holdings.

    ``id`` and ``counter`` for every known strategy are cached in memory
    (loaded once at startup), so tagging an order costs a dict lookup plus
    one committed ``UPDATE`` — the write is what guarantees a counter value
    is never handed out twice, even across a crash.

    Assumes a single writer (the consolidator); the in-memory cache is the
    source of truth while the process runs.

    ``readonly=True`` opens an existing file without changing it (no schema
    migration either) — used by the recovery check.

    ``now`` is where every timestamp comes from (default: the real time);
    a backtest passes its simulated clock, so trade times and funding
    settlement times line up.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        readonly: bool = False,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        # Where timestamps come from: the real time, or a backtest's clock.
        self._clock_now: Callable[[], datetime] = now or (lambda: datetime.now(UTC))
        path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        if readonly:
            self._conn = sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._migrate()

        # strategy name → [id, counter]
        self._strategies: dict[str, list[int]] = {}
        self.reload()
        log.info("Ledger DB opened: %s (%d strategies)", path, len(self._strategies))

    def _now(self) -> str:
        return self._clock_now().isoformat()

    def reload(self) -> None:
        """Re-read the strategy list from the file — for a read-only copy,
        whose writer (the consolidator) may have registered new strategies
        since it was opened."""
        self._strategies = {
            name: [sid, counter]
            for sid, name, counter in self._conn.execute(
                "SELECT id, name, counter FROM strategies"
            )
        }

    def _migrate(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS strategies (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT NOT NULL UNIQUE,
                counter  INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS trades (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                order_link_id  TEXT NOT NULL UNIQUE,
                strategy_id    INTEGER NOT NULL REFERENCES strategies(id),
                exchange       TEXT NOT NULL,
                symbol         TEXT NOT NULL,
                side           TEXT NOT NULL,
                requested_qty  TEXT NOT NULL,
                status         TEXT NOT NULL,
                filled_qty     TEXT NOT NULL DEFAULT '0',
                avg_price      TEXT,
                fees           TEXT NOT NULL DEFAULT '{}',
                order_id       TEXT,
                reason         TEXT,
                created_at     TEXT NOT NULL,
                updated_at     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trades_strategy
                ON trades(strategy_id, id);
            CREATE INDEX IF NOT EXISTS idx_trades_status
                ON trades(status);

            -- Guard trips (engine/order/guard_registry.py): a trip stays
            -- until reset; a reset re-arms the guard from reset_pnl
            -- (realized + funding - fees) and, when the guard also counts
            -- unrealized, reset_pnl_net (the same plus unrealized).
            CREATE TABLE IF NOT EXISTS guard_trips (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id         INTEGER NOT NULL REFERENCES strategies(id),
                tripped_at          TEXT NOT NULL,
                pnl                 TEXT NOT NULL,
                max_loss            TEXT NOT NULL,
                include_unrealized  INTEGER NOT NULL,
                reset_at            TEXT,
                reset_pnl           TEXT,
                reset_pnl_net       TEXT
            );

            CREATE TABLE IF NOT EXISTS holdings (
                strategy_id      INTEGER NOT NULL REFERENCES strategies(id),
                exchange         TEXT NOT NULL,
                symbol           TEXT NOT NULL,
                quantity         TEXT NOT NULL,
                avg_entry_price  TEXT NOT NULL,
                realized_pnl     TEXT NOT NULL,
                fees_paid        TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                PRIMARY KEY (strategy_id, exchange, symbol)
            );
        """)
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(guard_trips)")
        }
        if "reset_pnl_net" not in columns:  # ledgers from before the two baselines
            self._conn.execute("ALTER TABLE guard_trips ADD COLUMN reset_pnl_net TEXT")
        self._conn.commit()

    # ------------------------------------------------------------------ registry

    def strategy_num(self, name: str) -> int:
        """Return the permanent integer id for *name*, registering it if new."""
        return self._get_or_register(name)[0]

    def _get_or_register(self, name: str) -> list[int]:
        entry = self._strategies.get(name)
        if entry is None:
            cur = self._conn.execute(
                "INSERT INTO strategies (name) VALUES (?)", (name,)
            )
            self._conn.commit()
            entry = [int(cur.lastrowid or 0), 0]
            self._strategies[name] = entry
            log.info("registered strategy %r as id=%d", name, entry[0])
        return entry

    # ------------------------------------------------------------------ order tags

    def next_order_link_id(self, name: str) -> str:
        """Allocate the next order tag for strategy *name*: ``"{id}_{counter}"``.

        The incremented counter is committed before returning, so a value is
        never reused — a crash after this call leaves a gap, not a duplicate.

        The consolidator uses :meth:`begin_order` instead, which also logs
        the pending trade in the same transaction.
        """
        entry = self._get_or_register(name)
        entry[1] += 1
        self._conn.execute(
            "UPDATE strategies SET counter = ? WHERE id = ?", (entry[1], entry[0])
        )
        self._conn.commit()
        return f"{entry[0]}_{entry[1]}"

    # ------------------------------------------------------------------ trade log

    def begin_order(
        self,
        strategy: str,
        exchange: str,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
    ) -> str:
        """Allocate *strategy*'s next tag and log the order as ``pending``, in
        one transaction.  Call *before* placing the order; returns the tag.

        One transaction matters for snapshots taken by another process: they
        must never see a counter without its trade row, or recovery would
        treat that order as already covered and lose its fill.
        """
        entry = self._get_or_register(strategy)
        counter = entry[1] + 1
        tag = f"{entry[0]}_{counter}"
        now = self._now()
        with self._conn:
            self._conn.execute(
                "UPDATE strategies SET counter = ? WHERE id = ?", (counter, entry[0])
            )
            self._conn.execute(
                "INSERT INTO trades (order_link_id, strategy_id, exchange, symbol,"
                " side, requested_qty, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tag, entry[0], exchange, symbol, side.value, str(quantity),
                 PENDING, now, now),
            )
        entry[1] = counter
        return tag

    def record_pending(
        self,
        order_link_id: str,
        strategy: str,
        exchange: str,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        at: datetime | None = None,
    ) -> None:
        """Log an order about to be sent.  Call *before* placing it.

        *at* overrides the creation time (the recovery uses the exchange's).
        """
        sid = self.strategy_num(strategy)
        now = at.isoformat() if at else self._now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO trades (order_link_id, strategy_id, exchange, symbol,"
                " side, requested_qty, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order_link_id, sid, exchange, symbol, side.value, str(quantity),
                 PENDING, now, now),
            )

    def record_accepted(self, order_link_id: str, order_id: str) -> None:
        """Store the exchange's ``order_id`` once it has accepted the order —
        how a restart tells "accepted" apart from "maybe never sent"."""
        with self._conn:
            self._conn.execute(
                "UPDATE trades SET order_id = ?, updated_at = ?"
                " WHERE order_link_id = ? AND order_id IS NULL",
                (order_id, self._now(), order_link_id),
            )

    def record_result(self, fill: FillConfirmation, at: datetime | None = None) -> bool:
        """Store the outcome of an order and apply it to the holdings, in one
        transaction.

        Only a ``pending`` or ``unknown`` trade can be updated, so the same
        fill is never applied twice.  An ``unknown`` trade may already hold
        what had filled when it was given up on (still open after the
        cancel); updating it applies only the rest.  Returns whether it was
        recorded.  *at* overrides the update time (the recovery uses the
        exchange's).
        """
        tag = fill.order_link_id
        if not tag:
            return False
        with self._conn:
            row = self._conn.execute(
                "SELECT strategy_id, status, filled_qty, avg_price, fees FROM trades"
                " WHERE order_link_id = ?",
                (tag,),
            ).fetchone()
            if row is None:
                log.error("no trade row for %s — fill not recorded: %s", tag, fill)
                return False
            sid, status, before_qty, before_price, before_fees = row
            if status not in _UNFINISHED:
                log.warning(
                    "trade %s is already %s — ignoring %s", tag, status, fill.status
                )
                return False
            applied = _already_applied(fill, before_qty, before_price, before_fees)

            executed = fill.quantity != 0
            self._conn.execute(
                "UPDATE trades SET status = ?, filled_qty = ?, avg_price = ?,"
                " fees = ?, order_id = COALESCE(?, order_id), reason = ?,"
                " updated_at = ? WHERE order_link_id = ?",
                (
                    fill.status.value,
                    str(fill.quantity),
                    str(fill.fill_price) if executed else None,
                    json.dumps({coin: str(amount) for coin, amount in fill.fees.items()}),
                    fill.order_id,
                    fill.reason,
                    at.isoformat() if at else self._now(),
                    tag,
                ),
            )
            if applied is None:
                if executed:
                    self._apply_to_holding(sid, fill)
            elif (rest := _rest_of(fill, applied)) is not None:
                self._apply_to_holding(sid, rest)
        return True

    def _apply_to_holding(self, sid: int, fill: FillConfirmation) -> None:
        row = self._conn.execute(
            "SELECT quantity, avg_entry_price, realized_pnl, fees_paid FROM holdings"
            " WHERE strategy_id = ? AND exchange = ? AND symbol = ?",
            (sid, fill.exchange, fill.symbol),
        ).fetchone()
        qty, avg, realized, fees = (
            (Decimal(v) for v in row) if row else (Decimal(0),) * 4
        )
        qty, avg, realized = apply_fill(qty, avg, realized, fill.quantity, fill.fill_price)
        fees += fees_in_quote(fill.symbol, fill.fees, fill.fill_price)
        self._conn.execute(
            "INSERT INTO holdings (strategy_id, exchange, symbol, quantity,"
            " avg_entry_price, realized_pnl, fees_paid, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (strategy_id, exchange, symbol) DO UPDATE SET"
            " quantity = excluded.quantity,"
            " avg_entry_price = excluded.avg_entry_price,"
            " realized_pnl = excluded.realized_pnl,"
            " fees_paid = excluded.fees_paid,"
            " updated_at = excluded.updated_at",
            (sid, fill.exchange, fill.symbol, str(qty), str(avg), str(realized),
             str(fees), self._now()),
        )

    # ------------------------------------------------------------------ reads

    def holdings(self, strategy: str) -> list[Holding]:
        """Every holding of *strategy*, including closed ones (quantity 0),
        which keep their realized PnL and fees."""
        entry = self._strategies.get(strategy)
        if entry is None:
            return []
        rows = self._conn.execute(
            "SELECT exchange, symbol, quantity, avg_entry_price, realized_pnl,"
            " fees_paid FROM holdings WHERE strategy_id = ?"
            " ORDER BY exchange, symbol",
            (entry[0],),
        ).fetchall()
        return [
            Holding(
                exchange=r[0],
                symbol=r[1],
                quantity=Decimal(r[2]),
                avg_entry_price=Decimal(r[3]),
                realized_pnl=Decimal(r[4]),
                fees_paid=Decimal(r[5]),
            )
            for r in rows
        ]

    def trades(self, strategy: str, limit: int | None = 100) -> list[Trade]:
        """The most recent trades of *strategy*, newest first (all of them
        when *limit* is ``None``)."""
        entry = self._strategies.get(strategy)
        if entry is None:
            return []
        rows = self._conn.execute(
            f"SELECT {_TRADE_COLUMNS} FROM trades t"  # noqa: S608 - constant columns
            " JOIN strategies s ON s.id = t.strategy_id"
            " WHERE t.strategy_id = ? ORDER BY t.id DESC LIMIT ?",
            (entry[0], -1 if limit is None else limit),
        ).fetchall()
        return [_trade(r) for r in rows]

    def trade(self, order_link_id: str) -> Trade | None:
        """The trade tagged *order_link_id*, or ``None``."""
        row = self._conn.execute(
            f"SELECT {_TRADE_COLUMNS} FROM trades t"  # noqa: S608 - constant columns
            " JOIN strategies s ON s.id = t.strategy_id"
            " WHERE t.order_link_id = ?",
            (order_link_id,),
        ).fetchone()
        return _trade(row) if row else None

    def positions_at(self, exchange: str, symbol: str, at: datetime) -> dict[str, Decimal]:
        """Each strategy's net position in *symbol* on *exchange* at time
        *at*: the sum of its fills recorded up to then (strategies at 0 are
        left out).  A fill is dated when its result was recorded."""
        positions: dict[str, Decimal] = {}
        rows = self._conn.execute(
            "SELECT s.name, t.filled_qty, t.updated_at FROM trades t"
            " JOIN strategies s ON s.id = t.strategy_id"
            " WHERE t.exchange = ? AND t.symbol = ? AND t.status IN (?, ?)",
            (exchange, symbol, FillStatus.FILLED.value, FillStatus.PARTIAL.value),
        )
        for name, qty, updated_at in rows:
            if datetime.fromisoformat(updated_at) <= at:
                positions[name] = positions.get(name, Decimal(0)) + Decimal(qty)
        return {name: qty for name, qty in positions.items() if qty != 0}

    def strategy_ids(self) -> dict[str, int]:
        """Every strategy name → its permanent number."""
        return {name: entry[0] for name, entry in self._strategies.items()}

    def strategies(self) -> list[str]:
        """Every strategy name ever registered."""
        return list(self._strategies)

    def counter(self, strategy: str) -> int:
        """The number of orders tagged for *strategy* so far (0 if unknown)."""
        entry = self._strategies.get(strategy)
        return entry[1] if entry else 0

    def snapshot(self, strategy: str) -> StrategySnapshot:
        """Current state of *strategy*: holdings, totals, trade counts.

        All reads happen in one read transaction and the counter is read
        from the file, not the cache — so it's consistent even when another
        process (the consolidator) is writing meanwhile.
        """
        sid = self._strategies[strategy][0]
        own_txn = not self._conn.in_transaction
        if own_txn:
            self._conn.execute("BEGIN")
        try:
            (counter,) = self._conn.execute(
                "SELECT counter FROM strategies WHERE id = ?", (sid,)
            ).fetchone()
            holdings = self.holdings(strategy)
            by_status = dict(
                self._conn.execute(
                    "SELECT status, COUNT(*) FROM trades WHERE strategy_id = ?"
                    " GROUP BY status ORDER BY status",
                    (sid,),
                ).fetchall()
            )
            unfinished = [
                tag
                for (tag,) in self._conn.execute(
                    "SELECT order_link_id FROM trades"
                    " WHERE strategy_id = ? AND status IN (?, ?) ORDER BY id",
                    (sid, *_UNFINISHED),
                )
            ]
        finally:
            if own_txn:
                self._conn.execute("COMMIT")
        return StrategySnapshot(
            strategy=strategy,
            strategy_num=sid,
            counter=counter,
            taken_at=self._clock_now(),
            holdings=holdings,
            realized_pnl=sum((h.realized_pnl for h in holdings), Decimal(0)),
            fees_paid=sum((h.fees_paid for h in holdings), Decimal(0)),
            trades_by_status=by_status,
            unfinished=unfinished,
        )

    def unfinished_trades(self) -> list[Trade]:
        """Trades still ``pending`` or ``unknown``, oldest first."""
        rows = self._conn.execute(
            f"SELECT {_TRADE_COLUMNS} FROM trades t"  # noqa: S608 - constant columns
            " JOIN strategies s ON s.id = t.strategy_id"
            " WHERE t.status IN (?, ?) ORDER BY t.id",
            _UNFINISHED,
        ).fetchall()
        return [_trade(r) for r in rows]

    # ------------------------------------------------------------- guard trips

    def record_trip(
        self, strategy: str, pnl: Decimal, max_loss: Decimal, include_unrealized: bool
    ) -> GuardTrip:
        sid = self.strategy_num(strategy)
        trip = GuardTrip(
            strategy=strategy,
            tripped_at=self._clock_now(),
            pnl=pnl,
            max_loss=max_loss,
            include_unrealized=include_unrealized,
        )
        with self._conn:
            self._conn.execute(
                "INSERT INTO guard_trips (strategy_id, tripped_at, pnl, max_loss,"
                " include_unrealized) VALUES (?, ?, ?, ?, ?)",
                (sid, trip.tripped_at.isoformat(), str(pnl), str(max_loss),
                 int(include_unrealized)),
            )
        return trip

    def active_trips(self) -> dict[str, GuardTrip]:
        """Every strategy whose guard is tripped and not yet reset."""
        rows = self._conn.execute(
            "SELECT s.name, g.tripped_at, g.pnl, g.max_loss, g.include_unrealized"
            " FROM guard_trips g JOIN strategies s ON s.id = g.strategy_id"
            " WHERE g.reset_at IS NULL ORDER BY g.id"
        )
        return {
            name: GuardTrip(
                strategy=name,
                tripped_at=datetime.fromisoformat(at),
                pnl=Decimal(pnl),
                max_loss=Decimal(limit),
                include_unrealized=bool(unrealized),
            )
            for name, at, pnl, limit, unrealized in rows
        }

    def reset_trip(
        self, strategy: str, pnl: Decimal, pnl_net: Decimal | None = None
    ) -> bool:
        """Reset *strategy*'s trip; its guard re-arms from *pnl* (the loss
        limit counts from here) — and from *pnl_net* for the checks that
        count unrealized PnL.  Returns ``False`` if it wasn't tripped."""
        entry = self._strategies.get(strategy)
        if entry is None:
            return False
        with self._conn:
            cur = self._conn.execute(
                "UPDATE guard_trips SET reset_at = ?, reset_pnl = ?, reset_pnl_net = ?"
                " WHERE strategy_id = ? AND reset_at IS NULL",
                (self._clock_now().isoformat(), str(pnl),
                 None if pnl_net is None else str(pnl_net), entry[0]),
            )
        return cur.rowcount > 0

    def guard_baselines(self) -> dict[str, Decimal]:
        """Each strategy's PnL at its last guard reset — what its loss limit
        counts from (strategies never reset aren't listed: they count from 0)."""
        rows = self._conn.execute(
            "SELECT s.name, g.reset_pnl FROM guard_trips g"
            " JOIN strategies s ON s.id = g.strategy_id"
            " WHERE g.reset_at IS NOT NULL ORDER BY g.id"
        )
        return {name: Decimal(pnl) for name, pnl in rows}  # latest reset wins

    def guard_baselines_net(self) -> dict[str, Decimal]:
        """Like :meth:`guard_baselines`, for the checks that count unrealized
        PnL — only strategies whose latest reset recorded one."""
        out: dict[str, Decimal] = {}
        for name, net in self._conn.execute(
            "SELECT s.name, g.reset_pnl_net FROM guard_trips g"
            " JOIN strategies s ON s.id = g.strategy_id"
            " WHERE g.reset_at IS NOT NULL ORDER BY g.id"
        ):
            if net is None:
                out.pop(name, None)  # the latest reset didn't count unrealized
            else:
                out[name] = Decimal(net)
        return out

    # ----------------------------------------------------------- recovery only
    #
    # Used by engine/order/recovery.py to build a fresh ledger from a
    # snapshot + the exchange's order history.  Never call these on the
    # ledger the consolidator is using.

    def restore_strategy(self, num: int, name: str) -> None:
        """Register *name* under its original number (counter set later)."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO strategies (id, name, counter) VALUES (?, ?, 0)", (num, name)
            )
        self._strategies[name] = [num, 0]

    def set_counter(self, strategy: str, counter: int) -> None:
        entry = self._strategies[strategy]
        with self._conn:
            self._conn.execute(
                "UPDATE strategies SET counter = ? WHERE id = ?", (counter, entry[0])
            )
        entry[1] = counter

    def copy_guard_trips(self, source: LedgerDB) -> int:
        """Copy every guard trip and reset of *source* (trips still latched,
        reset baselines) for the strategies restored here; returns how many."""
        rows = source._conn.execute(
            "SELECT s.name, g.tripped_at, g.pnl, g.max_loss, g.include_unrealized,"
            " g.reset_at, g.reset_pnl, g.reset_pnl_net"
            " FROM guard_trips g JOIN strategies s ON s.id = g.strategy_id"
            " ORDER BY g.id"
        ).fetchall()
        copied = 0
        with self._conn:
            for name, *rest in rows:
                entry = self._strategies.get(name)
                if entry is None:
                    continue
                self._conn.execute(
                    "INSERT INTO guard_trips (strategy_id, tripped_at, pnl, max_loss,"
                    " include_unrealized, reset_at, reset_pnl, reset_pnl_net)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (entry[0], *rest),
                )
                copied += 1
        return copied

    def restore_holding(self, strategy: str, holding: Holding) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO holdings (strategy_id, exchange, symbol, quantity,"
                " avg_entry_price, realized_pnl, fees_paid, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self._strategies[strategy][0], holding.exchange, holding.symbol,
                 str(holding.quantity), str(holding.avg_entry_price),
                 str(holding.realized_pnl), str(holding.fees_paid), self._now()),
            )

    def restore_trade(self, trade: Trade) -> None:
        """Copy a trade row as is (holdings are not touched)."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO trades (order_link_id, strategy_id, exchange, symbol,"
                " side, requested_qty, status, filled_qty, avg_price, fees, order_id,"
                " reason, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade.order_link_id,
                    self._strategies[trade.strategy][0],
                    trade.exchange,
                    trade.symbol,
                    trade.side.value,
                    str(trade.requested_qty),
                    trade.status,
                    str(trade.filled_qty),
                    str(trade.avg_price) if trade.avg_price is not None else None,
                    json.dumps({coin: str(a) for coin, a in trade.fees.items()}),
                    trade.order_id,
                    trade.reason,
                    trade.created_at.isoformat(),
                    trade.updated_at.isoformat(),
                ),
            )

    # ----------------------------------------------------------------- lifecycle

    def checkpoint(self) -> None:
        """Fold the WAL into the main file, so the file can be moved alone."""
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        self._conn.close()
