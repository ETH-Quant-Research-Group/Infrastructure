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
from dashboard import nats_bridge, store
from dashboard.app import app
from dashboard.persistence import DashboardDB

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

    # ---- SQLite persistence: open DB and seed in-memory stores ----
    db = DashboardDB()  # ~/.qrf/dashboard.db
    app.state.db = db
    store.set_persistence(db)

    store.broker_pnl_history.extend(db.load_broker_pnl())
    if store.broker_pnl_history:
        store.broker_pnl_latest = store.broker_pnl_history[-1]

    for sid, records in db.load_strategy_pnl().items():
        store.pnl_history[sid] = records
        if records:
            store.pnl_latest[sid] = records[-1]

    store.orders.extend(db.load_orders())

    for sid, records in db.load_fills().items():
        store.fills_by_strategy[sid] = records

    log.info(
        "Loaded from DB: %d broker_pnl, %d strategy series, %d orders, %d fill series",
        len(store.broker_pnl_history),
        len(store.pnl_history),
        len(store.orders),
        len(store.fills_by_strategy),
    )

    # ---- NATS ----
    nc = await nats.connect(NATS_URL)
    app.state.nc = nc
    await nats_bridge.start(nc)
    log.info("Dashboard ready — NATS connected to %s", NATS_URL)


@app.on_event("shutdown")
async def _shutdown() -> None:
    nc: nats.aio.client.Client = app.state.nc
    await nc.drain()
    if hasattr(app.state, "db"):
        app.state.db.close()
    log.info("Dashboard shutdown — NATS drained")
