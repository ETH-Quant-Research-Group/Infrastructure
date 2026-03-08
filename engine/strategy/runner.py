from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

from data.types import DollarBar, FundingRate, TickBar, TimeBar, Trade, VolumeBar
from interfaces.signals import TargetPosition

if TYPE_CHECKING:
    import asyncio

    from engine.strategy.guard import StrategyGuard
    from engine.strategy.pnl_calc import PnLCalc
    from engine.types import BusEvent, BusProtocol
    from execution.types import FillConfirmation
    from interfaces.strategy import BaseStrategy


class StrategyRunner:
    """One async task per strategy.

    Subscribes to the requested bus topics, dispatches events to the strategy,
    applies the guard, and emits :class:`~interfaces.signals.TargetPosition`
    onto the shared queue consumed by
    :class:`~engine.order.consolidator.OrderConsolidator`.

    Each emitted signal carries a signed quantity **delta** (buy/sell N units)
    and the last known bar close price for the symbol, stamped by the runner.

    When the guard trips, flatten signals are emitted for every open position
    (negative of cumulative quantity) before the runner stops.
    """

    def __init__(
        self,
        strategy_id: str,
        strategy: BaseStrategy,
        bus: BusProtocol,
        topics: list[str],
        target_queue: asyncio.Queue[TargetPosition],
        guard: StrategyGuard,
        pnlcalc: PnLCalc,
    ) -> None:
        self._strategy_id = strategy_id
        self._strategy = strategy
        self._bus: BusProtocol = bus
        self._topics = topics
        self._target_queue = target_queue
        self._guard = guard
        self._pnlcalc = pnlcalc
        # last bar close price per symbol — stamped onto outgoing signals
        self._last_price: dict[str, Decimal] = {}
        # cumulative position per symbol — used to emit flatten signals on guard trip
        self._cum_position: dict[str, Decimal] = {}

    @property
    def pnl_calc(self) -> PnLCalc:
        return self._pnlcalc

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
                self._last_price[event.symbol] = event.close
                self._pnlcalc.update_market_price(event.symbol, event.close)
                return self._strategy.on_bar(event)
            case Trade():
                return self._strategy.on_trade(event)
            case FundingRate():
                return self._strategy.on_funding_rate(event)
            case _:
                return None

    async def notify_fill(self, fill: FillConfirmation) -> None:
        """Forward a fill confirmation to the strategy and emit any follow-up signal."""
        result = self._strategy.on_fill(fill)
        await self._emit(result)

    async def _emit(self, target: TargetPosition | None) -> None:
        if not self._guard.is_active:
            # Flatten all open positions and stop emitting
            for symbol, cum_qty in list(self._cum_position.items()):
                if cum_qty != Decimal(0):
                    await self._target_queue.put(
                        TargetPosition(
                            symbol=symbol,
                            quantity=-cum_qty,
                            price=self._last_price.get(symbol, Decimal(0)),
                            strategy_id=self._strategy_id,
                        )
                    )
            self._cum_position.clear()
            return

        if target is None or target.quantity == Decimal(0):
            return

        stamped = replace(
            target,
            strategy_id=self._strategy_id,
            price=self._last_price.get(target.symbol, target.price),
        )
        self._cum_position[stamped.symbol] = (
            self._cum_position.get(stamped.symbol, Decimal(0)) + stamped.quantity
        )
        await self._target_queue.put(stamped)
