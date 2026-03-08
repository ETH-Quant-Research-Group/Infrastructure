"""Consolidator worker — nets strategy signals and routes broker orders.

Run with::

    python -m workers.consolidator_worker

Environment variables:

- ``NATS_URL``          (default: ``nats://localhost:4222``)
- ``DEFAULT_EXCHANGE``  (default: ``bybit_paper``)  — exchange used when a
                        strategy signal carries no ``exchange`` field.

Available exchanges (add more in ``_build_brokers()``):

    bybit_paper   BybitPaperBroker  — paper fills at live Bybit bid/ask
    paper         PaperBroker       — fills at last bar close, no fees

Strategies select an exchange by setting ``exchange="lighter"`` (or any other
registered name) on the :class:`~interfaces.signals.TargetPosition` they emit.
Signals with no exchange default to ``DEFAULT_EXCHANGE``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import nats
import nats.aio.client
import nats.aio.msg
from execution.brokers.bybit_paper import BybitPaperBroker

from config import NATS_URL
from engine.data import codec
from engine.order.consolidator import OrderConsolidator
from execution.brokers.bybit import BybitBroker
from execution.brokers.paper import PaperBroker
from execution.types import FillConfirmation, Order, OrderSide, OrderType, PerpOrder

if TYPE_CHECKING:
    from interfaces.broker import BaseBroker
    from interfaces.signals import TargetPosition

log = logging.getLogger(__name__)

_DEFAULT_EXCHANGE = os.getenv("DEFAULT_EXCHANGE", "bybit_paper").lower()


# ---------------------------------------------------------------------------
# Broker registry
#
# Add new brokers here.  Key = exchange name used in TargetPosition.exchange.
# ---------------------------------------------------------------------------


def _build_brokers() -> dict[str, BaseBroker]:
    """Instantiate every broker that should be active for this run."""
    brokers: dict[str, BaseBroker] = {
        "bybit_paper": BybitPaperBroker(),
        "paper": PaperBroker(),
        "bybit_demo": BybitBroker(
            demo=True
        ),  # requires BYBIT_API_KEY + BYBIT_API_SECRET
        # "bybit":      BybitBroker(demo=False),  # live trading — be careful
    }
    log.info(
        "Active brokers: %s  |  default: %s",
        list(brokers),
        _DEFAULT_EXCHANGE,
    )
    return brokers


# ---------------------------------------------------------------------------
# Order factory
#
# Returns the correct Order subtype for the target exchange.
# For cross-exchange strategies, inspect `symbol` or add an `exchange` arg
# and return EquityOrder / FXOrder as needed.
# ---------------------------------------------------------------------------


def _order_factory(
    symbol: str, side: OrderSide, quantity: Decimal, price: Decimal
) -> Order:
    order_type = OrderType.LIMIT if price > Decimal(0) else OrderType.MARKET
    return PerpOrder(
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
    )


# ---------------------------------------------------------------------------
# NATS coroutines
# ---------------------------------------------------------------------------


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
    placed_queue: asyncio.Queue[tuple[str, Order]],
) -> None:
    while True:
        exchange, order = await placed_queue.get()
        await nc.publish(
            f"orders.placed.{exchange}.{order.symbol}", codec.encode_order(order)
        )
        log.info(
            "placed order: %s %s %s @ %s",
            order.side,
            order.quantity,
            order.symbol,
            exchange,
        )


async def _publish_broker_pnl(
    nc: nats.aio.client.Client,
    brokers: dict[str, BaseBroker],
    interval: float = 5.0,
) -> None:
    """Publish a PnL snapshot for every broker that exposes pnl properties."""
    while True:
        await asyncio.sleep(interval)
        ts = datetime.now(UTC).isoformat()
        total_realized = Decimal(0)
        total_unrealized = Decimal(0)
        for exchange, broker in brokers.items():
            if not (
                hasattr(broker, "total_realized_pnl")
                and hasattr(broker, "total_unrealized_pnl")
            ):
                continue
            realized = broker.total_realized_pnl
            unrealized = broker.total_unrealized_pnl
            total_realized += realized
            total_unrealized += unrealized
            payload: dict[str, Any] = {
                "exchange": exchange,
                "total_realized": str(realized),
                "total_unrealized": str(unrealized),
                "total": str(realized + unrealized),
                "timestamp": ts,
            }
            # Fetch live wallet balance (AUM) for brokers that support it
            if hasattr(broker, "wallet_balance"):
                wb = await broker.wallet_balance()
                if wb:
                    payload["total_equity"] = wb.get("totalEquity", "")
                    payload["total_wallet_balance"] = wb.get("totalWalletBalance", "")
                    payload["available_balance"] = wb.get("totalAvailableBalance", "")
            # Per-exchange subject so the dashboard can show each broker separately
            await nc.publish(
                f"broker.pnl.{exchange}",
                json.dumps(payload).encode(),
            )
        # Aggregate across all brokers on the plain broker.pnl subject
        await nc.publish(
            "broker.pnl",
            json.dumps(
                {
                    "exchange": "all",
                    "total_realized": str(total_realized),
                    "total_unrealized": str(total_unrealized),
                    "total": str(total_realized + total_unrealized),
                    "timestamp": ts,
                }
            ).encode(),
        )


async def _publish_positions(
    nc: nats.aio.client.Client,
    consolidator: OrderConsolidator,
    interval: float = 5.0,
) -> None:
    """Query every broker for its tracked symbols and publish a unified snapshot."""
    while True:
        await asyncio.sleep(interval)
        snapshot: list[dict[str, Any]] = []
        for exchange, broker in consolidator.brokers.items():
            symbols = consolidator.tracked_symbols_for(exchange)
            for symbol in symbols:
                pos = await broker.position(symbol)
                if pos is not None:
                    snapshot.append(
                        {
                            "symbol": pos.symbol,
                            "exchange": exchange,
                            "quantity": str(pos.quantity),
                            "avg_entry_price": str(pos.avg_entry_price),
                            "unrealized_pnl": str(pos.unrealized_pnl),
                            "realized_pnl": str(pos.realized_pnl),
                        }
                    )
        payload = json.dumps(snapshot).encode()
        await nc.publish("positions.snapshot", payload)
        log.debug("published positions snapshot: %d positions", len(snapshot))


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


async def _feed_market_prices(
    nc: nats.aio.client.Client,
    brokers: dict[str, BaseBroker],
) -> None:
    """Push bar/funding prices to every broker that accepts update_market_price()."""

    def _update_all(symbol: str, price: Decimal) -> None:
        for broker in brokers.values():
            if hasattr(broker, "update_market_price"):
                broker.update_market_price(symbol, price)

    async def _on_bar(msg: nats.aio.msg.Msg) -> None:
        try:
            event = codec.decode(msg.data)
            _update_all(event.symbol, event.close)  # type: ignore[union-attr]
        except Exception as exc:
            log.debug("_on_bar decode error: %s", exc)

    async def _on_funding(msg: nats.aio.msg.Msg) -> None:
        try:
            event = codec.decode(msg.data)
            _update_all(event.symbol, event.mark_price)  # type: ignore[union-attr]
        except Exception as exc:
            log.debug("_on_funding decode error: %s", exc)

    await nc.subscribe("futures.*.bars.*", cb=_on_bar)
    await nc.subscribe("futures.*.funding_rate", cb=_on_funding)
    await asyncio.get_running_loop().create_future()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    brokers = _build_brokers()
    target_queue: asyncio.Queue[TargetPosition] = asyncio.Queue()
    placed_queue: asyncio.Queue[tuple[str, Order]] = asyncio.Queue()
    fills_queue: asyncio.Queue[FillConfirmation] = asyncio.Queue()

    consolidator = OrderConsolidator(
        target_queue=target_queue,
        brokers=brokers,
        default_exchange=_DEFAULT_EXCHANGE,
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
            tg.create_task(_publish_positions(nc, consolidator))
            tg.create_task(_publish_broker_pnl(nc, brokers))
            tg.create_task(_feed_market_prices(nc, brokers))
    finally:
        for broker in brokers.values():
            await broker.aclose()
        await nc.drain()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped")
