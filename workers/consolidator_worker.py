"""Consolidator worker — nets strategy signals and places broker orders.

Run with::

    python -m workers.consolidator_worker

Environment variables:

- ``NATS_URL`` (default: ``nats://localhost:4222``)

**Broker configuration**: edit ``_make_broker()`` below to return your
configured broker instance before running this worker.
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

if TYPE_CHECKING:
    from interfaces.broker import BaseBroker
    from interfaces.signals import TargetPosition

log = logging.getLogger(__name__)


def _make_broker() -> BaseBroker:
    """Return the broker used by the consolidator.

    Edit this function to wire up your preferred broker, e.g.::

        import os
        from execution.brokers.lighter import LighterBroker

        return LighterBroker(
            account_index=int(os.environ["LIGHTER_ACCOUNT"]),
            api_private_keys={0: os.environ["LIGHTER_KEY"]},
            symbol_map={"BTC-USDC": 0},
        )
    """
    raise NotImplementedError(
        "Configure a broker in workers/consolidator_worker.py::_make_broker()"
    )


async def _bridge_targets(
    nc: nats.aio.client.Client,
    queue: asyncio.Queue[TargetPosition],
) -> None:
    async def _cb(msg: nats.aio.msg.Msg) -> None:
        queue.put_nowait(codec.decode_target(msg.data))

    await nc.subscribe("signals.targets.*", cb=_cb)
    await asyncio.get_running_loop().create_future()  # block until cancelled


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    broker = _make_broker()
    target_queue: asyncio.Queue[TargetPosition] = asyncio.Queue()
    consolidator = OrderConsolidator(
        target_queue=target_queue,
        broker=broker,
        min_order_size=Decimal("0.001"),
    )

    nc = await nats.connect(NATS_URL)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_bridge_targets(nc, target_queue))
            tg.create_task(consolidator.run())
    finally:
        await broker.aclose()
        await nc.drain()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped")
