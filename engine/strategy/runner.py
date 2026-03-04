from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

from data.types import DollarBar, FundingRate, TickBar, TimeBar, Trade, VolumeBar

if TYPE_CHECKING:
    import asyncio

    from engine.strategy.guard import StrategyGuard
    from engine.types import BusEvent, BusProtocol
    from interfaces.signals import TargetPosition
    from interfaces.strategy import BaseStrategy


class StrategyRunner:
    """One async task per strategy.

    Subscribes to the requested bus topics, dispatches events to the strategy,
    applies the guard, and emits :class:`~interfaces.signals.TargetPosition`
    onto the shared queue consumed by
    :class:`~engine.order.consolidator.OrderConsolidator`.

    When the guard trips, all previously emitted targets are flattened to zero
    before the runner stops producing new signals.
    """

    def __init__(
        self,
        strategy_id: str,
        strategy: BaseStrategy,
        bus: BusProtocol,
        topics: list[str],
        target_queue: asyncio.Queue[TargetPosition],
        guard: StrategyGuard,
    ) -> None:
        self._strategy_id = strategy_id
        self._strategy = strategy
        self._bus: BusProtocol = bus
        self._topics = topics
        self._target_queue = target_queue
        self._guard = guard
        self._last_targets: dict[str, TargetPosition] = {}

    async def run(self) -> None:
        """Subscribe, start the strategy, and process events until cancelled."""
        queue = self._bus.subscribe(*self._topics)
        self._strategy.on_start()
        try:
            while True:
                event = await queue.get()
                result = self._dispatch(event)
                await self._emit(result)
        finally:
            self._strategy.on_stop()
            self._bus.unsubscribe(queue)

    def _dispatch(self, event: BusEvent) -> TargetPosition | None:
        match event:
            case TimeBar() | TickBar() | VolumeBar() | DollarBar():
                return self._strategy.on_bar(event)
            case Trade():
                return self._strategy.on_trade(event)
            case FundingRate():
                return self._strategy.on_funding_rate(event)
            case _:
                return None

    async def _emit(self, target: TargetPosition | None) -> None:
        if not self._guard.is_active:
            for last in list(self._last_targets.values()):
                flat = replace(last, quantity=Decimal(0))
                await self._target_queue.put(flat)
            self._last_targets.clear()
            return
        if target is not None:
            stamped = replace(target, strategy_id=self._strategy_id)
            self._last_targets[stamped.symbol] = stamped
            await self._target_queue.put(stamped)
