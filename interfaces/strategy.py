from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data.types import AnyBar, Trade


class BaseStrategy(ABC):
    """Foundation class for all trading strategies.

    Subclass this and implement :meth:`on_bar` to react to incoming market
    data.  Override :meth:`on_start` and :meth:`on_stop` for setup and
    teardown logic.

    Example::

        class MomentumStrategy(BaseStrategy):
            def on_bar(self, bar: AnyBar) -> None:
                # your signal logic here
                ...
    """

    def on_start(self) -> None:  # noqa: B027
        """Called once before the strategy begins receiving bars."""

    @abstractmethod
    def on_bar(self, bar: AnyBar) -> None:
        """Called for each new bar, whether live or historical."""

    def on_trade(self, trade: Trade) -> None:  # noqa: B027
        """Called for each individual trade tick (optional override)."""

    def on_stop(self) -> None:  # noqa: B027
        """Called once after the strategy stops receiving bars."""
