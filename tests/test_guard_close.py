"""The guard's periodic unrealized checks and closing a tripped strategy's
own positions (GUARD_UNREALIZED_EVERY, GUARD_CLOSE_ON_TRIP / CLOSE_ON_TRIP)."""

from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from engine.order.consolidator import OrderConsolidator
from engine.order.guard_registry import StrategyGuardRegistry
from engine.order.ledger import LedgerDB
from execution.types import OrderSide
from interfaces.signals import TargetPosition
from tests.test_guard import _PRICE, _Broker, _fill, _Prices
from workers.consolidator_worker import _parse_close_on_trip

if TYPE_CHECKING:
    from pathlib import Path


def _guard(
    ledger: LedgerDB,
    tmp_path: Path,
    *,
    limit: str = "100",
    every: int = 0,
    close: bool = False,
    prices: _Prices | None = None,
) -> StrategyGuardRegistry:
    return StrategyGuardRegistry(
        Decimal(limit),
        ledger=ledger,
        funding_path=tmp_path / "funding.db",
        prices=prices or _Prices("100"),
        unrealized_every=every,
        close_on_trip=close,
    )


def _held(ledger: LedgerDB, strategy: str) -> Decimal:
    return sum((h.quantity for h in ledger.snapshot(strategy).holdings), Decimal(0))


# ------------------------------------------------- unrealized every x checks


def test_unrealized_counts_on_every_xth_periodic_check(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "10", "100")
    guard = _guard(ledger, tmp_path, every=3, prices=_Prices("80"))  # −200 open

    asyncio.run(guard.check_all())
    asyncio.run(guard.check_all())
    assert guard.is_active("alpha")  # realized + fees only: −0.5

    asyncio.run(guard.check_all())  # 3rd: −200.5 incl. unrealized
    assert not guard.is_active("alpha")
    assert ledger.active_trips()["alpha"].include_unrealized
    assert "incl. unrealized" in (guard.describe_trip("alpha") or "")


def test_per_fill_checks_never_count_unrealized(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "10", "100")
    guard = _guard(ledger, tmp_path, every=1, prices=_Prices("80"))

    assert guard.check("alpha") == Decimal("-0.5")
    assert guard.is_active("alpha")


def test_unrealized_every_needs_a_price_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        StrategyGuardRegistry(
            Decimal(100),
            ledger=LedgerDB(tmp_path / "ledger.db"),
            unrealized_every=2,
        )
    with pytest.raises(ValueError):
        _guard(LedgerDB(tmp_path / "other.db"), tmp_path, every=-1)


def test_reset_rearms_incl_unrealized_when_checks_count_it(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "10", "100")
    guard = _guard(ledger, tmp_path, every=1, prices=_Prices("80"))
    asyncio.run(guard.check_all())  # −200.5 → tripped

    assert guard.reset("alpha") == Decimal("-200.5")
    asyncio.run(guard.check_all())
    assert guard.is_active("alpha")  # not tripped again by the same open loss
    assert guard.check("alpha", unrealized=True) == Decimal("0")


# ------------------------------------------------------------ close on trip


def _consolidator(
    ledger: LedgerDB,
    guard: StrategyGuardRegistry,
    broker: _Broker,
    queue: asyncio.Queue[TargetPosition] | None = None,
) -> OrderConsolidator:
    return OrderConsolidator(
        target_queue=queue or asyncio.Queue(),
        brokers={"bybit_demo": broker},
        default_exchange="bybit_demo",
        guard_registry=guard,
        ledger=ledger,
    )


def _signal(strategy: str, qty: str) -> TargetPosition:
    return TargetPosition(symbol="ETHUSDT", quantity=Decimal(qty), strategy_id=strategy)


async def _trade(consolidator: OrderConsolidator) -> None:
    _PRICE[0] = "100"
    await consolidator.handle(_signal("alpha", "2"))
    await consolidator.handle(_signal("beta", "1"))
    _PRICE[0] = "1"
    await consolidator.handle(_signal("alpha", "-1"))  # −99 realized → trips 50


def test_trip_closes_only_the_tripped_strategys_positions(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    guard = _guard(ledger, tmp_path, limit="50", close=True)
    broker = _Broker()

    asyncio.run(_trade(_consolidator(ledger, guard, broker)))

    assert not guard.is_active("alpha")
    assert _held(ledger, "alpha") == 0
    assert _held(ledger, "beta") == 1  # same symbol, other strategy: untouched
    close = broker.placed[-1]
    assert (close.side, close.quantity) == (OrderSide.SELL, Decimal(1))
    assert close.client_order_id is not None
    assert close.client_order_id.startswith(f"{ledger.strategy_num('alpha')}_")
    assert len(ledger.trades("alpha")) == 3  # recorded as alpha's own order


def test_no_close_when_switched_off_for_that_strategy(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    guard = _guard(ledger, tmp_path, limit="50", close=True)
    guard.configure("alpha", Decimal("50"), close_on_trip=False)
    broker = _Broker()

    asyncio.run(_trade(_consolidator(ledger, guard, broker)))

    assert not guard.is_active("alpha")
    assert _held(ledger, "alpha") == 1
    assert len(broker.placed) == 3


def test_no_close_by_default(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    guard = _guard(ledger, tmp_path, limit="50")
    broker = _Broker()

    asyncio.run(_trade(_consolidator(ledger, guard, broker)))

    assert _held(ledger, "alpha") == 1
    assert guard.pop_to_close() == []


def test_close_runs_in_the_strategys_lane(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    guard = _guard(ledger, tmp_path, limit="50", close=True)
    broker = _Broker()
    queue: asyncio.Queue[TargetPosition] = asyncio.Queue()
    consolidator = _consolidator(ledger, guard, broker, queue)

    async def _run() -> None:
        task = asyncio.create_task(consolidator.run())
        _PRICE[0] = "100"
        queue.put_nowait(_signal("alpha", "2"))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(broker.placed) == 1:
                break
        _PRICE[0] = "1"
        queue.put_nowait(_signal("alpha", "-1"))
        queue.put_nowait(_signal("alpha", "5"))  # blocked: tripped
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(broker.placed) == 3:
                break
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run())

    assert [o.side for o in broker.placed] == [
        OrderSide.BUY,
        OrderSide.SELL,
        OrderSide.SELL,
    ]
    assert _held(ledger, "alpha") == 0


def test_periodic_trip_asks_for_a_close_once(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "10", "100")
    guard = _guard(ledger, tmp_path, every=1, close=True, prices=_Prices("80"))

    asyncio.run(guard.check_all())
    asyncio.run(guard.check_all())

    assert guard.pop_to_close() == ["alpha"]
    assert guard.pop_to_close() == []


def test_a_trip_from_before_a_restart_is_not_closed_again(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "10", "100")
    asyncio.run(
        _guard(ledger, tmp_path, every=1, close=True, prices=_Prices("80")).check_all()
    )

    restarted = _guard(ledger, tmp_path, every=1, close=True, prices=_Prices("80"))
    asyncio.run(restarted.check_all())

    assert restarted.pop_to_close() == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("maybe", None),
        ("true", True),
        ("1", True),
        ("false", False),
        ("0", False),
        ("NO", False),
    ],
)
def test_parse_close_on_trip(raw: str | None, expected: bool | None) -> None:
    assert _parse_close_on_trip(raw) is expected
