"""Feed server — streams market data to NATS on demand.

Listens on ``control.subscribe.<subject>`` for stream requests from strategy
workers and starts the corresponding live feed.  Each subject is streamed at
most once regardless of how many workers request it.  Live subjects carry
**live bars only**.

History is a separate request, ``control.history`` {"topic": ..., "count":
60}: the strategy worker asks for it at start-up and feeds it to the
strategy's ``on_history_bar`` — never to ``on_bar``, so a strategy can't
trade on history (B12).  Only finished bars are returned.

Run with::

    python -m workers.feed_server

Environment variables:

- ``NATS_URL`` (default: ``nats://localhost:4222``)
- ``DATAFEED_SEED_LIVE`` — ``1`` brings back the old behaviour of publishing
  the last 60 bars into the live stream when it starts (off by default).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

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

    from data.types import TimeBar
    from engine.types import BusEvent

log = logging.getLogger(__name__)

_SEED_LIVE = os.environ.get("DATAFEED_SEED_LIVE", "").strip().lower() in ("1", "true", "yes", "on")


async def _stream(
    nc: nats.aio.client.Client,
    subject: str,
    source: AsyncIterable[BusEvent],
) -> None:
    async for event in source:
        await nc.publish(subject, codec.encode(event))


HISTORY_SUBJECT = "control.history"
DEFAULT_HISTORY_BARS = 60
_MAX_HISTORY_BARS = 1000

_INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200,
    "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400,
}


async def fetch_history(
    client: BaseCryptoClient,
    symbol: str,
    interval: str,
    count: int,
    now: datetime | None = None,
) -> list[TimeBar]:
    """The last *count* **finished** bars of *symbol* at *interval*, oldest
    first.  The bar still in progress is left out: it would otherwise be
    seen half-built now and complete again when it closes live."""
    now = now or datetime.now(UTC)
    seconds = _INTERVAL_SECONDS.get(interval)
    if seconds is None:
        raise ValueError(f"unsupported interval {interval!r}")
    start = now - timedelta(seconds=seconds * (count + 1))
    bars = await client.time_bars(symbol, KlineInterval(interval), start=start, end=now)
    finished = sorted((b for b in bars if b.close_time <= now), key=lambda b: b.timestamp)
    return finished[-count:]


async def history_reply(
    clients_by_market: dict[str, BaseCryptoClient], data: bytes
) -> dict[str, Any]:
    """Reply for ``control.history`` {"topic": "futures.ETHUSDT.bars.8h",
    "count": 60}: the finished bars as encoded dicts, or {"error": ...}."""
    try:
        body = json.loads(data or b"{}")
        topic = str(body["topic"])
        count = min(int(body.get("count", DEFAULT_HISTORY_BARS)), _MAX_HISTORY_BARS)
        parts = topic.split(".")
        if len(parts) != 4 or parts[2] != "bars":
            raise ValueError(f"not a bar topic: {topic!r}")
        client = clients_by_market.get(parts[0])
        if client is None:
            raise ValueError(f"no client for market {parts[0]!r}")
        bars = await fetch_history(client, parts[1], parts[3], count)
    except Exception as exc:
        return {"error": str(exc)}
    return {"topic": topic, "bars": [json.loads(codec.encode(b)) for b in bars]}


async def _run_stream(
    subject: str,
    nc: nats.aio.client.Client,
    clients_by_market: dict[str, BaseCryptoClient],
    ref_counts: dict[str, int],
    tasks: dict[str, asyncio.Task[None]],
) -> None:
    parts = subject.split(".")
    market, symbol, stream_type = parts[0], parts[1], parts[2]

    client = clients_by_market.get(market)
    if client is None:
        log.warning("No client for market %r — ignoring %s", market, subject)
        ref_counts.pop(subject, None)
        tasks.pop(subject, None)
        return

    # Retry loop — restarts the stream on failure instead of dying silently.
    while subject in ref_counts:
        try:
            if stream_type == "bars":
                interval_str = parts[3]
                kinterval = KlineInterval(interval_str)
                # Legacy (DATAFEED_SEED_LIVE=1): publish the last 60 bars into
                # the live stream first — strategies then took them for live
                # bars (B12).  Off by default: strategies get history through
                # control.history -> on_history_bar instead.
                if _SEED_LIVE:
                    hours = {
                        "1m": 1 / 60,
                        "5m": 5 / 60,
                        "15m": 0.25,
                        "1h": 1,
                        "4h": 4,
                        "8h": 8,
                        "1d": 24,
                    }.get(interval_str, 8)
                    try:
                        end = datetime.now(UTC)
                        start = end - timedelta(hours=hours * 60)  # last 60 bars
                        hist = await client.time_bars(
                            symbol, kinterval, start=start, end=end
                        )
                        hist_sorted = sorted(hist, key=lambda b: b.timestamp)
                        for bar in hist_sorted:
                            await nc.publish(subject, codec.encode(bar))
                        log.info(
                            "Seeded %d historical bars for %s", len(hist_sorted), subject
                        )
                    except Exception as exc:
                        log.warning(
                            "Could not seed historical bars for %s: %s", subject, exc
                        )
                await _stream(nc, subject, client.live_time_bars(symbol, kinterval))
            elif stream_type == "trades":
                await _stream(nc, subject, client.live_trades(symbol))
            elif stream_type == "funding_rate" and isinstance(
                client, BaseCryptoFuturesClient
            ):
                await _stream(nc, subject, client.live_funding_rates(symbol))
            else:
                log.warning(
                    "Unknown stream type %r in subject %s", stream_type, subject
                )
                break
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Stream %s failed — restarting in 10s", subject)
            await asyncio.sleep(10)

    ref_counts.pop(subject, None)
    tasks.pop(subject, None)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
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

    async def _on_history(msg: nats.aio.msg.Msg) -> None:
        reply = await history_reply(clients_by_market, msg.data)
        await msg.respond(json.dumps(reply).encode())

    await nc.subscribe("control.subscribe.>", cb=_on_subscribe)
    await nc.subscribe("control.unsubscribe.>", cb=_on_unsubscribe)
    await nc.subscribe(HISTORY_SUBJECT, cb=_on_history)
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
