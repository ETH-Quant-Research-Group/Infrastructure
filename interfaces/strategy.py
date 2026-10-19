from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from data.types import AnyBar, FundingRate, Trade
    from engine.clock import Clock
    from engine.order.instruments import OrderSizing
    from execution.types import FillConfirmation
    from interfaces.signals import TargetPosition


class BaseStrategy(ABC):
    """Foundation class for all trading strategies.

    Subclass this and implement :meth:`on_bar` to react to incoming market
    data.  Override :meth:`on_start` and :meth:`on_stop` for setup and
    teardown logic.

    Class-level attributes to set on each subclass:

    ``topics``
        List of bus topic strings this strategy wants to receive.
        NATS subject strings, e.g. ``"futures.BTCUSDT.bars.1m"``.

    ``max_loss``
        Maximum cumulative loss (positive number) before the
        :class:`~engine.guard.StrategyGuard` halts the strategy.

    Example::

        class MomentumStrategy(BaseStrategy):
            topics = [futures_bars_topic("BTCUSDT", KlineInterval.M1)]
            max_loss = Decimal("500")

            def on_bar(self, bar: AnyBar) -> TargetPosition | None:
                # your signal logic here
                ...
    """

    topics: ClassVar[list[str]] = []
    max_loss: ClassVar[Decimal] = Decimal("1000")

    #: The exchanges' order-size rules for this strategy's symbols (lot
    #: step, min / max quantity, min order value), set by the strategy worker
    #: before :meth:`on_start` and kept up to date.  All in memory — reading
    #: it never makes a network call.  ``None`` outside the worker (e.g. in
    #: a backtest).  Example::
    #:
    #:     qty = self.order_sizing.round_qty("bybit_spot", "SOLUSDT", raw_qty)
    #:     why_not = self.order_sizing.check("bybit_demo", "SOLUSDT", qty, price)
    order_sizing: OrderSizing | None = None

    #: Where :meth:`now` gets the time from — set by the backtest to its
    #: simulated clock.  ``None`` = the real time.
    clock: Clock | None = None

    def now(self) -> datetime:
        """The current time (UTC) — the simulated time in a backtest.

        Use this instead of ``datetime.now()``, so the strategy behaves the
        same in a backtest as live.
        """
        return self.clock.now() if self.clock is not None else datetime.now(UTC)

    def on_start(self) -> None:  # noqa: B027
        """Called once before the strategy begins receiving bars."""

    def on_history_bar(self, bar: AnyBar) -> None:  # noqa: B027
        """Called once per past bar at start-up, oldest first — after
        :meth:`on_start`, before the first live :meth:`on_bar`.

        For warming up indicators (rolling volatility, moving averages, …).
        It cannot trade: there is no return value, and anything returned is
        ignored.  The history covers the strategy's bar topics (the last 60
        finished bars of each); a strategy that doesn't override this simply
        starts without it.
        """

    @abstractmethod
    def on_bar(self, bar: AnyBar) -> TargetPosition | None:
        """Called for each new **live** bar.  Past bars from start-up go to
        :meth:`on_history_bar` instead, never here."""

    def on_trade(self, trade: Trade) -> TargetPosition | None:  # noqa: B027
        """Called for each individual trade tick (optional override)."""
        return None

    def on_funding_rate(self, rate: FundingRate) -> TargetPosition | None:  # noqa: B027
        """Called for each funding-rate update (optional override)."""
        return None

    def on_fill(self, fill: FillConfirmation) -> TargetPosition | None:  # noqa: B027
        """Called when the consolidator confirms a fill for this strategy (optional).

        Return a :class:`~interfaces.signals.TargetPosition` to immediately emit
        a follow-up signal — useful for placing a spot hedge after a perp fill.
        Return ``None`` for no further action.
        """
        return None

    def on_stop(self) -> None:  # noqa: B027
        """Called once after the strategy stops receiving bars."""
