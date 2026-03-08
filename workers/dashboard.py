"""Dashboard server — serves the REST API, WebSocket feed, and static frontend.

Connects to NATS on startup and starts the bridge that relays market data
and strategy signals to connected browser clients.

Run with:

    uv run uvicorn workers.dashboard:app --reload --port 8000

Environment variables:

- ``NATS_URL`` (default: ``nats://localhost:4222``)
"""

from __future__ import annotations

import logging

import nats

from config import NATS_URL
from dashboard import nats_bridge
from dashboard.app import app

log = logging.getLogger(__name__)


@app.on_event("startup")
async def _startup() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress uvicorn's per-connection lifecycle noise
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    nc = await nats.connect(NATS_URL)
    app.state.nc = nc
    await nats_bridge.start(nc)
    log.info("Dashboard ready — NATS connected to %s", NATS_URL)


@app.on_event("shutdown")
async def _shutdown() -> None:
    nc: nats.aio.client.Client = app.state.nc
    await nc.drain()
    log.info("Dashboard shutdown — NATS drained")
