from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"
    STOP_LOSS = "stop-loss"
    STOP_LOSS_LIMIT = "stop-loss-limit"
    TAKE_PROFIT = "take-profit"
    TAKE_PROFIT_LIMIT = "take-profit-limit"


class TimeInForce(StrEnum):
    GTT = "good-till-time"
    IOC = "immediate-or-cancel"
    POST_ONLY = "post-only"


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Order:
    """Exchange-agnostic order request.

    ``symbol`` uses a unified naming convention; each broker resolves it
    to its native format internally (e.g. ``"ETH-USDC"`` → ``market_index=1``
    on Lighter, a ``Contract`` object on IBKR, ``"EUR_USD"`` on OANDA).

    ``quantity`` is in base-asset units: shares for equities, base currency
    for FX, base asset for crypto.

    ``price`` is the limit price.  Set to ``Decimal(0)`` for market orders.

    ``client_order_id`` is an optional caller-assigned label that most brokers
    echo back; its format is broker-specific (some require integers).
    """

    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    price: Decimal
    time_in_force: TimeInForce = TimeInForce.GTT
    client_order_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class PerpOrder(Order):
    """Perpetual futures order — extends :class:`Order` with perp-specific fields.

    ``order_expiry`` follows Lighter's convention: ``-1`` = 28-day default,
    ``0`` = IOC immediate.
    """

    reduce_only: bool = False
    trigger_price: Decimal | None = None
    order_expiry: int = -1


@dataclass(frozen=True, kw_only=True)
class EquityOrder(Order):
    """Equity / stock order — adds exchange routing and currency."""

    exchange: str = "SMART"  # IBKR smart routing default
    currency: str = "USD"


@dataclass(frozen=True, kw_only=True)
class FXOrder(Order):
    """FX spot / forward order.

    When ``quantity_is_base`` is ``True`` (default), ``quantity`` is in the
    base currency of the pair.  Set to ``False`` to express ``quantity`` as a
    quote-currency notional instead.
    """

    quantity_is_base: bool = True


# ---------------------------------------------------------------------------
# Order result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class OrderResult:
    """Returned by a broker after placing or canceling an order.

    ``order_id`` is the exchange-assigned identifier.  For Lighter it is the
    ZK rollup tx hash; for conventional brokers it is the exchange order ID.
    Brokers that require separate market + order indices to cancel encode both
    in ``order_id`` (e.g. ``"1:42"`` for market 1, order 42 on Lighter).

    ``fill_price`` is the execution price of the order.  Brokers that cannot
    return an exact fill price (e.g. Lighter, which only returns a tx hash)
    populate this with the best bid/ask at submission time as a proxy.
    ``None`` for cancel results.
    """

    order_id: str | None
    order: Order | None  # echo of the submitted order; None for cancels
    error: str | None
    fill_price: Decimal | None = None

    @property
    def ok(self) -> bool:
        """``True`` when the transaction was accepted without error."""
        return self.error is None


# ---------------------------------------------------------------------------
# Fill confirmation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class FillConfirmation:
    """Published by the consolidator back to each strategy worker after a fill.

    ``fill_price`` is the actual broker execution price when an order was sent,
    or the signal's reference price (bar close) when trades internally netted
    and no broker order was placed.
    """

    strategy_id: str
    symbol: str
    quantity: Decimal  # signed: positive = bought, negative = sold
    fill_price: Decimal


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Position:
    """Open position in any instrument.

    ``quantity`` is signed: positive = long, negative = short.
    """

    symbol: str
    quantity: Decimal
    avg_entry_price: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal


@dataclass(frozen=True, kw_only=True)
class PerpPosition(Position):
    """Perpetual futures position — extends :class:`Position` with perp fields."""

    liquidation_price: Decimal | None = None
    funding_paid: Decimal | None = None
    mark_price: Decimal | None = None
