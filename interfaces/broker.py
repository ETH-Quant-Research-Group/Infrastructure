from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from execution.types import (
        FundingSettlement,
        InstrumentRules,
        Order,
        OrderResult,
        OrderStatus,
        Position,
    )


class BaseBroker(ABC):
    """Contract that every broker implementation must fulfil.

    Concrete implementations (e.g. :class:`~execution.brokers.bybit.BybitBroker`)
    hide all exchange-specific wiring so callers only deal with canonical
    types from :mod:`execution.types`.

    ``symbol`` follows a unified naming convention throughout; each broker
    resolves it to its native format internally.

    Every method here is required: the consolidator and the other workers
    rely on all of them — real fills (:meth:`order_status`), cancelling at
    the timeout, order sizing (:meth:`instrument_rules`), ledger recovery
    (:meth:`order_history`), the funding split (:meth:`funding_settlements`)
    and position snapshots (:meth:`list_positions`).  A broker missing one
    can't be instantiated.  (``execution/brokers/paper.py`` and
    ``lighter.py`` are obsolete for that reason.)

    Optional extras some brokers have, looked up with ``hasattr``:
    ``wallet_balance()`` (dashboard AUM), ``recent_fills()`` (dashboard
    replay), ``update_market_price()`` (simulated brokers).
    """

    # ----------------------------------------------------------------- trading

    @abstractmethod
    async def place_order(self, order: Order) -> OrderResult:
        """Sign and submit *order*.  Always inspect ``result.error``.

        A request that failed in transit (timeout, dropped connection) must
        return ``maybe_placed=True``: the exchange may still have it, and the
        consolidator then looks it up by ``order.client_order_id``.
        """
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str | None = None) -> OrderResult:
        """Cancel the open order identified by *order_id*.

        *order_id* must be the value returned in
        :attr:`~execution.types.OrderResult.order_id` from a prior
        :meth:`place_order` or :meth:`open_orders` call.  *symbol* is passed
        when known — some exchanges (Bybit) require it.
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
    async def order_status(
        self,
        order_id: str | None = None,
        client_order_id: str | None = None,
        symbol: str | None = None,
    ) -> OrderStatus | None:
        """The current state of one order, by the exchange's *order_id* or
        our *client_order_id* tag: what really filled, at what average price,
        with which fees.

        Returns ``None`` **only** when the exchange confirms there is no such
        order.  Network / API errors must raise, so a caller never mistakes
        "couldn't ask" for "never placed".
        """
        ...

    @abstractmethod
    async def order_history(self, start: datetime, end: datetime) -> list[OrderStatus]:
        """Every order created between *start* and *end*, any status.  Must
        raise rather than return a partial history."""
        ...

    @abstractmethod
    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        """Return all open orders, optionally filtered to *symbol*."""
        ...

    @abstractmethod
    async def position(self, symbol: str) -> Position | None:
        """Return the current position for *symbol*, or ``None`` if flat."""
        ...

    @abstractmethod
    async def list_positions(self) -> list[Position]:
        """Every open position on the account (one per symbol, signed)."""
        ...

    @abstractmethod
    async def instrument_rules(self) -> list[InstrumentRules]:
        """The order-size rules (lot step, min / max quantity, min order
        value) of every tradable symbol.  Called rarely — the consolidator
        caches the result — and must raise on errors."""
        ...

    @abstractmethod
    async def funding_settlements(
        self, start: datetime, end: datetime
    ) -> list[FundingSettlement]:
        """Every funding payment on the account between *start* and *end*.
        A broker without perpetuals returns ``[]``.  Must raise rather than
        return a partial list."""
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
