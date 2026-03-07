"""NATS → WebSocket bridge.

Subscribes to NATS subjects and forwards messages to all connected
WebSocket clients via the ConnectionManager.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import nats.aio.client

if TYPE_CHECKING:
    import nats.aio.msg

from dashboard.store import (
    record_bar,
    record_fill,
    record_order,
    record_pnl,
    record_positions,
    register_strategy,
    unregister_strategy,
)
from dashboard.ws.manager import manager

log = logging.getLogger(__name__)

# Subjects to relay to the frontend.
_SUBSCRIPTIONS = [
    "futures.>",
    "signals.>",
    "orders.>",
    "fills.>",
    "positions.>",
    "strategy.>",
    "pnl.>",
]


async def start(nc: nats.aio.client.Client) -> None:
    async def _relay(msg: nats.aio.msg.Msg) -> None:
        try:
            data = json.loads(msg.data)
        except Exception:
            data = msg.data.decode()

        if (
            msg.subject.startswith("futures.")
            and ".bars." in msg.subject
            and isinstance(data, dict)
        ):
            record_bar(data, msg.subject)
        elif msg.subject.startswith("orders.placed.") and isinstance(data, dict):
            record_order(data)
        elif msg.subject.startswith("fills.") and isinstance(data, dict):
            record_fill(data)
        elif msg.subject.startswith("strategy.register.") and isinstance(data, dict):
            register_strategy(data)
        elif msg.subject.startswith("strategy.unregister."):
            name = msg.subject.removeprefix("strategy.unregister.")
            unregister_strategy(name)
        elif msg.subject.startswith("pnl.") and isinstance(data, dict):
            record_pnl(data)
        elif msg.subject == "positions.snapshot" and isinstance(data, list):
            record_positions(data)

        await manager.broadcast({"subject": msg.subject, "data": data})

    for subject in _SUBSCRIPTIONS:
        await nc.subscribe(subject, cb=_relay)
        log.info("NATS bridge subscribed: %s", subject)
