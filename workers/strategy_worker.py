"""Strategy worker — subscribes to market data and forwards signals to NATS.

Run with::

    STRATEGY_NAME=ExampleStrategy python -m workers.strategy_worker

Environment variables:

- ``STRATEGY_NAME`` (required): class name of a ``BaseStrategy`` subclass
  found anywhere in the ``strategies`` package.
- ``NATS_URL`` (default: ``nats://localhost:4222``)
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import pkgutil
from typing import TYPE_CHECKING

import nats
import nats.aio.client

import strategies as _strategies_pkg
from config import NATS_URL
from engine.data import codec
from engine.data.nats_bus import NatsBus
from engine.strategy.guard import StrategyGuard
from engine.strategy.runner import StrategyRunner
from interfaces.strategy import BaseStrategy

if TYPE_CHECKING:
    from interfaces.signals import TargetPosition

log = logging.getLogger(__name__)


def _load_strategy(name: str) -> type[BaseStrategy]:
    """Scan every module in the ``strategies`` package and return the named class."""
    for module_info in pkgutil.iter_modules(_strategies_pkg.__path__):
        mod = importlib.import_module(f"strategies.{module_info.name}")
        if hasattr(mod, name):
            cls = getattr(mod, name)
            if isinstance(cls, type) and issubclass(cls, BaseStrategy):
                return cls

    raise RuntimeError(f"Strategy '{name}' not found in the strategies package")


async def _forward_targets(
    nc: nats.aio.client.Client,
    queue: asyncio.Queue[TargetPosition],
) -> None:
    while True:
        t = await queue.get()
        await nc.publish(f"signals.targets.{t.strategy_id}", codec.encode_target(t))


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    strategy_name = os.environ.get("STRATEGY_NAME", "")
    if not strategy_name:
        raise RuntimeError("STRATEGY_NAME environment variable is required")

    strategy_cls = _load_strategy(strategy_name)
    log.info("loaded strategy %s", strategy_cls.__name__)

    bus = NatsBus()
    target_queue: asyncio.Queue[TargetPosition] = asyncio.Queue()
    guard = StrategyGuard(max_loss=strategy_cls.max_loss)

    runner = StrategyRunner(
        strategy_id=strategy_cls.__name__,
        strategy=strategy_cls(),
        bus=bus,
        topics=strategy_cls.topics,
        target_queue=target_queue,
        guard=guard,
    )

    nc = await nats.connect(NATS_URL)
    try:
        await bus.start(nc)

        # Request streams from feed_server for every topic this strategy needs.
        for topic in strategy_cls.topics:
            await nc.publish(f"control.subscribe.{topic}", b"")
        log.info(
            "requested %d stream(s): %s", len(strategy_cls.topics), strategy_cls.topics
        )

        async with asyncio.TaskGroup() as tg:
            tg.create_task(runner.run())
            tg.create_task(_forward_targets(nc, target_queue))
    finally:
        for topic in strategy_cls.topics:
            await nc.publish(f"control.unsubscribe.{topic}", b"")
        await nc.drain()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped")
