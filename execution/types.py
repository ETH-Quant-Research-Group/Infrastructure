from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
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
    # True when the request failed in transit (timeout, dropped connection):
    # the exchange may still have received and executed the order.
    maybe_placed: bool = False

    @property
    def ok(self) -> bool:
        """``True`` when the transaction was accepted without error."""
        return self.error is None


# ---------------------------------------------------------------------------
# Order status
# ---------------------------------------------------------------------------


class OrderState(StrEnum):
    """Exchange-agnostic lifecycle state of a placed order.

    ``CANCELLED`` includes orders that filled partly before the rest was
    cancelled (e.g. the unfilled remainder of an IOC market order) — check
    :attr:`OrderStatus.filled_qty` rather than assuming nothing filled.
    """

    OPEN = "open"  # accepted, nothing filled yet
    PARTIALLY_FILLED = "partially-filled"  # some filled, still working
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

    @property
    def is_final(self) -> bool:
        """``True`` once the order can no longer change."""
        return self in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED)


@dataclass(frozen=True, kw_only=True)
class OrderStatus:
    """Current state of a placed order, as reported by the exchange.

    ``quantity`` is what was requested; ``filled_qty`` what actually executed,
    at ``avg_price`` (``None`` while nothing has filled).  ``filled_value`` is
    the executed notional in quote currency.

    ``fees`` maps fee coin → amount paid (positive = paid, negative =
    rebate).  Spot fees may be charged in the base coin (e.g. ``{"ETH": …}``
    on a Bybit taker buy), in which case the coin received is
    ``filled_qty`` minus that fee.

    ``raw_status`` keeps the exchange's own status string for debugging.
    """

    order_id: str
    client_order_id: str | None
    symbol: str
    side: OrderSide
    state: OrderState
    quantity: Decimal
    filled_qty: Decimal
    leaves_qty: Decimal
    avg_price: Decimal | None
    filled_value: Decimal
    fees: dict[str, Decimal] = field(default_factory=dict)
    reject_reason: str | None = None
    raw_status: str = ""
    updated_at: datetime | None = None
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Fill confirmation
# ---------------------------------------------------------------------------


class FillStatus(StrEnum):
    """Outcome of one strategy signal, as reported back to the strategy."""

    FILLED = "filled"  # the full requested quantity executed
    PARTIAL = "partial"  # some executed, the rest was cancelled (IOC)
    REJECTED = "rejected"  # nothing executed (refused, cancelled, or not found)
    DROPPED = "dropped"  # never sent: quantity rounds below the lot / min size
    BLOCKED = "blocked"  # never sent: the strategy's guard has tripped
    UNKNOWN = "unknown"  # sent, but the exchange could not be asked what happened


@dataclass(frozen=True, kw_only=True)
class FillConfirmation:
    """Published by the consolidator back to each strategy worker for every
    signal it handled — including ones where nothing filled.

    ``quantity`` is what actually executed, signed (positive = bought,
    negative = sold), net of fees charged in the base coin — i.e. the real
    change in the strategy's holdings.  It is ``0`` unless ``status`` is
    ``FILLED`` or ``PARTIAL``; ``fill_price`` is then ``0`` too.
    ``fill_price`` is the exchange's average execution price.

    ``requested_qty`` is the signed quantity that was sent (after lot-size
    rounding).  ``fees`` maps fee coin → amount paid.  ``reason`` explains
    a non-``FILLED`` status.

    ``exchange`` is the broker name (e.g. "bybit_demo", "bybit_spot") so
    consumers can decide whether to include this fill in PnL tracking.
    """

    strategy_id: str
    symbol: str
    quantity: Decimal  # signed: positive = bought, negative = sold
    fill_price: Decimal
    exchange: str = ""
    status: FillStatus = FillStatus.FILLED
    requested_qty: Decimal | None = None
    fees: dict[str, Decimal] = field(default_factory=dict)
    order_id: str | None = None
    order_link_id: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Instrument rules (order sizing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class InstrumentRules:
    """An exchange's order-size rules for one symbol.

    ``qty_step``: quantities must be a multiple of it.  ``min_qty`` /
    ``max_market_qty``: smallest order / largest market order (base coin).
    ``min_notional``: smallest order value in the quote coin.  A rule the
    exchange doesn't report is ``None`` (not checked).  ``trading`` is
    ``False`` for a symbol that can't be traded right now.
    """

    symbol: str
    qty_step: Decimal
    min_qty: Decimal | None = None
    max_market_qty: Decimal | None = None
    min_notional: Decimal | None = None
    trading: bool = True


# ---------------------------------------------------------------------------
# Funding
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class FundingSettlement:
    """One funding payment on the account, for one perp symbol.

    ``amount`` is signed: positive = received, negative = paid.
    ``position`` is the account's signed position size at settlement
    (positive = long), or ``None`` if the exchange didn't report it.
    """

    settlement_id: str
    symbol: str
    settled_at: datetime
    amount: Decimal
    position: Decimal | None


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
