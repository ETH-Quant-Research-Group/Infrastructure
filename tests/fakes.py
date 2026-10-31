"""A stand-in exchange for tests, implementing the full ``BaseBroker`` contract."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from execution.types import (
    FundingSettlement,
    InstrumentRules,
    Order,
    OrderResult,
    OrderState,
    OrderStatus,
    Position,
)
from interfaces.broker import BaseBroker

if TYPE_CHECKING:
    from datetime import datetime


class FakeBroker(BaseBroker):
    """Fills every order instantly and completely at ``fill_price``, with no
    fees, and reports it truthfully through :meth:`order_status`.  Tests
    override the methods they care about."""

    def __init__(self, fill_price: Decimal = Decimal(100)) -> None:
        self.fill_price = fill_price
        self.placed: list[Order] = []
        self._statuses: dict[str, OrderStatus] = {}

    async def place_order(self, order: Order) -> OrderResult:
        self.placed.append(order)
        order_id = f"fake-{len(self.placed)}"
        status = OrderStatus(
            order_id=order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            state=OrderState.FILLED,
            quantity=order.quantity,
            filled_qty=order.quantity,
            leaves_qty=Decimal(0),
            avg_price=self.fill_price,
            filled_value=order.quantity * self.fill_price,
        )
        self._statuses[order_id] = status
        if order.client_order_id:
            self._statuses[order.client_order_id] = status
        return OrderResult(order_id=order_id, order=order, error=None, fill_price=self.fill_price)

    async def order_status(
        self,
        order_id: str | None = None,
        client_order_id: str | None = None,
        symbol: str | None = None,
    ) -> OrderStatus | None:
        return self._statuses.get(client_order_id or "") or self._statuses.get(order_id or "")

    async def order_history(self, start: datetime, end: datetime) -> list[OrderStatus]:
        return list({id(s): s for s in self._statuses.values()}.values())

    async def cancel_order(self, order_id: str, symbol: str | None = None) -> OrderResult:
        return OrderResult(order_id=order_id, order=None, error=None)

    async def cancel_all_orders(self, symbol: str | None = None) -> OrderResult:
        return OrderResult(order_id=None, order=None, error=None)

    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        return []

    async def position(self, symbol: str) -> Position | None:
        return None

    async def list_positions(self) -> list[Position]:
        return []

    async def instrument_rules(self) -> list[InstrumentRules]:
        return []

    async def funding_settlements(
        self, start: datetime, end: datetime
    ) -> list[FundingSettlement]:
        return []

    async def aclose(self) -> None:
        pass
