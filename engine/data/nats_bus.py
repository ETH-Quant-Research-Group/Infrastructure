from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from typing import TYPE_CHECKING

from engine.data import codec

if TYPE_CHECKING:
    import nats.aio.client
    import nats.aio.msg
    import nats.aio.subscription

    from engine.types import BusEvent


class NatsBus:
    """Drop-in replacement for DataBus backed by a live NATS connection.

    Call :meth:`subscribe` at any time — before or after :meth:`start`.
    Call :meth:`start` once to register all pending NATS subscriptions.
    Subscriptions added after :meth:`start` are registered immediately via
    ``asyncio.ensure_future``.  Each NATS subject is subscribed at most once
    regardless of how many queues listen to it.
    """

    def __init__(self) -> None:
        self._subject_to_queues: dict[str, list[asyncio.Queue[BusEvent]]] = defaultdict(
            list
        )
        self._subs: dict[str, nats.aio.subscription.Subscription] = {}
        self._nc: nats.aio.client.Client | None = None

    def subscribe(self, *topics: str) -> asyncio.Queue[BusEvent]:
        """Return a new queue fed by *topics*.  Mirrors ``DataBus.subscribe``."""
        queue: asyncio.Queue[BusEvent] = asyncio.Queue()
        for topic in topics:
            subject = topic
            is_new = subject not in self._subs
            self._subject_to_queues[subject].append(queue)
            if self._nc is not None and is_new:
                asyncio.ensure_future(self._register(subject))
        return queue

    async def start(self, nc: nats.aio.client.Client) -> None:
        """Attach to *nc* and register NATS subscriptions for all known subjects."""
        self._nc = nc
        for subject in list(self._subject_to_queues):
            await self._register(subject)

    async def _register(self, subject: str) -> None:
        if subject in self._subs:
            return
        queues = self._subject_to_queues[subject]

        async def _cb(
            msg: nats.aio.msg.Msg,
            _qs: list[asyncio.Queue[BusEvent]] = queues,
        ) -> None:
            event = codec.decode(msg.data)
            for q in _qs:
                q.put_nowait(event)

        self._subs[subject] = await self._nc.subscribe(subject, cb=_cb)  # type: ignore[union-attr]

    def unsubscribe(self, queue: asyncio.Queue[BusEvent]) -> None:
        """Remove *queue* from all subjects.  Mirrors ``DataBus.unsubscribe``."""
        for queues in self._subject_to_queues.values():
            with contextlib.suppress(ValueError):
                queues.remove(queue)
