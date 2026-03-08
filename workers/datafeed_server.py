"""Feed server — streams market data to NATS on demand.

Listens on ``control.subscribe.<subject>`` for stream requests from strategy
workers and starts the corresponding live feed.  Each subject is streamed at
most once regardless of how many workers request it.

Run with::

    python -m workers.feed_server

Environment variables:

- ``NATS_URL`` (default: ``nats://localhost:4222``)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import nats
import nats.aio.client
import nats.aio.msg

import clients as _clients_pkg
from config import NATS_URL
from data.connectors.types import KlineInterval
from engine.data import codec
from interfaces.client import BaseCryptoClient, BaseCryptoFuturesClient
from utils.pckgs import discover_subclasses

if TYPE_CHECKING:
    from collections.abc import AsyncIterable

    from engine.types import BusEvent

log = logging.getLogger(__name__)


async def _stream(
    nc: nats.aio.client.Client,
    subject: str,
    source: AsyncIterable[BusEvent],
) -> None:
    async for event in source:
        await nc.publish(subject, codec.encode(event))


async def _run_stream(
    subject: str,
    nc: nats.aio.client.Client,
    clients_by_market: dict[str, BaseCryptoClient],
    ref_counts: dict[str, int],
    tasks: dict[str, asyncio.Task[None]],
) -> None:
    try:
        parts = subject.split(".")
        market, symbol, stream_type = parts[0], parts[1], parts[2]

        client = clients_by_market.get(market)
        if client is None:
            log.warning("No client for market %r — ignoring %s", market, subject)
            return

        if stream_type == "bars":
            await _stream(
                nc, subject, client.live_time_bars(symbol, KlineInterval(parts[3]))
            )
        elif stream_type == "trades":
            await _stream(nc, subject, client.live_trades(symbol))
        elif stream_type == "funding_rate" and isinstance(
            client, BaseCryptoFuturesClient
        ):
            await _stream(nc, subject, client.live_funding_rates(symbol))
        else:
            log.warning("Unknown stream type %r in subject %s", stream_type, subject)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Stream %s failed", subject)
    finally:
        ref_counts.pop(subject, None)
        tasks.pop(subject, None)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    client_classes = discover_subclasses(_clients_pkg, BaseCryptoClient)  # type: ignore[type-abstract]
    clients = [cls() for cls in client_classes]

    clients_by_market: dict[str, BaseCryptoClient] = {}
    for client in clients:
        if isinstance(client, BaseCryptoFuturesClient):
            clients_by_market["futures"] = client
        else:
            clients_by_market["spot"] = client

    log.info(
        "discovered %d client(s): %s",
        len(clients),
        [type(c).__name__ for c in clients],
    )

    nc = await nats.connect(NATS_URL)
    ref_counts: dict[str, int] = {}
    tasks: dict[str, asyncio.Task[None]] = {}

    async def _on_subscribe(msg: nats.aio.msg.Msg) -> None:
        subject = msg.subject.removeprefix("control.subscribe.")
        ref_counts[subject] = ref_counts.get(subject, 0) + 1
        if ref_counts[subject] == 1:
            log.info("Starting stream: %s", subject)
            tasks[subject] = asyncio.create_task(
                _run_stream(subject, nc, clients_by_market, ref_counts, tasks)
            )

    async def _on_unsubscribe(msg: nats.aio.msg.Msg) -> None:
        subject = msg.subject.removeprefix("control.unsubscribe.")
        if subject not in ref_counts:
            return
        ref_counts[subject] -= 1
        if ref_counts[subject] <= 0:
            log.info("Stopping stream: %s", subject)
            tasks.pop(subject).cancel()
            ref_counts.pop(subject)

    await nc.subscribe("control.subscribe.>", cb=_on_subscribe)
    await nc.subscribe("control.unsubscribe.>", cb=_on_unsubscribe)
    log.info("Ready — waiting for control messages on control.subscribe.>")

    try:
        await asyncio.get_running_loop().create_future()  # block until cancelled
    finally:
        for client in clients:
            await client.aclose()
        await nc.drain()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped")
