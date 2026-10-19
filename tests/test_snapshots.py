from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from engine.order.consolidator import OrderConsolidator
from engine.order.ledger import Holding, LedgerDB
from engine.order.snapshots import SnapshotDB, take_all
from workers import snapshot_all
from execution.types import (
    FillConfirmation,
    FillStatus,
    OrderSide,
)
from tests.fakes import FakeBroker
from interfaces.signals import TargetPosition

if TYPE_CHECKING:
    from pathlib import Path


def _trade(ledger: LedgerDB, strategy: str, qty: str, price: str = "100",
           status: FillStatus = FillStatus.FILLED) -> str:
    """Log one order for *strategy* and record its result."""
    tag = ledger.next_order_link_id(strategy)
    side = OrderSide.BUY if Decimal(qty) >= 0 else OrderSide.SELL
    ledger.record_pending(tag, strategy, "bybit_demo", "ETHUSDT", side, abs(Decimal(qty)))
    ledger.record_result(
        FillConfirmation(
            strategy_id=strategy,
            symbol="ETHUSDT",
            quantity=Decimal(qty),
            fill_price=Decimal(price) if qty != "0" else Decimal(0),
            exchange="bybit_demo",
            status=status,
            fees={"USDT": Decimal("0.1")} if qty != "0" else {},
            order_link_id=tag,
        )
    )
    return tag


# ----------------------------------------------------------------- content


