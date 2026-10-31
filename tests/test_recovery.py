"""Ledger recovery: rebuild from snapshot + exchange history, compare, swap."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from engine.order import recovery
from engine.order.consolidator import OrderConsolidator
from engine.order.ledger import LedgerDB
from engine.order.snapshots import SnapshotDB
from execution.types import Order, OrderSide, OrderState, OrderStatus, OrderType
from workers.recover_ledger import run

if TYPE_CHECKING:
    from pathlib import Path

    from engine.order.ledger import StrategySnapshot

_T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _status(
    tag: str,
    qty: str,
    price: str = "100",
    filled: str | None = None,
    state: OrderState = OrderState.FILLED,
    exchange_fee: str = "0.1",
) -> OrderStatus:
    """A Bybit order as its history returns it; *qty* is signed."""
    side = OrderSide.BUY if Decimal(qty) > 0 else OrderSide.SELL
    done = Decimal(filled if filled is not None else qty.lstrip("-"))
    return OrderStatus(
        order_id=f"bybit-{tag}",
        client_order_id=tag,
        symbol="ETHUSDT",
        side=side,
        state=state,
        quantity=abs(Decimal(qty)),
        filled_qty=done,
        leaves_qty=Decimal(0),
        avg_price=Decimal(price) if done else None,
        filled_value=done * Decimal(price),
        fees={"USDT": Decimal(exchange_fee)} if done else {},
        raw_status=state.value,
        created_at=_T0,
        updated_at=_T0,
    )


class _Live:
    """A running consolidator's ledger + the matching Bybit history."""

    def __init__(self, tmp_path: Path) -> None:
        self.ledger = LedgerDB(tmp_path / "ledger.db")
        self.snapshots = SnapshotDB(tmp_path / "snapshots.db")
        self.exchange: list[tuple[str, OrderStatus]] = []

    def order(self, qty: str, price: str = "100", strategy: str = "alpha",
              status: OrderStatus | None = None) -> str:
        """Place one order end to end: ledger rows + its Bybit history entry."""
        tag = self.ledger.next_order_link_id(strategy)
        st = status or _status(tag, qty, price)
        st = OrderStatus(**{**st.__dict__, "client_order_id": tag, "order_id": f"bybit-{tag}"})
        self.pending(tag, st, strategy)
        self.finish(tag, st, strategy)
        return tag

    def pending(self, tag: str, st: OrderStatus, strategy: str = "alpha") -> None:
        self.ledger.record_pending(tag, strategy, "bybit_demo", st.symbol, st.side, st.quantity)
        self.ledger.record_accepted(tag, st.order_id)

    def finish(self, tag: str, st: OrderStatus, strategy: str = "alpha") -> None:
        sign = Decimal(1) if st.side is OrderSide.BUY else Decimal(-1)
        order = Order(symbol=st.symbol, side=st.side, order_type=OrderType.MARKET,
                      quantity=st.quantity, price=Decimal(0), client_order_id=tag)
        self.ledger.record_result(
            OrderConsolidator.fill_from_status(strategy, "bybit_demo", order, st, sign)
        )
        self.exchange.append(("bybit_demo", st))

    def snapshot(self, strategy: str = "alpha") -> StrategySnapshot:
        snap = self.ledger.snapshot(strategy)
        self.snapshots.save(snap)
        return snap


def _rebuild(
    tmp_path: Path, live: _Live, current: LedgerDB | None, margin: int = 100
) -> tuple[LedgerDB, recovery.RecoveryReport]:
    target = LedgerDB(tmp_path / "rebuilt.db")
    report = recovery.rebuild(
        target, current, live.snapshots.latest_all(), live.exchange, _T0, margin
    )
    return target, report


# ------------------------------------------------------------------ rebuild


