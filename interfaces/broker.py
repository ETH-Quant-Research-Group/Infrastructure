from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from execution.types import Order, OrderResult, Position


class BaseBroker(ABC):
    """Contract that every broker implementation must fulfil.

    Concrete implementations (e.g. :class:`~execution.brokers.lighter.LighterBroker`)
    hide all exchange-specific wiring so callers only deal with canonical
    types from :mod:`execution.types`.

    ``symbol`` follows a unified naming convention throughout; each broker
    resolves it to its native format internally.
    """

    # ----------------------------------------------------------------- trading

    @abstractmethod
    async def place_order(self, order: Order) -> OrderResult:
        """Sign and submit *order*.  Always inspect ``result.error``."""
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel the open order identified by *order_id*.

        *order_id* must be the value returned in
        :attr:`~execution.types.OrderResult.order_id` from a prior
        :meth:`place_order` or :meth:`open_orders` call.
        """
        ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: str | None = None) -> OrderResult:
        """Cancel every open order, optionally filtered to *symbol*.

        Brokers that do not support per-symbol cancellation will cancel all
        markets and document that behaviour in their docstring.
        """
        ...

    # --------------------------------------------------------------- read-only

    @abstractmethod
    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        """Return all open orders, optionally filtered to *symbol*."""
        ...

    @abstractmethod
    async def position(self, symbol: str) -> Position | None:
        """Return the current position for *symbol*, or ``None`` if flat."""
        ...

    # ----------------------------------------------------------------- lifecycle

    @abstractmethod
    async def aclose(self) -> None:
        """Release any held connections or resources."""
        ...

    async def __aenter__(self) -> BaseBroker:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
