"""Dashboard pusher — subscribes to NATS and forwards events to the dashboard
via HTTP POST, so the dashboard can run anywhere (local or remote).

Run with:

    uv run python -m workers.dashboard

Environment variables:

- ``NATS_URL``      (default: ``nats://localhost:4222``)
- ``DASHBOARD_URL`` (default: ``http://localhost:8000``)
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import nats
import nats.aio.msg

from config import DASHBOARD_URL, NATS_URL

log = logging.getLogger(__name__)

_SUBSCRIPTIONS = [
    "futures.>",
    "signals.>",
    "orders.>",
    "fills.>",
    "positions.>",
    "strategy.>",
    "pnl.>",
    "broker.>",
]


async def _post(client: httpx.AsyncClient, path: str, data: object) -> None:
    try:
        await client.post(f"{DASHBOARD_URL}/api/v1/push{path}", json=data, timeout=5)
    except Exception as exc:
        log.warning("push %s failed: %s", path, exc)


async def _handle(msg: nats.aio.msg.Msg, client: httpx.AsyncClient) -> None:
    try:
        data = json.loads(msg.data)
    except Exception:
        data = msg.data.decode()

    subj = msg.subject

    if subj == "positions.snapshot" and isinstance(data, list):
        await _post(client, "/positions", data)
    elif subj.startswith("pnl.") and isinstance(data, dict):
        await _post(client, "/pnl", data)
    elif isinstance(data, dict) and (
        subj == "broker.pnl" or subj.startswith("broker.pnl.")
    ):
        await _post(client, "/broker-pnl", data)
    elif subj.startswith("orders.placed.") and isinstance(data, dict):
        await _post(client, "/orders", data)
    elif subj.startswith("fills.") and isinstance(data, dict):
        await _post(client, "/fills", data)
    elif subj.startswith("futures.") and ".bars." in subj and isinstance(data, dict):
        await _post(client, "/bars", {"subject": subj, "data": data})
    elif subj.startswith("strategy.register.") and isinstance(data, dict):
        await _post(client, "/strategy/register", data)
    elif subj.startswith("strategy.unregister."):
        name = subj.removeprefix("strategy.unregister.")
        await _post(client, "/strategy/unregister", {"name": name})


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    nc = await nats.connect(NATS_URL)
    log.info("Connected to NATS at %s", NATS_URL)
    log.info("Pushing to dashboard at %s", DASHBOARD_URL)

    async with httpx.AsyncClient() as client:

        async def _cb(msg: nats.aio.msg.Msg) -> None:
            await _handle(msg, client)

        for subject in _SUBSCRIPTIONS:
            await nc.subscribe(subject, cb=_cb)

        log.info("Subscribed — forwarding to %s/api/v1/push/...", DASHBOARD_URL)
        try:
            await asyncio.Future()  # run forever
        except asyncio.CancelledError:
            pass
        finally:
            await nc.drain()
            log.info("NATS drained")


if __name__ == "__main__":
    asyncio.run(main())