def test_check_finds_nothing_wrong_with_an_intact_ledger(tmp_path: Path) -> None:
    live = _Live(tmp_path)
    live.order("2", "100")
    live.order("-1", "110")
    live.snapshot()  # at order 2
    live.order("1", "120")
    live.order("-1", "130")

    target, report = _rebuild(tmp_path, live, live.ledger)

    assert not report.has_differences, report.text()
    (s,) = report.strategies
    assert (s.snapshot_counter, s.replayed, s.kept, s.counter_rebuilt) == (2, 2, 2, 104)
    assert target.holdings("alpha") == live.ledger.holdings("alpha")
    assert [t.order_link_id for t in target.trades("alpha")] == ["1_4", "1_3", "1_2", "1_1"]
    assert target.counter("alpha") == 104


def test_guard_trips_and_resets_survive_a_rebuild(tmp_path: Path) -> None:
    live = _Live(tmp_path)
    live.order("1", "100")
    live.order("1", "100", strategy="beta")
    live.snapshot()
    live.snapshot("beta")
    # alpha's trip is still latched; beta's was reset
    live.ledger.record_trip("alpha", Decimal(-150), Decimal(100), False)
    live.ledger.record_trip("beta", Decimal(-120), Decimal(100), True)
    live.ledger.reset_trip("beta", Decimal(-120), Decimal(-130))

    target, _ = _rebuild(tmp_path, live, live.ledger)

    assert list(target.active_trips()) == ["alpha"]
    assert target.guard_baselines() == {"beta": Decimal(-120)}
    assert target.guard_baselines_net() == {"beta": Decimal(-130)}


def test_guard_trips_lost_with_the_ledger_are_reported(tmp_path: Path) -> None:
    live = _Live(tmp_path)
    live.order("1", "100")
    live.snapshot()

    _, report = _rebuild(tmp_path, live, current=None)

    (s,) = report.strategies
    assert any("guard trips and resets are lost" in n for n in s.notes)


def test_rebuilds_a_lost_ledger_from_snapshot_and_exchange(tmp_path: Path) -> None:
    live = _Live(tmp_path)
    live.order("2", "100")
    live.order("-1", "110")
    live.snapshot()
    live.order("1", "120")
    live.order("-1", "130")

    target, report = _rebuild(tmp_path, live, current=None)

    assert target.holdings("alpha") == live.ledger.holdings("alpha")
    assert [t.order_link_id for t in target.trades("alpha")] == ["1_4", "1_3"]
    assert target.trades("alpha")[0].status == "filled"
    (s,) = report.strategies
    assert s.counter_current is None
    assert any("trade log before order 2 is lost" in n for n in s.notes)


def test_order_in_flight_at_snapshot_time_is_replayed(tmp_path: Path) -> None:
    live = _Live(tmp_path)
    live.order("1", "100")
    tag = live.ledger.next_order_link_id("alpha")
    st = _status(tag, "1", "120")
    live.pending(tag, st)
    snap = live.snapshot()  # order 2 still in flight
    assert snap.unfinished == [tag]
    live.finish(tag, st)

    target, _ = _rebuild(tmp_path, live, current=None)

    (holding,) = target.holdings("alpha")
    assert holding.quantity == Decimal("2")
    assert holding.avg_entry_price == Decimal("110")


def test_drift_between_ledger_and_exchange_is_reported(tmp_path: Path) -> None:
    live = _Live(tmp_path)
    live.order("1", "100")
    live.snapshot()
    tag = live.order("1", "100")
    # Bybit actually filled only half of order 2.
    live.exchange[-1] = ("bybit_demo", _status(tag, "1", "100", filled="0.5",
                                               state=OrderState.CANCELLED))

    target, report = _rebuild(tmp_path, live, live.ledger)

    assert report.has_differences
    diffs = report.strategies[0].differences
    assert any(d.startswith("holding bybit_demo ETHUSDT: quantity 2 -> 1.5") for d in diffs)
    assert any(d.startswith(f"trade {tag}: current filled 1 ") for d in diffs)
    assert target.holdings("alpha")[0].quantity == Decimal("1.5")


