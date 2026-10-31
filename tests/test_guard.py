from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from engine.order.consolidator import OrderConsolidator
from engine.order.funding import FundingDB
from engine.order.guard_registry import StrategyGuardRegistry
from engine.order.ledger import LedgerDB
from engine.strategy.guard import StrategyGuard
from execution.types import (
    FillConfirmation,
    FillStatus,
    FundingSettlement,
    Order,
    OrderResult,
    OrderSide,
)
from tests.fakes import FakeBroker
from interfaces.signals import TargetPosition
from workers import guard_control, strategy_worker
from workers.consolidator_worker import _env_flag

if TYPE_CHECKING:
    from pathlib import Path


def _fill(ledger: LedgerDB, qty: str, price: str, strategy: str = "alpha") -> None:
    side = OrderSide.BUY if Decimal(qty) > 0 else OrderSide.SELL
    tag = ledger.begin_order(strategy, "bybit_demo", "ETHUSDT", side, abs(Decimal(qty)))
    ledger.record_result(FillConfirmation(
        strategy_id=strategy, symbol="ETHUSDT", quantity=Decimal(qty),
        fill_price=Decimal(price), exchange="bybit_demo", status=FillStatus.FILLED,
        fees={"USDT": Decimal("0.5")}, order_link_id=tag,
    ))


class _Prices:
    def __init__(self, price: str | None) -> None:
        self.price = price

    async def __call__(self, exchange: str, symbol: str) -> Decimal | None:
        return Decimal(self.price) if self.price else None


def _guard(ledger: LedgerDB, tmp_path: Path, unrealized: bool = False,
           prices: _Prices | None = None, limit: str = "100") -> StrategyGuardRegistry:
    guard = StrategyGuardRegistry(
        Decimal(limit), ledger=ledger, funding_path=tmp_path / "funding.db",
        prices=prices or _Prices("100"), include_unrealized=unrealized,
    )
    return guard


# ------------------------------------------------------------- what it counts


