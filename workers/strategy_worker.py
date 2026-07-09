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
import json
import logging
import os
from datetime import UTC, datetime
import pkgutil
from typing import TYPE_CHECKING

import nats
import nats.aio.client

import strategies as _strategies_pkg
from config import NATS_URL
from engine.data import codec
from engine.data.nats_bus import NatsBus
from engine.strategy.guard import StrategyGuard
from engine.strategy.pnl_calc import PnLCalc
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


async def _listen_fills(
    nc: nats.aio.client.Client,
    strategy_id: str,
    runner: StrategyRunner,
) -> None:
    """Subscribe to fill confirmations and feed them into the runner's PnLCalc."""
    import nats.aio.msg as _nats_msg

    async def _cb(msg: _nats_msg.Msg) -> None:
        fill = codec.decode_fill(msg.data)
        # Track every leg by (symbol, exchange) so spot and perp unrealized
        # offset correctly for delta-neutral strategies.
        runner.pnl_calc.on_fill(fill.symbol, fill.quantity, fill.fill_price, exchange=fill.exchange)
        await runner.notify_fill(fill)

    await nc.subscribe(f"fills.{strategy_id}", cb=_cb)
    await asyncio.get_running_loop().create_future()


async def _publish_heartbeat_periodically(
    nc: nats.aio.client.Client,
    strategy_id: str,
    strategy_instance=None,
    interval: float = 8.0,
) -> None:
    """Publish a heartbeat so the dashboard can show the strategy as active.

    If the strategy exposes ``heartbeat_state()`` (returning a dict snapshot
    of fill_state, perp/spot qty, holding period, etc. per symbol), the
    snapshot is included in the payload so the dashboard can render rich
    per-symbol state without subscribing to fills/positions separately.
    """
    while True:
        await asyncio.sleep(interval)
        body: dict = {
            "strategy_id": strategy_id,
            "status": "active",
            "ts": datetime.now(UTC).isoformat(),
        }
        if strategy_instance is not None and hasattr(strategy_instance, "heartbeat_state"):
            try:
                body["state"] = strategy_instance.heartbeat_state()
            except Exception as exc:
                log.debug("heartbeat_state() raised: %s", exc)
        await nc.publish(
            f"strategy.heartbeat.{strategy_id}",
            json.dumps(body, default=str).encode(),
        )


async def _publish_registration_periodically(
    nc: nats.aio.client.Client,
    strategy_cls: type[BaseStrategy],
    interval: float = 10.0,
) -> None:
    """Re-broadcast strategy registration so the dashboard recovers after a restart."""
    payload = json.dumps(
        {
            "name": strategy_cls.__name__,
            "display_name": getattr(strategy_cls, "display_name", strategy_cls.__name__),
            "topics": list(strategy_cls.topics),
            "max_loss": str(strategy_cls.max_loss),
        }
    ).encode()
    while True:
        await asyncio.sleep(interval)
        await nc.publish(f"strategy.register.{strategy_cls.__name__}", payload)