def test_refused_order_row_is_kept_without_a_difference(tmp_path: Path) -> None:
    live = _Live(tmp_path)
    live.order("1", "100")
    live.snapshot()
    refused = live.ledger.next_order_link_id("alpha")  # Bybit refused it: not in history
    live.ledger.record_pending(refused, "alpha", "bybit_demo", "ETHUSDT", OrderSide.BUY, Decimal(1))
    live.ledger.record_result(OrderConsolidator.fill_from_status(
        "alpha", "bybit_demo",
        Order(symbol="ETHUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
              quantity=Decimal(1), price=Decimal(0), client_order_id=refused),
        _status(refused, "1", filled="0", state=OrderState.REJECTED), Decimal(1),
    ))

    target, report = _rebuild(tmp_path, live, live.ledger)

    assert not report.has_differences, report.text()
    assert target.trade(refused).status == "rejected"  # type: ignore[union-attr]


def test_fill_missing_from_the_exchange_is_a_difference(tmp_path: Path) -> None:
    live = _Live(tmp_path)
    live.order("1", "100")
    live.snapshot()
    tag = live.order("1", "100")
    live.exchange.pop()  # Bybit has no such order

    target, report = _rebuild(tmp_path, live, live.ledger)

    diffs = report.strategies[0].differences
    assert any(d.startswith(f"trade {tag}: filled 1 @ 100") and "not on the exchange" in d
               for d in diffs)
    assert target.trade(tag) is None
    assert target.holdings("alpha")[0].quantity == Decimal("1")


def test_untagged_and_unknown_strategy_orders_are_reported(tmp_path: Path) -> None:
    live = _Live(tmp_path)
    live.order("1")
    live.snapshot()
    manual = _status("x", "1")
    live.exchange.append(("bybit_demo", OrderStatus(**{**manual.__dict__, "client_order_id": None})))
    live.exchange.append(("bybit_spot", _status("9_1", "1")))

    _, report = _rebuild(tmp_path, live, live.ledger)

    assert report.has_differences
    assert len(report.untagged) == 1 and "link=-" in report.untagged[0]
    assert len(report.unknown_strategy) == 1 and "link=9_1" in report.unknown_strategy[0]


def test_strategy_without_snapshot_is_rebuilt_from_history(tmp_path: Path) -> None:
    live = _Live(tmp_path)
    live.order("1", "100")
    live.order("1", "200", strategy="beta")
    live.snapshot("alpha")  # beta never got one

    target, report = _rebuild(tmp_path, live, live.ledger)

    beta = next(s for s in report.strategies if s.strategy == "beta")
    assert beta.snapshot_counter is None
    assert any("no snapshot" in n for n in beta.notes)
    assert not beta.differences  # its one order is still in Bybit's history
    assert target.holdings("beta") == live.ledger.holdings("beta")
    assert target.strategy_ids() == live.ledger.strategy_ids()


def test_fetch_since_reads_from_before_the_oldest_snapshot() -> None:
    now = _T0 + timedelta(days=3)
    snaps = [  # only taken_at / strategy matter
        type("S", (), {"strategy": "alpha", "taken_at": _T0 + timedelta(days=2)})(),
        type("S", (), {"strategy": "beta", "taken_at": _T0 + timedelta(days=1)})(),
    ]
    since = recovery.fetch_since(snaps, ["alpha", "beta"], now, timedelta(days=7))
    assert since == _T0 + timedelta(days=1) - recovery.FETCH_SLACK
    assert recovery.fetch_since(snaps, ["gamma"], now, timedelta(days=7)) == now - timedelta(days=7)


# --------------------------------------------------------------------- swap