def test_realized_loss_and_fees_trip_the_guard(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    guard = _guard(ledger, tmp_path)
    _fill(ledger, "1", "100")
    _fill(ledger, "-1", "1.5")  # realized −98.5, fees 1 → −99.5: still inside

    assert guard.check("alpha") == Decimal("-99.5")
    assert guard.is_active("alpha")

    _fill(ledger, "1", "100")  # one more fee: −100 is not *below* −100
    assert guard.check("alpha") == Decimal("-100.0")
    assert guard.is_active("alpha")
    _fill(ledger, "-1", "99.9")  # −0.1 realized, −0.5 fees
    guard.check("alpha")
    assert not guard.is_active("alpha")
    assert guard.pop_newly_tripped() == ["alpha"]
    assert guard.pop_newly_tripped() == []  # reported once


def test_funding_counts_toward_the_guard(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "-1", "100")
    FundingDB(tmp_path / "funding.db").record(
        FundingSettlement(settlement_id="s", symbol="ETHUSDT",
                          settled_at=datetime(2026, 10, 18, tzinfo=UTC),
                          amount=Decimal("-150"), position=Decimal(-1)),
        {"alpha": Decimal("-150")},
    )
    guard = _guard(ledger, tmp_path)

    assert guard.check("alpha") == Decimal("-150.5")
    assert not guard.is_active("alpha")


def test_open_loss_is_ignored_by_default(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "10", "100")
    guard = _guard(ledger, tmp_path, prices=_Prices("50"))  # −500 unrealized

    asyncio.run(guard.check_all())

    assert guard.is_active("alpha")


def test_open_loss_trips_when_unrealized_counts(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "10", "100")
    prices = _Prices("95")
    guard = _guard(ledger, tmp_path, unrealized=True, prices=prices)

    asyncio.run(guard.check_all())
    assert guard.is_active("alpha")  # −50.5

    prices.price = "89"
    asyncio.run(guard.check_all())  # −110.5
    assert not guard.is_active("alpha")


def test_missing_price_neither_trips_nor_relaxes(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "10", "100")
    guard = _guard(ledger, tmp_path, unrealized=True, prices=_Prices(None))

    asyncio.run(guard.check_all())

    assert guard.check("alpha") is None
    assert guard.is_active("alpha")


def test_unrealized_needs_a_price_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        StrategyGuardRegistry(
            Decimal(1), ledger=LedgerDB(tmp_path / "ledger.db"), include_unrealized=True
        )


# -------------------------------------------------------------- latching


def test_trip_latches_when_the_price_recovers(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "10", "100")
    prices = _Prices("80")
    guard = _guard(ledger, tmp_path, unrealized=True, prices=prices)
    asyncio.run(guard.check_all())
    assert not guard.is_active("alpha")

    prices.price = "120"
    asyncio.run(guard.check_all())
    assert not guard.is_active("alpha")


def test_trip_survives_a_restart_and_is_not_reported_again(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "1", "100")
    _fill(ledger, "-1", "0.5")
    first = _guard(ledger, tmp_path)
    first.check("alpha")
    assert first.pop_newly_tripped() == ["alpha"]
    ledger.close()

    restarted = _guard(LedgerDB(tmp_path / "ledger.db"), tmp_path)

    assert not restarted.is_active("alpha")
    assert restarted.pop_newly_tripped() == []  # its container was stopped already


# ----------------------------------------------------------------- reset


def test_reset_rearms_from_the_current_pnl(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "1", "100")
    _fill(ledger, "-1", "0.5")  # −99.5 realized, −1 fees
    guard = _guard(ledger, tmp_path)
    guard.check("alpha")

    assert guard.reset("alpha") == Decimal("-100.5")
    assert guard.is_active("alpha")
    assert guard.check("alpha") == Decimal("0")  # counted from the reset

    _fill(ledger, "1", "100")
    _fill(ledger, "-1", "1.5")  # another −99.5
    guard.check("alpha")
    assert guard.is_active("alpha")
    _fill(ledger, "1", "100")
    _fill(ledger, "-1", "99")
    guard.check("alpha")
    assert not guard.is_active("alpha")  # −101.5 since the reset


def test_reset_baseline_survives_a_restart(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "1", "100")
    _fill(ledger, "-1", "0.5")
    guard = _guard(ledger, tmp_path)
    guard.check("alpha")
    guard.reset("alpha")
    ledger.close()

    restarted = _guard(LedgerDB(tmp_path / "ledger.db"), tmp_path)

    assert restarted.is_active("alpha")
    assert restarted.check("alpha") == Decimal("0")


def test_reset_errors(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "10", "100")
    prices = _Prices("80")
    guard = _guard(ledger, tmp_path, unrealized=True, prices=prices)

    with pytest.raises(ValueError, match="not tripped"):
        guard.reset("alpha")
    asyncio.run(guard.check_all())
    prices.price = None
    asyncio.run(guard.check_all())  # prices now unknown
    with pytest.raises(ValueError, match="can't price"):
        guard.reset("alpha")


def test_status_lists_every_strategy(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "1", "100")
    _fill(ledger, "-1", "0.5")
    _fill(ledger, "1", "100", strategy="beta")
    guard = _guard(ledger, tmp_path)
    guard.configure("beta", Decimal("500"))
    guard.check("alpha")

    alpha, beta = guard.status()

    assert (alpha["strategy"], alpha["tripped"], alpha["tripped_pnl"]) == ("alpha", True, "-100.5")
    assert (beta["strategy"], beta["tripped"], beta["max_loss"], beta["pnl"]) == (
        "beta", False, "500", "-0.5"
    )
    text = guard_control.format_status(guard.status())
    assert "alpha: TRIPPED at" in text
    assert "beta: active; PnL -0.5 (realized + funding - fees, since reset at 0), limit -500" in text


# ---------------------------------------------------------- consolidator


class _Broker(FakeBroker):
    """Fills instantly at the current _PRICE."""

    async def place_order(self, order: Order) -> OrderResult:
        self.fill_price = Decimal(_PRICE[0])
        return await super().place_order(order)


_PRICE = ["100"]


def test_consolidator_drops_signals_once_the_fill_trips_the_guard(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    guard = _guard(ledger, tmp_path, limit="50")
    broker = _Broker()
    consolidator = OrderConsolidator(
        target_queue=asyncio.Queue(), brokers={"bybit_demo": broker},
        default_exchange="bybit_demo", guard_registry=guard, ledger=ledger,
    )

    async def signal(qty: str, price: str) -> None:
        _PRICE[0] = price
        await consolidator._reconcile(
            TargetPosition(symbol="ETHUSDT", quantity=Decimal(qty), strategy_id="alpha")
        )

    async def _run() -> None:
        await signal("1", "100")
        await signal("-1", "1")  # −99 realized → trips on this fill
        await signal("1", "100")  # dropped

    asyncio.run(_run())

    assert not guard.is_active("alpha")
    assert len(ledger.trades("alpha")) == 2


def test_blocked_signal_is_answered_with_the_trip_details(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "1", "100")
    _fill(ledger, "-1", "0.5")  # −100.5 → trips the 100 limit
    guard = _guard(ledger, tmp_path)
    guard.check("alpha")
    fills: asyncio.Queue[FillConfirmation] = asyncio.Queue()
    consolidator = OrderConsolidator(
        target_queue=asyncio.Queue(), brokers={"bybit_demo": _Broker()},
        default_exchange="bybit_demo", fills_queue=fills, guard_registry=guard, ledger=ledger,
    )

    asyncio.run(consolidator._reconcile(
        TargetPosition(symbol="ETHUSDT", quantity=Decimal(1), strategy_id="alpha")
    ))

    fill = fills.get_nowait()
    assert fill.status is FillStatus.BLOCKED
    assert fill.reason is not None
    assert fill.reason.startswith("guard tripped for alpha at ")
    assert fill.reason.endswith(
        "(PnL -100.5, realized + funding - fees, below -100) - signal not sent"
    )
    assert len(ledger.trades("alpha")) == 2  # nothing new was sent
    assert guard.describe_trip("beta") is None


# --------------------------------------------------------- legacy / switches


def test_legacy_mode_is_unchanged_without_a_ledger() -> None:
    guard = StrategyGuardRegistry(Decimal("50"))
    guard.record_fill("alpha", "ETHUSDT", "bybit_demo", Decimal(1), Decimal(100))
    guard.record_fill("alpha", "ETHUSDT", "bybit_demo", Decimal(-1), Decimal(1))

    assert not guard.is_active("alpha")
    assert guard.pop_newly_tripped() == ["alpha"]


@pytest.mark.parametrize(("raw", "expected"), [
    ("", False), ("false", False), ("0", False), ("true", True), ("1", True), ("YES", True),
])
def test_env_flag(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("SOME_FLAG", raw)
    assert _env_flag("SOME_FLAG") is expected


@pytest.mark.parametrize("feed", [False, True])
def test_old_in_container_guard_is_fed_only_when_switched_on(feed: bool) -> None:
    guard = StrategyGuard(max_loss=Decimal("1000"))
    published: list[str] = []

    class _NC:
        cb: Any = None

        async def subscribe(self, subject: str, cb: Any) -> None:
            self.cb = cb

        async def publish(self, subject: str, data: bytes) -> None:
            published.append(subject)

    nc = _NC()

    async def _run() -> None:
        task = asyncio.create_task(
            strategy_worker._bridge_broker_pnl_to_strategy(nc, "alpha", guard, feed_guard=feed)  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
        for total in ("0", "-300"):
            await nc.cb(SimpleNamespace(data=json.dumps({"total": total}).encode()))
        task.cancel()

    asyncio.run(_run())

    assert guard._total_pnl == (Decimal(-300) if feed else Decimal(0))
    assert published == ["pnl.alpha", "pnl.alpha"]  # the dashboard bridge still runs