async def _bridge_broker_pnl_to_strategy(
    nc: nats.aio.client.Client,
    strategy_id: str,
    guard: StrategyGuard,
) -> None:
    """Re-publish broker aggregate PnL as strategy PnL.

    For a delta-neutral funding arb strategy the broker's equity change IS the
    strategy's P&L (funding income - fees).  The fill-based PnLCalc always nets
    to near zero while the position is open, so the strategy chart would be a
    flat line.  Bridging broker.pnl here makes the chart show real performance.
    """
    from decimal import Decimal

    # last_total is None until the first broker.pnl message arrives.  Until
    # then we cannot compute a meaningful delta, so the guard is not fed.
    # This prevents the strategy worker startup from synthesising a giant
    # delta against a 0 baseline (especially harmful when the persisted
    # baseline has been shifted, since the consolidator's first publish can
    # be hundreds of dollars away from anything this worker has seen).
    last_total: Decimal | None = None

    # Sanity clamp: a real PnL change in 5 s on this fund cannot exceed
    # max_loss * 2.  Any larger swing is treated as a publisher-restart
    # artifact (e.g. consolidator reloaded with different baseline math),
    # snaps the bridge baseline, and is NOT fed to the guard.  Without this
    # clamp, the cascade observed 2026-05-03 13:40 UTC recurs: a synthetic
    # ~$2.7K delta tripped the guard, and the runner's auto-flatten then
    # opened phantom positions.
    delta_clamp = guard.max_loss * Decimal(2)

    async def _cb(msg: nats.aio.msg.Msg) -> None:
        nonlocal last_total
        try:
            data = json.loads(msg.data)
            current_total = Decimal(str(data.get("total", "0") or "0"))
            if last_total is None:
                # First message after process start — establish baseline only.
                last_total = current_total
            else:
                delta = current_total - last_total
                if abs(delta) > delta_clamp:
                    # Treat as publisher restart / baseline shift artifact.
                    log.warning(
                        "broker.pnl delta %.2f exceeds clamp %.2f — snapping baseline",
                        float(delta), float(delta_clamp),
                    )
                    last_total = current_total
                else:
                    guard.record_pnl(delta)
                    last_total = current_total
            await nc.publish(
                f"pnl.{strategy_id}",
                json.dumps({
                    "strategy_id": strategy_id,
                    "total_realized": data.get("total_realized", "0"),
                    "total_unrealized": data.get("total_unrealized", "0"),
                    "total": data.get("total", "0"),
                    "timestamp": data.get("timestamp", ""),
                }).encode(),
            )
        except Exception:
            pass

    await nc.subscribe("broker.pnl", cb=_cb)
    await asyncio.get_running_loop().create_future()


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

    strategy_id = strategy_cls.__name__
    bus = NatsBus()
    target_queue: asyncio.Queue[TargetPosition] = asyncio.Queue()
    guard = StrategyGuard(max_loss=strategy_cls.max_loss)
    pnl_calc = PnLCalc()

    # Seed PnLCalc with current open positions from Bybit so that unrealized PnL
    # is correct immediately after a restart without waiting for new fills.
    # Uses the same brokers as the consolidator by reading env vars directly.
    try:
        from decimal import Decimal as _D
        from execution.brokers.bybit import BybitBroker as _BybitBroker
        from execution.brokers.bybit_spot import BybitSpotBroker as _BybitSpot
        _perp = _BybitBroker(demo=True)
        _spot = _BybitSpot(demo=True)
        _perp_positions = await _perp.list_positions()
        _spot_positions = await _spot.list_positions()
        # Build perp entry price map — used as reference for spot seeding so
        # both legs start at the same price and unrealized nets to ~$0.
        perp_entry_map: dict[str, _D] = {}
        for p in _perp_positions:
            if p.quantity != _D(0):
                pnl_calc.on_fill(p.symbol, p.quantity, p.avg_entry_price, exchange="bybit_demo")
                if p.avg_entry_price > _D(0):
                    perp_entry_map[p.symbol] = p.avg_entry_price
        for p in _spot_positions:
            if p.quantity > _D(0):
                # Use perp entry as reference so spot unrealized offsets perp unrealized.
                # Falls back to current price (usdValue/qty) if no matching perp.
                entry = perp_entry_map.get(p.symbol, p.avg_entry_price)
                pnl_calc.on_fill(p.symbol, p.quantity, entry, exchange="bybit_spot")
        await _perp.aclose()
        await _spot.aclose()
        log.info(
            "Seeded PnLCalc: %d perp + %d spot positions",
            len(_perp_positions), len(_spot_positions),
        )
    except Exception as _exc:
        log.warning("Could not seed PnLCalc from open positions: %s", _exc)

    strategy_instance = strategy_cls()

    # Restore strategy state from live broker positions on restart.
    if hasattr(strategy_instance, "restore_from_positions"):
        try:
            from execution.brokers.bybit import BybitBroker as _BB
            from execution.brokers.bybit_spot import BybitSpotBroker as _BS
            _p = _BB(demo=True)
            _s = _BS(demo=True)
            _perp_pos = await _p.list_positions()
            _spot_pos = await _s.list_positions()
            await _p.aclose()
            await _s.aclose()
            strategy_instance.restore_from_positions(_perp_pos, _spot_pos)
        except Exception as _exc:
            log.warning("Could not restore strategy state from positions: %s", _exc)

    runner = StrategyRunner(
        strategy_id=strategy_id,
        strategy=strategy_instance,
        bus=bus,
        topics=strategy_cls.topics,
        target_queue=target_queue,
        guard=guard,
        pnlcalc=pnl_calc,
    )

    nc = await nats.connect(NATS_URL)
    try:
        await bus.start(nc)

        await nc.publish(
            f"strategy.register.{strategy_cls.__name__}",
            json.dumps(
                {
                    "name": strategy_cls.__name__,
                    "display_name": getattr(strategy_cls, "display_name", strategy_cls.__name__),
                    "topics": list(strategy_cls.topics),
                    "max_loss": str(strategy_cls.max_loss),
                }
            ).encode(),
        )

        # Request streams from feed_server for every topic this strategy needs.
        for topic in strategy_cls.topics:
            await nc.publish(f"control.subscribe.{topic}", b"")
        log.info(
            "requested %d stream(s): %s", len(strategy_cls.topics), strategy_cls.topics
        )

        async with asyncio.TaskGroup() as tg:
            tg.create_task(runner.run())
            tg.create_task(_forward_targets(nc, target_queue))
            tg.create_task(_listen_fills(nc, strategy_id, runner))
            tg.create_task(_bridge_broker_pnl_to_strategy(nc, strategy_id, guard))
            tg.create_task(_publish_heartbeat_periodically(nc, strategy_id, strategy_instance))
            tg.create_task(_publish_registration_periodically(nc, strategy_cls))
    finally:
        await nc.publish(f"strategy.unregister.{strategy_cls.__name__}", b"")
        for topic in strategy_cls.topics:
            await nc.publish(f"control.unsubscribe.{topic}", b"")
        await nc.drain()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped")
