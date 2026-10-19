"""Rebuild the ledger from the latest snapshots + the exchange's order history.

Run by hand, with the consolidator stopped, through
``workers/recover_ledger.py``.  For each strategy:

1. start from its latest snapshot (holdings and order counter);
2. take its orders from the exchange's history: every tag
   ``{num}_{counter}`` above the snapshot's counter, plus the snapshot's
   ``unfinished`` tags (in flight when it was taken, so their fills aren't
   in its holdings);
3. replay them in counter order through the normal ledger code
   (``record_pending`` → ``record_accepted`` → ``record_result``, with
   :meth:`OrderConsolidator.fill_from_status`), so the holdings come out
   exactly as the consolidator would have recorded them;
4. keep trade rows from the current ledger where the exchange can't help:
   everything the snapshot already covers, and orders the exchange never
   had (refused outright, so no fill and no effect on holdings);
5. set the counter above every tag seen anywhere, plus a margin.

The result is written to a separate, new ledger file.  :func:`rebuild`
returns a report of how it differs from the current ledger;
:func:`swap_in` replaces the current ledger with it, keeping a backup.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from engine.order.consolidator import OrderConsolidator
from execution.types import Order, OrderSide, OrderType

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from engine.order.ledger import Holding, LedgerDB, StrategySnapshot, Trade
    from execution.types import OrderStatus

log = logging.getLogger(__name__)

_TAG = re.compile(r"^(\d+)_(\d+)$")

# Read the exchange from a bit before each snapshot: an order in flight when
# the snapshot was taken was created before it.
FETCH_SLACK = timedelta(hours=1)

# Added on top of the highest counter seen.  Tags of orders the exchange
# refused outright are in neither its history nor a lost ledger; the margin
# keeps a rebuilt counter from handing them out again.
COUNTER_MARGIN = 100

_HOLDING_FIELDS = ("quantity", "avg_entry_price", "realized_pnl", "fees_paid")


def parse_tag(tag: str | None) -> tuple[int, int] | None:
    """``"3_1042"`` → ``(3, 1042)``; anything else → ``None``."""
    m = _TAG.match(tag or "")
    return (int(m[1]), int(m[2])) if m else None


# --------------------------------------------------------------------- report


@dataclass
class StrategyReport:
    strategy: str
    num: int
    snapshot_counter: int | None
    counter_current: int | None  # None: not in the current ledger
    counter_rebuilt: int = 0
    replayed: int = 0  # orders replayed from the exchange
    kept: int = 0  # trade rows kept from the current ledger
    differences: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class RecoveryReport:
    since: datetime
    strategies: list[StrategyReport] = field(default_factory=list)
    unknown_strategy: list[str] = field(default_factory=list)
    untagged: list[str] = field(default_factory=list)

    @property
    def has_differences(self) -> bool:
        return bool(
            self.unknown_strategy
            or self.untagged
            or any(s.differences for s in self.strategies)
        )

    def text(self) -> str:
        lines = [f"Exchange orders read since {self.since.isoformat()}", ""]
        if not self.strategies:
            lines.append("No strategies found in the ledger or the snapshots.")
        for s in self.strategies:
            snap = (
                f"snapshot at order {s.snapshot_counter}"
                if s.snapshot_counter is not None
                else "no snapshot"
            )
            current = (
                f"current counter {s.counter_current}"
                if s.counter_current is not None
                else "not in the current ledger"
            )
            lines.append(
                f"[{s.num}] {s.strategy}: {snap}; {s.replayed} order(s) replayed"
                f" from the exchange, {s.kept} trade row(s) kept from the current"
                f" ledger; {current} -> rebuilt counter {s.counter_rebuilt}"
            )
            lines += [f"    note: {n}" for n in s.notes]
            lines += [f"    DIFF: {d}" for d in s.differences]
            if s.counter_current is not None and not s.differences:
                lines.append("    matches the current ledger")
        if self.unknown_strategy:
            lines += ["", "Orders tagged for a strategy number no ledger or snapshot knows:"]
            lines += [f"    {o}" for o in self.unknown_strategy]
        if self.untagged:
            lines += ["", "Orders without a consolidator tag (placed outside the consolidator?):"]
            lines += [f"    {o}" for o in self.untagged]
        return "\n".join(lines)


# ---------------------------------------------------------------- exchange


def fetch_since(
    snapshots: Iterable[StrategySnapshot],
    strategies: Iterable[str],
    now: datetime,
    lookback: timedelta,
) -> datetime:
    """How far back the exchange's history must be read: just before the
    oldest latest-snapshot, or *lookback* for a strategy without one."""
    taken = {s.strategy: s.taken_at for s in snapshots}
    starts = [
        taken[name] - FETCH_SLACK if name in taken else now - lookback
        for name in set(strategies) | set(taken)
    ]
    return min(starts, default=now - lookback)


async def fetch_orders(
    brokers: dict[str, Any], since: datetime, until: datetime
) -> list[tuple[str, OrderStatus]]:
    """Every order on every broker with an ``order_history``, as
    ``(exchange, status)``, without the duplicates history windows can give."""
    seen: set[tuple[str, str]] = set()
    orders: list[tuple[str, OrderStatus]] = []
    for exchange, broker in brokers.items():
        if not hasattr(broker, "order_history"):
            log.warning("%s has no order history - its orders can't be recovered", exchange)
            continue
        for status in await broker.order_history(since, until):
            if (exchange, status.order_id) not in seen:
                seen.add((exchange, status.order_id))
                orders.append((exchange, status))
    return orders


def _describe(exchange: str, st: OrderStatus) -> str:
    return (
        f"{exchange} {st.symbol} {st.side} qty={st.quantity} filled={st.filled_qty}"
        f" {st.raw_status} id={st.order_id} link={st.client_order_id or '-'}"
    )


# ----------------------------------------------------------------- rebuild


def rebuild(
    target: LedgerDB,
    current: LedgerDB | None,
    snapshots: list[StrategySnapshot],
    orders: list[tuple[str, OrderStatus]],
    since: datetime,
    margin: int = COUNTER_MARGIN,
) -> RecoveryReport:
    """Fill the empty ledger *target* from *snapshots* + exchange *orders*,
    keeping what it can from *current* (``None`` if lost or unreadable),
    and report how the result differs from *current*."""
    report = RecoveryReport(since=since)

    names: dict[int, str] = {}
    if current is not None:
        names.update({num: name for name, num in current.strategy_ids().items()})
    for snap in snapshots:
        names.setdefault(snap.strategy_num, snap.strategy)
    snaps = {s.strategy: s for s in snapshots}

    by_num: dict[int, dict[int, tuple[str, OrderStatus]]] = {}
    for exchange, st in orders:
        parsed = parse_tag(st.client_order_id)
        if parsed is None:
            report.untagged.append(_describe(exchange, st))
        elif parsed[0] not in names:
            report.unknown_strategy.append(_describe(exchange, st))
        else:
            by_num.setdefault(parsed[0], {})[parsed[1]] = (exchange, st)

    for num in sorted(names):
        name = names[num]
        report.strategies.append(
            _rebuild_strategy(
                target, current, num, name, snaps.get(name), by_num.get(num, {}), margin
            )
        )
    return report


def _rebuild_strategy(
    target: LedgerDB,
    current: LedgerDB | None,
    num: int,
    name: str,
    snap: StrategySnapshot | None,
    on_exchange: dict[int, tuple[str, OrderStatus]],
    margin: int,
) -> StrategyReport:
    in_current = current is not None and name in current.strategy_ids()
    report = StrategyReport(
        strategy=name,
        num=num,
        snapshot_counter=snap.counter if snap else None,
        counter_current=current.counter(name) if current is not None and in_current else None,
    )
    base = snap.counter if snap else 0
    unfinished = set(snap.unfinished) if snap else set()

    def covered(counter: int, tag: str) -> bool:
        """Already reflected in the snapshot's holdings."""
        return counter <= base and tag not in unfinished

    if snap is None:
        report.notes.append(
            "no snapshot: rebuilt from the exchange's history alone - fills older"
            " than it are missing"
        )
    elif not in_current and base > 0:
        report.notes.append(
            f"trade log before order {base} is lost (holdings come from the snapshot)"
        )

    target.restore_strategy(num, name)
    for holding in snap.holdings if snap else []:
        target.restore_holding(name, holding)

    replay = {
        counter: order
        for counter, order in on_exchange.items()
        if not covered(counter, order[1].client_order_id or "")
    }
    replay_tags = {st.client_order_id for _, st in replay.values()}

    old_trades = {t.order_link_id: t for t in current.trades(name, limit=None)} if (
        current is not None and in_current
    ) else {}
    keep: dict[int, Trade] = {}
    for tag, trade in old_trades.items():
        if tag in replay_tags:
            continue
        counter = (parse_tag(tag) or (0, 0))[1]
        if covered(counter, tag) or trade.filled_qty == 0:
            keep[counter] = trade
        else:
            report.differences.append(
                f"trade {tag}: {trade.status} {trade.filled_qty} @ {trade.avg_price} in"
                " the current ledger, but not on the exchange - left out"
            )

    for counter in sorted(replay.keys() | keep.keys()):
        if counter in replay:
            _replay(target, name, *replay[counter])
            report.replayed += 1
        else:
            target.restore_trade(keep[counter])
            report.kept += 1

    seen = [
        base,
        report.counter_current or 0,
        *on_exchange,
        *((parse_tag(tag) or (0, 0))[1] for tag in old_trades),
    ]
    report.counter_rebuilt = max(seen) + margin
    target.set_counter(name, report.counter_rebuilt)

    if current is not None and in_current:
        report.differences += _holding_diffs(current.holdings(name), target.holdings(name))
        for _, (_, st) in sorted(replay.items()):
            tag = st.client_order_id or ""
            old, new = old_trades.get(tag), target.trade(tag)
            assert new is not None
            if old is None:
                report.differences.append(
                    f"trade {tag}: on the exchange ({new.status} {new.filled_qty} @"
                    f" {new.avg_price}) but not in the current ledger"
                )
            elif (old.status, old.filled_qty, old.avg_price) != (
                new.status, new.filled_qty, new.avg_price
            ):
                report.differences.append(
                    f"trade {tag}: current {old.status} {old.filled_qty} @"
                    f" {old.avg_price} -> exchange {new.status} {new.filled_qty} @"
                    f" {new.avg_price}"
                )
    return report


