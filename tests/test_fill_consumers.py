"""What the strategy worker and the webapp do with fill reports."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from engine.data import codec
from engine.strategy.pnl_calc import PnLCalc
from execution.types import FillConfirmation, FillStatus
from webapp import store
from workers.strategy_worker import _listen_fills

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import pytest


def _fill(qty: str, status: FillStatus = FillStatus.FILLED) -> FillConfirmation:
    return FillConfirmation(
        strategy_id="alpha",
        symbol="ETHUSDT",
        quantity=Decimal(qty),
        fill_price=Decimal("2500") if qty != "0" else Decimal(0),
        exchange="bybit_demo",
        status=status,
        requested_qty=Decimal("0.5"),
    )


class _Runner:
    def __init__(self) -> None:
        self.pnl_calc = PnLCalc()
        self.notified: list[FillConfirmation] = []

    async def notify_fill(self, fill: FillConfirmation) -> None:
        self.notified.append(fill)


class _NC:
    def __init__(self) -> None:
        self.callbacks: dict[str, Callable[[Any], Awaitable[None]]] = {}

    async def subscribe(self, subject: str, cb: Callable[[Any], Awaitable[None]]) -> None:
        self.callbacks[subject] = cb


def _deliver(*fills: FillConfirmation) -> _Runner:
    """Run _listen_fills against a fake NATS and push *fills* through it."""
    runner = _Runner()
    nc = _NC()

    async def _run() -> None:
        task = asyncio.create_task(_listen_fills(nc, "alpha", runner))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        cb = nc.callbacks["fills.alpha"]
        for fill in fills:
            await cb(SimpleNamespace(data=codec.encode_fill(fill)))
        task.cancel()

    asyncio.run(_run())
    return runner


def test_strategy_worker_passes_real_fills_on() -> None:
    runner = _deliver(_fill("-0.2", FillStatus.PARTIAL))

    assert [f.quantity for f in runner.notified] == [Decimal("-0.2")]
    assert runner.pnl_calc.latest("ETHUSDT") is not None


def test_strategy_worker_drops_zero_quantity_reports() -> None:
    runner = _deliver(
        _fill("0", FillStatus.REJECTED),
        _fill("0", FillStatus.DROPPED),
        _fill("0", FillStatus.UNKNOWN),
    )

    assert runner.notified == []
    assert runner.pnl_calc.latest("ETHUSDT") is None


def test_store_records_only_executed_fills(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "fills_by_strategy", {})
    monkeypatch.setattr(store, "_db", None)

    for qty in ("0", "-0.00", "0E-8", "0.5", "-0.25"):
        store.record_fill({"strategy_id": "alpha", "symbol": "ETHUSDT", "quantity": qty})

    assert [r["quantity"] for r in store.fills_by_strategy["alpha"]] == ["0.5", "-0.25"]