def test_ledger_snapshot_has_holdings_totals_and_counts(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _trade(ledger, "alpha", "2", "100")
    _trade(ledger, "alpha", "-1", "110")
    _trade(ledger, "alpha", "0", status=FillStatus.REJECTED)
    stuck = ledger.next_order_link_id("alpha")  # sent, result never recorded
    ledger.record_pending(stuck, "alpha", "bybit_demo", "ETHUSDT", OrderSide.BUY, Decimal(1))

    snap = ledger.snapshot("alpha")

    assert (snap.strategy, snap.strategy_num, snap.counter) == ("alpha", 1, 4)
    assert snap.holdings == [
        Holding(
            exchange="bybit_demo",
            symbol="ETHUSDT",
            quantity=Decimal("1"),
            avg_entry_price=Decimal("100"),
            realized_pnl=Decimal("10"),
            fees_paid=Decimal("0.2"),
        )
    ]
    assert (snap.realized_pnl, snap.fees_paid, snap.net_pnl) == (
        Decimal("10"), Decimal("0.2"), Decimal("9.8")
    )
    assert snap.trades_by_status == {"filled": 2, "pending": 1, "rejected": 1}
    assert snap.unfinished == [stuck]


def test_snapshot_round_trip_and_survives_reopen(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _trade(ledger, "alpha", "2", "100")
    snap = ledger.snapshot("alpha")

    path = tmp_path / "snapshots.db"
    db = SnapshotDB(path)
    db.save(snap)
    db.close()

    reopened = SnapshotDB(path)
    assert reopened.latest("alpha") == snap
    assert reopened.latest("beta") is None


# ------------------------------------------------------------------- when


def test_due_every_n_orders_since_the_last_snapshot(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    db = SnapshotDB(tmp_path / "snapshots.db", every=3)
    assert [db.due("alpha", c) for c in (1, 2, 3)] == [False, False, True]

    for _ in range(3):
        _trade(ledger, "alpha", "1")
    db.save(ledger.snapshot("alpha"))  # at counter 3

    assert [db.due("alpha", c) for c in (3, 4, 5, 6)] == [False, False, False, True]
    assert not db.due("beta", 1)  # counted per strategy


def test_saving_the_same_state_twice_is_skipped(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _trade(ledger, "alpha", "1")
    db = SnapshotDB(tmp_path / "snapshots.db")

    assert db.save(ledger.snapshot("alpha"))
    assert not db.save(ledger.snapshot("alpha"))  # same counter: same state
    assert db.last_counter("alpha") == 1


def test_last_snapshot_counter_is_remembered_after_reopen(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    for _ in range(3):
        _trade(ledger, "alpha", "1")
    db = SnapshotDB(tmp_path / "snapshots.db", every=3)
    db.save(ledger.snapshot("alpha"))
    db.close()

    reopened = SnapshotDB(tmp_path / "snapshots.db", every=3)
    assert not reopened.due("alpha", 5)
    assert reopened.due("alpha", 6)


def test_every_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SnapshotDB(tmp_path / "snapshots.db", every=0)


# ----------------------------------------------------------- consolidator


_Broker = FakeBroker  # fills every order instantly at 100


def _consolidator(
    tmp_path: Path, every: int
) -> tuple[OrderConsolidator, LedgerDB, SnapshotDB, asyncio.Queue[FillConfirmation]]:
    ledger = LedgerDB(tmp_path / "ledger.db")
    snapshots = SnapshotDB(tmp_path / "snapshots.db", every=every)
    fills: asyncio.Queue[FillConfirmation] = asyncio.Queue()
    consolidator = OrderConsolidator(
        target_queue=asyncio.Queue(),
        brokers={"fake": _Broker()},
        default_exchange="fake",
        fills_queue=fills,
        ledger=ledger,
        snapshots=snapshots,
    )
    return consolidator, ledger, snapshots, fills


def _signals(consolidator: OrderConsolidator, *strategies: str) -> None:
    async def _run() -> None:
        for sid in strategies:
            await consolidator._reconcile(
                TargetPosition(symbol="ETHUSDT", quantity=Decimal(1), strategy_id=sid)
            )

    asyncio.run(_run())


def test_consolidator_snapshots_each_strategy_every_n_orders(tmp_path: Path) -> None:
    consolidator, _, snapshots, _ = _consolidator(tmp_path, every=2)
    _signals(consolidator, "alpha", "alpha", "beta", "alpha", "alpha", "alpha")

    alpha = snapshots.latest("alpha")
    assert alpha is not None
    assert alpha.counter == 4  # taken at 2 and 4; the 5th isn't due yet
    assert alpha.holdings[0].quantity == Decimal("4")
    assert snapshots.latest("beta") is None  # only 1 order


def test_consolidator_logs_each_order_pending_in_one_step(tmp_path: Path) -> None:
    """The counter and the pending row are written together: a snapshot taken
    by another process never sees a counter without its trade row."""
    ledger = LedgerDB(tmp_path / "ledger.db")
    tag = ledger.begin_order("alpha", "bybit_demo", "ETHUSDT", OrderSide.BUY, Decimal(1))

    snap = ledger.snapshot("alpha")
    assert (tag, snap.counter, snap.unfinished) == ("1_1", 1, ["1_1"])


# --------------------------------------------------------------- daily run


def test_daily_run_snapshots_only_strategies_with_new_orders(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    snapshots = SnapshotDB(tmp_path / "snapshots.db")
    _trade(ledger, "alpha", "1")
    _trade(ledger, "beta", "2")

    assert take_all(ledger, snapshots) == [("alpha", 1, True), ("beta", 1, True)]

    _trade(ledger, "beta", "-1")
    assert take_all(ledger, snapshots) == [("alpha", 1, False), ("beta", 2, True)]
    assert snapshots.latest("beta").holdings[0].quantity == Decimal("1")  # type: ignore[union-attr]


def test_daily_run_reads_a_ledger_another_process_is_writing(tmp_path: Path) -> None:
    """The consolidator's ledger keeps trading while the daily run reads it."""
    live = LedgerDB(tmp_path / "ledger.db")
    _trade(live, "alpha", "1")
    reader = LedgerDB(tmp_path / "ledger.db", readonly=True)
    _trade(live, "alpha", "1")  # after the reader opened: its cache says 1

    snap = reader.snapshot("alpha")

    assert snap.counter == 2  # read from the file, not the stale cache
    assert snap.holdings[0].quantity == Decimal("2")


def test_daily_program_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _trade(ledger, "alpha", "1")
    ledger.close()

    def run(ledger_path: Path) -> int:
        return asyncio.run(snapshot_all.run(
            ledger_path, tmp_path / "snapshots.db", tmp_path / "pnl.db", only="snapshots"
        ))

    assert run(tmp_path / "ledger.db") == 0
    assert run(tmp_path / "ledger.db") == 0

    out = capsys.readouterr().out
    assert "Snapshot alpha: snapshot saved (order 1)" in out
    assert "Snapshot alpha: unchanged since its last snapshot (order 1)" in out
    assert run(tmp_path / "missing.db") == 2


def test_snapshot_failure_still_reports_to_the_strategy(tmp_path: Path) -> None:
    consolidator, _, snapshots, fills = _consolidator(tmp_path, every=1)

    def broken(_snap: object) -> None:
        raise RuntimeError("disk full")

    snapshots.save = broken  # type: ignore[method-assign,assignment]
    _signals(consolidator, "alpha")

    assert fills.get_nowait().status is FillStatus.FILLED
