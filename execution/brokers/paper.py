from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from execution.types import (
    Order,
    OrderResult,
    OrderSide,
    OrderType,
    PerpPosition,
)
from interfaces.broker import BaseBroker


@dataclass
class PaperFill:
    """Single fill record in the paper broker trade ledger."""

    order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal  # always positive
    fill_price: Decimal
    fee: Decimal  # in quote currency
    timestamp: datetime


@dataclass
class _PaperPosition:
    qty: Decimal = field(default_factory=lambda: Decimal(0))
    avg_entry: Decimal = field(default_factory=lambda: Decimal(0))
    realized_pnl: Decimal = field(default_factory=lambda: Decimal(0))
    market_price: Decimal = field(
        default_factory=lambda: Decimal(0)
    )  # updated by live feed
    opened_at: datetime | None = None

    def unrealized_pnl(self) -> Decimal:
        price = self.market_price if self.market_price else self.avg_entry
        if self.qty > 0:
            return (price - self.avg_entry) * self.qty
        elif self.qty < 0:
            return (self.avg_entry - price) * abs(self.qty)
        return Decimal(0)


class PaperBroker(BaseBroker):
    """Simulated broker for paper trading and backtesting.

    Mirrors the interface of :class:`~execution.brokers.lighter.LighterBroker`
    so it can be swapped in without changing any calling code.

    Key features over the old ``_PaperBroker``:

    - **Live unrealized PnL** — call :meth:`update_market_price` whenever a
      new bar or mark-price event arrives. ``position()`` always returns PnL
      computed against the latest known price, not the last fill price.
    - **Trade ledger** — every fill is timestamped and stored in
      :attr:`trade_history`. Accessible for post-trade analysis and backtesting.
    - **Fee modeling** — configurable ``fee_rate`` (default 0.0 = 0%, matching
      Lighter's standard account). Fees are deducted from :attr:`realized_pnl`
      and tracked per-fill in the ledger.
    - **Unique order IDs** — each order gets a UUID so fills can be correlated.
    - **Returns PerpPosition** — same type as ``LighterBroker.position()``
      including ``funding_paid`` and ``mark_price`` fields.

    Usage in backtesting::

        broker = PaperBroker(initial_equity=Decimal("10000"))
        await broker.place_order(order)
        broker.update_market_price("ETHUSDT", Decimal("2500"))
        pos = await broker.position("ETHUSDT")
    """

    def __init__(
        self,
        initial_equity: Decimal = Decimal("10000"),
        fee_rate: Decimal = Decimal("0"),  # 0 = Lighter standard (0% fees)
    ) -> None:
        self._equity = initial_equity
        self._fee_rate = fee_rate
        self._positions: dict[str, _PaperPosition] = {}
        self._trade_history: list[PaperFill] = []
        self._order_counter: int = 0

    # ----------------------------------------------------------------- market data

    def update_market_price(self, symbol: str, price: Decimal) -> None:
        """Feed a live price update so unrealized PnL stays current.

        Call this from the consolidator worker on every incoming bar or
        mark-price event.  In a backtest, call it once per bar before
        querying positions.
        """
        pos = self._positions.get(symbol)
        if pos is not None:
            pos.market_price = price

    # ----------------------------------------------------------------- trading

    async def place_order(self, order: Order) -> OrderResult:
        self._order_counter += 1
        order_id = f"paper-{self._order_counter}-{uuid.uuid4().hex[:8]}"

        pos = self._positions.setdefault(order.symbol, _PaperPosition())

        # Fill price: use limit price if set, else current market price.
        # Refuse market orders with no known price — filling at 0 would
        # permanently corrupt avg_entry for the position.
        if order.order_type is OrderType.MARKET:
            if not pos.market_price:  # PNL fix!!!
                return OrderResult(
                    order_id=None,
                    order=order,
                    error="no market price known for market order — deliver a bar first",  # PNL fix!!!
                )
            fill_price = pos.market_price  # PNL fix!!!
        else:
            fill_price = order.price

        qty = order.quantity
        delta = qty if order.side is OrderSide.BUY else -qty
        fee = qty * fill_price * self._fee_rate
        old_qty = pos.qty
        new_qty = old_qty + delta

        if old_qty == Decimal(0):
            # Opening a fresh position
            pos.avg_entry = fill_price
            pos.opened_at = datetime.now(UTC)
        elif (old_qty > 0) == (delta > 0):
            # Adding to existing position — update weighted average entry
            total_cost = old_qty * pos.avg_entry + delta * fill_price
            pos.avg_entry = total_cost / new_qty
        else:
            # Reducing or flipping
            closed_qty = min(abs(delta), abs(old_qty))
            if old_qty > 0:
                pos.realized_pnl += (fill_price - pos.avg_entry) * closed_qty
            else:
                pos.realized_pnl += (pos.avg_entry - fill_price) * closed_qty
            pos.realized_pnl -= fee

            if new_qty == Decimal(0):
                pos.avg_entry = Decimal(0)
                pos.opened_at = None
            elif (old_qty > 0) != (new_qty > 0):
                # Position flipped direction
                pos.avg_entry = fill_price
                pos.opened_at = datetime.now(UTC)

        pos.qty = new_qty
        # Seed market_price so unrealized PnL is non-zero from the first fill
        if not pos.market_price:
            pos.market_price = fill_price

        self._trade_history.append(
            PaperFill(
                order_id=order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=qty,
                fill_price=fill_price,
                fee=fee,
                timestamp=datetime.now(UTC),
            )
        )

        return OrderResult(
            order_id=order_id,
            order=order,
            error=None,
            fill_price=fill_price,
        )

    async def cancel_order(self, order_id: str) -> OrderResult:
        return OrderResult(order_id=order_id, order=None, error=None)

    async def cancel_all_orders(self, symbol: str | None = None) -> OrderResult:
        return OrderResult(order_id=None, order=None, error=None)

    # --------------------------------------------------------------- read-only

    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        # Paper broker fills instantly — no open orders ever
        return []

    async def position(self, symbol: str) -> PerpPosition | None:
        pos = self._positions.get(symbol)
        if pos is None or pos.qty == Decimal(0):
            return None

        mark = pos.market_price if pos.market_price else pos.avg_entry

        return PerpPosition(
            symbol=symbol,
            quantity=pos.qty,
            avg_entry_price=pos.avg_entry,
            unrealized_pnl=pos.unrealized_pnl(),
            realized_pnl=pos.realized_pnl,
            mark_price=mark,
            funding_paid=None,
            liquidation_price=None,
        )

    # ------------------------------------------------------------ introspection

    @property
    def trade_history(self) -> list[PaperFill]:
        """Append-only list of all fills, oldest first."""
        return list(self._trade_history)

    @property
    def total_realized_pnl(self) -> Decimal:
        return sum((p.realized_pnl for p in self._positions.values()), Decimal(0))

    @property
    def total_unrealized_pnl(self) -> Decimal:
        return sum((p.unrealized_pnl() for p in self._positions.values()), Decimal(0))

    @property
    def total_fees_paid(self) -> Decimal:
        return sum((f.fee for f in self._trade_history), Decimal(0))

    # ----------------------------------------------------------------- lifecycle

    async def aclose(self) -> None:
        pass