def _replay(target: LedgerDB, name: str, exchange: str, st: OrderStatus) -> None:
    """Record one exchange order the way the consolidator would have."""
    tag = st.client_order_id or ""
    order = Order(
        symbol=st.symbol,
        side=st.side,
        order_type=OrderType.MARKET,
        quantity=st.quantity,
        price=Decimal(0),
        client_order_id=tag,
    )
    sign = Decimal(1) if st.side is OrderSide.BUY else Decimal(-1)
    target.record_pending(tag, name, exchange, st.symbol, st.side, st.quantity, at=st.created_at)
    target.record_accepted(tag, st.order_id)
    fill = OrderConsolidator.fill_from_status(name, exchange, order, st, sign)
    target.record_result(fill, at=st.updated_at)


def _holding_diffs(current: list[Holding], rebuilt: list[Holding]) -> list[str]:
    old = {(h.exchange, h.symbol): h for h in current}
    new = {(h.exchange, h.symbol): h for h in rebuilt}
    out: list[str] = []
    for key in sorted(old.keys() | new.keys()):
        a, b = old.get(key), new.get(key)
        where = f"holding {key[0]} {key[1]}"
        if a is None:
            assert b is not None
            out.append(f"{where}: not in the current ledger (rebuilt quantity {b.quantity})")
        elif b is None:
            out.append(f"{where}: only in the current ledger (quantity {a.quantity})")
        else:
            changed = [
                f"{f} {getattr(a, f)} -> {getattr(b, f)}"
                for f in _HOLDING_FIELDS
                if getattr(a, f) != getattr(b, f)
            ]
            if changed:
                out.append(f"{where}: " + ", ".join(changed))
    return out


# -------------------------------------------------------------------- swap


def swap_in(ledger_path: Path, rebuilt_path: Path, now: datetime | None = None) -> Path | None:
    """Put the rebuilt ledger in place of the current one.

    The current file (and its ``-wal`` / ``-shm`` side files) is renamed to
    ``<name>.bak-<UTC time>`` first; returns that backup path, or ``None``
    if there was no current file.
    """
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    backup = None
    if ledger_path.exists():
        backup = ledger_path.with_name(f"{ledger_path.name}.bak-{stamp}")
        ledger_path.rename(backup)
        for suffix in ("-wal", "-shm"):
            side = ledger_path.with_name(ledger_path.name + suffix)
            if side.exists():
                side.rename(backup.with_name(backup.name + suffix))
    rebuilt_path.rename(ledger_path)
    return backup
