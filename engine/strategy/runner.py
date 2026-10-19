from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

from data.types import DollarBar, FundingRate, TickBar, TimeBar, Trade, VolumeBar
from interfaces.signals import TargetPosition

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Iterable
    from datetime import datetime

    from data.types import AnyBar
    from engine.strategy.guard import StrategyGuard
    from engine.strategy.pnl_calc import PnLCalc
    from engine.types import BusEvent, BusProtocol
    from execution.types import FillConfirmation
    from interfaces.strategy import BaseStrategy

log = logging.getLogger(__name__)


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
        # cumulative position per (symbol, exchange) — keyed by both so that
        # a delta-neutral strategy's perp short and spot long on the same symbol
        # are tracked independently and the guard can flatten each leg correctly.
        self._cum_position: dict[tuple[str, str], Decimal] = {}
        # (symbol, bar type, interval) → open time of the newest start-up
        # history bar: live bars up to it were already seen as history.
        self._history_until: dict[tuple[str, str, int | None], datetime] = {}

    @property
    def pnl_calc(self) -> PnLCalc:
        return self._pnlcalc

    async def run(self, history: Iterable[AnyBar] = ()) -> None:
        """Subscribe, start the strategy, feed it *history*, then process
        live events until cancelled.

        *history* (past bars, oldest first) goes to the strategy's
        ``on_history_bar`` — after ``on_start``, before the first live
        event, and never to ``on_bar``, so nothing is traded on it.  Live
        bars arriving meanwhile wait in the queue; one that the history
        already covered is skipped, so no bar is seen twice.
        """
        queue = self._bus.subscribe(*self._topics)
        self.begin(history)
        try:
            while True:
                await self.process(await queue.get())
        finally:
            self._strategy.on_stop()
            self._bus.unsubscribe(queue)

    def begin(self, history: Iterable[AnyBar] = ()) -> None:
        """``on_start``, then *history* to ``on_history_bar`` — the start of
        :meth:`run`, also used by the backtest, which drives the runner one
        event at a time."""
        self._strategy.on_start()
        for bar in history:
            self._warm_up(bar)

    async def process(self, event: BusEvent) -> None:
        """Hand one live event to the strategy and emit what it returns."""
        await self._emit(self._dispatch(event))

    @staticmethod
    def _bar_key(bar: AnyBar) -> tuple[str, str, int | None]:
        return (bar.symbol, type(bar).__name__, getattr(bar, "interval_seconds", None))

    def _warm_up(self, bar: AnyBar) -> None:
        self._last_price[bar.symbol] = bar.close
        self._pnlcalc.update_market_price(bar.symbol, bar.close)
        key = self._bar_key(bar)
        if key not in self._history_until or bar.timestamp > self._history_until[key]:
            self._history_until[key] = bar.timestamp
        result = self._strategy.on_history_bar(bar)  # type: ignore[func-returns-value]
        if result is not None:
            log.warning(
                "%s: on_history_bar returned %r — ignored, history is never traded",
                self._strategy_id, result,
            )

    def _dispatch(self, event: BusEvent) -> TargetPosition | None:
        match event:
            case TimeBar() | TickBar() | VolumeBar() | DollarBar():
                covered = self._history_until.get(self._bar_key(event))
                if covered is not None and event.timestamp <= covered:
                    return None  # already seen in the start-up history
                self._last_price[event.symbol] = event.close
                self._pnlcalc.update_market_price(event.symbol, event.close)
                return self._strategy.on_bar(event)
            case Trade():
                return self._strategy.on_trade(event)
            case FundingRate():
                self._pnlcalc.update_market_price(event.symbol, event.mark_price)
                return self._strategy.on_funding_rate(event)
            case _:
                return None

    async def notify_fill(self, fill: FillConfirmation) -> None:
        """Forward a fill confirmation to the strategy and emit any follow-up signal."""
        result = self._strategy.on_fill(fill)
        await self._emit(result)

    async def _emit(self, target: TargetPosition | None) -> None:
        if not self._guard.is_active:
            # Guard tripped (cumulative loss exceeded max_loss).  HALT new
            # emissions and require operator intervention.
            #
            # The previous implementation auto-flattened by emitting
            # ``-cum_qty`` for every entry in ``_cum_position``.  That logic
            # is unsafe because ``_cum_position`` tracks signals emitted by
            # *this* runner instance since startup — NOT actual broker
            # positions.  After ``restore_from_positions`` reattaches the
            # strategy to existing broker positions, only the eventual EXIT
            # signals get tracked here, and flattening with ``-cum_qty`` then
            # OPENS positions in the wrong direction (observed 2026-05-03
            # 13:40 UTC: a guard trip caused phantom perp-shorts and spot
            # buys totalling ~$30K notional).
            #
            # Until the runner has access to broker truth, the only safe
            # action on a guard trip is to stop and let a human decide.
            if self._cum_position:
                # Log once, then keep halting silently — the guard does not
                # auto-reset, so this branch fires on every event after trip.
                pass
            return

        if target is None or target.quantity == Decimal(0):
            return

        stamped = replace(
            target,
            strategy_id=self._strategy_id,
            price=self._last_price.get(target.symbol, target.price),
        )
        key = (stamped.symbol, stamped.exchange or "")
        self._cum_position[key] = (
            self._cum_position.get(key, Decimal(0)) + stamped.quantity
        )
        await self._target_queue.put(stamped)
