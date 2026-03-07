"""Consolidator worker — nets strategy signals and places broker orders.

Run with::

    python -m workers.consolidator_worker

Environment variables:

- ``NATS_URL`` (default: ``nats://localhost:4222``)

**Broker configuration**: edit ``_make_broker()`` and ``_order_factory()`` below
to return your configured broker instance and matching order type before running.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import nats
import nats.aio.client
import nats.aio.msg

from config import NATS_URL
from engine.data import codec
from engine.order.consolidator import OrderConsolidator
from execution.types import FillConfirmation, Order, OrderSide, OrderType, PerpOrder

if TYPE_CHECKING:
    from interfaces.broker import BaseBroker
    from interfaces.signals import TargetPosition

log = logging.getLogger(__name__)


class _PaperBroker:
    """Stub broker that logs orders instead of sending them to an exchange."""

    def __init__(self) -> None:
        self._positions: dict[str, Decimal] = {}

    async def place_order(self, order: object) -> object:
        from execution.types import OrderResult

        log.info("PAPER  place_order  %s", order)
        assert isinstance(order, Order)
        delta = order.quantity if order.side == OrderSide.BUY else -order.quantity
        self._positions[order.symbol] = (
            self._positions.get(order.symbol, Decimal(0)) + delta
        )
        return OrderResult(order_id="paper-0", order=order, error=None)

    async def cancel_order(self, order_id: str) -> object:
        from execution.types import OrderResult

        return OrderResult(order_id=order_id, order=None, error=None)

    async def cancel_all_orders(self, symbol: str | None = None) -> object:
        from execution.types import OrderResult

        return OrderResult(order_id=None, order=None, error=None)

    async def open_orders(self, symbol: str | None = None) -> list:
        return []

    async def position(self, symbol: str) -> object | None:
        from execution.types import Position

        qty = self._positions.get(symbol, Decimal(0))
        if qty == Decimal(0):
            return None
        return Position(
            symbol=symbol,
            quantity=qty,
            avg_entry_price=Decimal(0),
            unrealized_pnl=Decimal(0),
            realized_pnl=Decimal(0),
        )

    async def aclose(self) -> None:
        pass


def _make_broker() -> BaseBroker:
    return _PaperBroker()  # type: ignore[return-value]


def _order_factory(
    symbol: str, side: OrderSide, quantity: Decimal, price: Decimal
) -> Order:
    """Produce the correct order type for the configured broker.

    price=0 → market order, price>0 → limit order.
    Swap PerpOrder for EquityOrder / FXOrder when connecting a different broker.
    """
    order_type = OrderType.LIMIT if price > Decimal(0) else OrderType.MARKET
    return PerpOrder(
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
    )


async def _bridge_targets(
    nc: nats.aio.client.Client,
    queue: asyncio.Queue[TargetPosition],
) -> None:
    async def _cb(msg: nats.aio.msg.Msg) -> None:
        queue.put_nowait(codec.decode_target(msg.data))

    await nc.subscribe("signals.targets.*", cb=_cb)
    await asyncio.get_running_loop().create_future()


async def _publish_placed_orders(
    nc: nats.aio.client.Client,
    placed_queue: asyncio.Queue[Order],
) -> None:
    while True:
        order = await placed_queue.get()
        await nc.publish(f"orders.placed.{order.symbol}", codec.encode_order(order))
        log.info("placed order: %s %s %s", order.side, order.quantity, order.symbol)


async def _publish_fills(
    nc: nats.aio.client.Client,
    fills_queue: asyncio.Queue[FillConfirmation],
) -> None:
    while True:
        fill = await fills_queue.get()
        await nc.publish(f"fills.{fill.strategy_id}", codec.encode_fill(fill))
        log.info(
            "fill: %s qty=%s price=%s → %s",
            fill.symbol,
            fill.quantity,
            fill.fill_price,
            fill.strategy_id,
        )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    broker = _make_broker()
    target_queue: asyncio.Queue[TargetPosition] = asyncio.Queue()
    placed_queue: asyncio.Queue[Order] = asyncio.Queue()
    fills_queue: asyncio.Queue[FillConfirmation] = asyncio.Queue()

    consolidator = OrderConsolidator(
        target_queue=target_queue,
        broker=broker,
        min_order_size=Decimal("0.001"),
        placed_queue=placed_queue,
        fills_queue=fills_queue,
        order_factory=_order_factory,
    )

    nc = await nats.connect(NATS_URL)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_bridge_targets(nc, target_queue))
            tg.create_task(consolidator.run())
            tg.create_task(_publish_placed_orders(nc, placed_queue))
            tg.create_task(_publish_fills(nc, fills_queue))
    finally:
        await broker.aclose()
        await nc.drain()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped")
