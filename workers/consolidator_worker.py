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
import json
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import nats
import nats.aio.client
import nats.aio.msg

from config import NATS_URL
from engine.data import codec
from engine.order.consolidator import OrderConsolidator
from execution.types import (
    FillConfirmation,
    Order,
    OrderResult,
    OrderSide,
    OrderType,
    PerpOrder,
    Position,
)
from interfaces.broker import BaseBroker

if TYPE_CHECKING:
    from interfaces.signals import TargetPosition

log = logging.getLogger(__name__)


class _PaperPosition:
    __slots__ = ("qty", "avg_entry", "realized_pnl", "last_price")

    def __init__(self) -> None:
        self.qty = Decimal(0)
        self.avg_entry = Decimal(0)
        self.realized_pnl = Decimal(0)
        self.last_price = Decimal(0)


class _PaperBroker(BaseBroker):
    """Stub broker that simulates fills locally without hitting an exchange.

    Tracks average entry price and realized PnL the same way PnLCalc does.
    Unrealized PnL is estimated using the last fill price as a market proxy.
    """

    def __init__(self) -> None:
        self._positions: dict[str, _PaperPosition] = {}

    async def place_order(self, order: Order) -> OrderResult:
        log.info("PAPER  place_order  %s", order)

        pos = self._positions.setdefault(order.symbol, _PaperPosition())
        delta = order.quantity if order.side == OrderSide.BUY else -order.quantity
        fill_price = order.price if order.price > Decimal(0) else pos.last_price
        old_qty = pos.qty
        new_qty = old_qty + delta

        if old_qty == Decimal(0):
            pos.avg_entry = fill_price
        elif (old_qty > 0) == (delta > 0):
            # Adding to position — update weighted average entry
            total_cost = old_qty * pos.avg_entry + delta * fill_price
            pos.avg_entry = total_cost / new_qty
        else:
            # Reducing or flipping — realize PnL on closed portion
            closed = min(abs(delta), abs(old_qty))
            if old_qty > 0:
                pos.realized_pnl += (fill_price - pos.avg_entry) * closed
            else:
                pos.realized_pnl += (pos.avg_entry - fill_price) * closed
            if new_qty == Decimal(0):
                pos.avg_entry = Decimal(0)
            elif (old_qty > 0) != (new_qty > 0):
                pos.avg_entry = fill_price

        pos.qty = new_qty
        pos.last_price = fill_price
        return OrderResult(
            order_id="paper-0", order=order, error=None, fill_price=fill_price
        )

    async def cancel_order(self, order_id: str) -> OrderResult:
        return OrderResult(order_id=order_id, order=None, error=None)

    async def cancel_all_orders(self, symbol: str | None = None) -> OrderResult:
        return OrderResult(order_id=None, order=None, error=None)

    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        return []

    async def position(self, symbol: str) -> Position | None:
        pos = self._positions.get(symbol)
        if pos is None or pos.qty == Decimal(0):
            return None

        if pos.qty > 0:
            unrealized = (pos.last_price - pos.avg_entry) * pos.qty
        else:
            unrealized = (pos.avg_entry - pos.last_price) * abs(pos.qty)

        return Position(
            symbol=symbol,
            quantity=pos.qty,
            avg_entry_price=pos.avg_entry,
            unrealized_pnl=unrealized,
            realized_pnl=pos.realized_pnl,
        )

    async def aclose(self) -> None:
        pass


def _make_broker() -> BaseBroker:
    return _PaperBroker()


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


async def _publish_positions(
    nc: nats.aio.client.Client,
    consolidator: OrderConsolidator,
    broker: BaseBroker,
    interval: float = 5.0,
) -> None:
    """Periodically query the broker for all tracked positions and broadcast."""
    while True:
        await asyncio.sleep(interval)
        symbols = consolidator.tracked_symbols
        if not symbols:
            continue
        snapshot: list[dict] = []
        for symbol in symbols:
            pos = await broker.position(symbol)
            if pos is not None:
                snapshot.append(
                    {
                        "symbol": pos.symbol,
                        "quantity": str(pos.quantity),
                        "avg_entry_price": str(pos.avg_entry_price),
                        "unrealized_pnl": str(pos.unrealized_pnl),
                        "realized_pnl": str(pos.realized_pnl),
                    }
                )
        payload = json.dumps(snapshot).encode()
        await nc.publish("positions.snapshot", payload)
        log.debug("published positions snapshot: %d symbols", len(snapshot))


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
            tg.create_task(_publish_positions(nc, consolidator, broker))
    finally:
        await broker.aclose()
        await nc.drain()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped")