def test_swap_in_keeps_a_backup_with_its_side_files(tmp_path: Path) -> None:
    ledger, rebuilt = tmp_path / "ledger.db", tmp_path / "rebuilt.db"
    ledger.write_text("old")
    (tmp_path / "ledger.db-wal").write_text("old wal")
    rebuilt.write_text("new")

    backup = recovery.swap_in(ledger, rebuilt, now=_T0)

    assert ledger.read_text() == "new"
    assert backup == tmp_path / "ledger.db.bak-20261001T000000Z"
    assert backup.read_text() == "old"
    assert (tmp_path / "ledger.db.bak-20261001T000000Z-wal").read_text() == "old wal"
    assert not rebuilt.exists()


def test_swap_in_without_a_current_ledger(tmp_path: Path) -> None:
    rebuilt = tmp_path / "rebuilt.db"
    rebuilt.write_text("new")

    assert recovery.swap_in(tmp_path / "ledger.db", rebuilt) is None
    assert (tmp_path / "ledger.db").read_text() == "new"


# ---------------------------------------------------------------------- CLI


class _HistoryBroker:
    def __init__(self, orders: list[OrderStatus]) -> None:
        self.orders = orders

    async def order_history(self, start: datetime, end: datetime) -> list[OrderStatus]:
        return self.orders


def _args(tmp_path: Path, write: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        write=write,
        ledger=tmp_path / "ledger.db",
        snapshots=tmp_path / "snapshots.db",
        lookback_days=7.0,
        margin=100,
    )


def _closed_live(tmp_path: Path, drift: bool = False) -> dict[str, _HistoryBroker]:
    live = _Live(tmp_path)
    live.order("1", "100")
    live.snapshot()
    tag = live.order("1", "100")
    if drift:
        live.exchange[-1] = ("bybit_demo", _status(tag, "1", "100", filled="0.5",
                                                   state=OrderState.CANCELLED))
    live.ledger.close()
    live.snapshots.close()
    return {"bybit_demo": _HistoryBroker([st for _, st in live.exchange])}


def _files(tmp_path: Path) -> list[str]:
    """Database files, ignoring SQLite's -wal / -shm side files (a read-only
    open of a WAL-mode file creates empty ones; the data is unchanged)."""
    return sorted(
        p.name for p in tmp_path.iterdir() if not p.name.endswith(("-wal", "-shm"))
    )


def test_cli_check_mode_changes_nothing(tmp_path: Path) -> None:
    brokers = _closed_live(tmp_path, drift=True)
    before = (tmp_path / "ledger.db").read_bytes()
    files = _files(tmp_path)

    assert asyncio.run(run(_args(tmp_path), brokers)) == 1  # differences found

    assert (tmp_path / "ledger.db").read_bytes() == before
    assert _files(tmp_path) == files  # no rebuilt file left behind


def test_cli_write_replaces_a_drifted_ledger_and_keeps_a_backup(tmp_path: Path) -> None:
    brokers = _closed_live(tmp_path, drift=True)

    assert asyncio.run(run(_args(tmp_path, write=True), brokers)) == 0

    assert any(name.startswith("ledger.db.bak-") for name in _files(tmp_path))
    fixed = LedgerDB(tmp_path / "ledger.db", readonly=True)
    assert fixed.holdings("alpha")[0].quantity == Decimal("1.5")
    assert fixed.counter("alpha") == 102


def test_cli_write_leaves_an_intact_ledger_alone(tmp_path: Path) -> None:
    brokers = _closed_live(tmp_path)
    files = _files(tmp_path)

    assert asyncio.run(run(_args(tmp_path, write=True), brokers)) == 0

    assert _files(tmp_path) == files


def test_cli_write_recreates_a_lost_ledger(tmp_path: Path) -> None:
    brokers = _closed_live(tmp_path)
    for name in _files(tmp_path):
        if name.startswith("ledger.db"):
            (tmp_path / name).unlink()

    assert asyncio.run(run(_args(tmp_path, write=True), brokers)) == 0

    rebuilt = LedgerDB(tmp_path / "ledger.db", readonly=True)
    assert rebuilt.holdings("alpha")[0].quantity == Decimal("2")
    assert rebuilt.counter("alpha") == 102
