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

from dashboard.ws.manager import manager

log = logging.getLogger(__name__)

# Subjects to relay to the frontend.
_SUBSCRIPTIONS = [
    "futures.>",
    "signals.>",
]


async def start(nc: nats.aio.client.Client) -> None:
    async def _relay(msg: nats.aio.msg.Msg) -> None:
        try:
            data = json.loads(msg.data)
        except Exception:
            data = msg.data.decode()
        await manager.broadcast({"subject": msg.subject, "data": data})

    for subject in _SUBSCRIPTIONS:
        await nc.subscribe(subject, cb=_relay)
        log.info("NATS bridge subscribed: %s", subject)
