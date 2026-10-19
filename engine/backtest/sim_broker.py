"""A simulated Bybit for backtests: fills, fees, positions and funding.

:class:`SimExchange` holds what both markets share — the simulated clock,
the historical bars (the price source), the fee model.  Each
:class:`SimBroker` is one market on it (``bybit_demo`` perps or
``bybit_spot`` spot) and implements the full ``BaseBroker`` contract, so the
real consolidator, ledger, guard and funding split run on it unchanged.

**Fills.** A market order fills completely at the **open of the next bar**
after the current simulated time — you can't trade at a close you only just
saw — moved against you by ``slippage_bps``.  No bar after now (end of the
data) → the order is refused.  Orders are final immediately, so the
consolidator never waits.

**Fees** (Bybit taker): perps ``perp_fee`` of the value, in USDT.  Spot buys
pay ``spot_fee`` in the **base coin** (you receive less coin), spot sells
in USDT — like Bybit, so the consolidator's base-coin-fee handling is
exercised exactly as live.

**Funding.** :meth:`SimBroker.settle_funding` pays the perp position at a
settlement: ``-position × mark × rate`` (longs pay a positive rate), with
the mark taken as the last bar close before the settlement.

**Simplifications (v1):** spot uses the perp bars as its price (no basis);
no margin, leverage or liquidation; a spot sell needs the coin balance; no
order book, so size doesn't move the price beyond ``slippage_bps``.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from engine.order.ledger import apply_fill
from execution.types import (
    FundingSettlement,
    InstrumentRules,
    Order,
    OrderResult,
    OrderSide,
    OrderState,
    OrderStatus,
    PerpPosition,
    Position,
)
from interfaces.broker import BaseBroker

if TYPE_CHECKING:
    from datetime import datetime

    from data.types import TimeBar
    from engine.clock import SimClock

PERP = "perp"
SPOT = "spot"
_QUOTES = ("USDT", "USDC")


def base_coin(symbol: str) -> str:
    for quote in _QUOTES:
        if symbol.endswith(quote):
            return symbol[: -len(quote)]
    return symbol


@dataclass
class _Series:
    """One symbol's bars, sorted, with lookup keys."""

    bars: list[TimeBar]

    def __post_init__(self) -> None:
        self.bars.sort(key=lambda b: b.timestamp)
        self.opens = [b.timestamp for b in self.bars]
        self.closes = [b.close_time for b in self.bars]


class SimExchange:
    """The shared simulated exchange: clock, prices, fees, order-size rules."""

    def __init__(
        self,
        clock: SimClock,
        bars: dict[str, list[TimeBar]],
        *,
        perp_fee: Decimal = Decimal("0.00055"),
        spot_fee: Decimal = Decimal("0.001"),
        slippage_bps: Decimal = Decimal(0),
        rules: dict[tuple[str, str], InstrumentRules] | None = None,
    ) -> None:
        self.clock = clock
        self._series = {symbol: _Series(list(b)) for symbol, b in bars.items()}
        self.perp_fee = perp_fee
        self.spot_fee = spot_fee
        self.slippage = slippage_bps / Decimal(10_000)
        self.rules = dict(rules or {})

    def mark(self, symbol: str) -> Decimal | None:
        """The last bar close known now (closed at or before the clock)."""
        series = self._series.get(symbol)
        if series is None:
            return None
        i = bisect.bisect_right(series.closes, self.clock.now()) - 1
        return series.bars[i].close if i >= 0 else None

    def next_open(self, symbol: str) -> tuple[Decimal, datetime] | None:
        """Open price and time of the first bar opening at or after now."""
        series = self._series.get(symbol)
        if series is None:
            return None
        i = bisect.bisect_left(series.opens, self.clock.now())
        return (series.bars[i].open, series.bars[i].timestamp) if i < len(series.bars) else None

    async def price(self, exchange: str, symbol: str) -> Decimal | None:
        """Price source for the guard / valuation (same signature as BybitPrices)."""
        return self.mark(symbol)


class SimBroker(BaseBroker):
    """One market (perp or spot) of a :class:`SimExchange`."""

    def __init__(self, sim: SimExchange, exchange: str, market: str) -> None:
        if market not in (PERP, SPOT):
            raise ValueError(f"market must be {PERP!r} or {SPOT!r}")
        self.sim = sim
        self.exchange = exchange
        self.market = market
        self._statuses: dict[str, OrderStatus] = {}
        self._by_link: dict[str, str] = {}
        self._history: list[OrderStatus] = []
        # perp: symbol → (signed qty, avg entry); spot: base coin → balance
        self._perp: dict[str, tuple[Decimal, Decimal]] = {}
        self._spot: dict[str, Decimal] = {}
        self._settlements: list[FundingSettlement] = []
        self._count = 0

    # ----------------------------------------------------------------- trading

    async def place_order(self, order: Order) -> OrderResult:
        quote = self.sim.next_open(order.symbol)
        if quote is None:
            return OrderResult(
                order_id=None, order=order,
                error=f"no price data for {order.symbol} after {self.sim.clock.now()}",
            )
        open_price, _ = quote
        buy = order.side is OrderSide.BUY
        price = open_price * (1 + self.sim.slippage if buy else 1 - self.sim.slippage)
        qty = order.quantity
        value = qty * price

        fees: dict[str, Decimal]
        if self.market == PERP:
            fees = {"USDT": value * self.sim.perp_fee}
            held, avg = self._perp.get(order.symbol, (Decimal(0), Decimal(0)))
            new_qty, new_avg, _ = apply_fill(held, avg, Decimal(0), qty if buy else -qty, price)
            self._perp[order.symbol] = (new_qty, new_avg)
        else:
            coin = base_coin(order.symbol)
            balance = self._spot.get(coin, Decimal(0))
            if buy:
                fee = qty * self.sim.spot_fee
                fees = {coin: fee}
                self._spot[coin] = balance + qty - fee
            else:
                if qty > balance:
                    return OrderResult(
                        order_id=None, order=order,
                        error=f"insufficient {coin} balance: {balance} < {qty}",
                    )
                fees = {"USDT": value * self.sim.spot_fee}
                self._spot[coin] = balance - qty

        self._count += 1
        order_id = f"sim-{self.exchange}-{self._count}"
        now = self.sim.clock.now()
        status = OrderStatus(
            order_id=order_id, client_order_id=order.client_order_id, symbol=order.symbol,
            side=order.side, state=OrderState.FILLED, quantity=qty, filled_qty=qty,
            leaves_qty=Decimal(0), avg_price=price, filled_value=value, fees=fees,
            raw_status="Filled", created_at=now, updated_at=now,
        )
        self._statuses[order_id] = status
        if order.client_order_id:
            self._by_link[order.client_order_id] = order_id
        self._history.append(status)
        return OrderResult(order_id=order_id, order=order, error=None, fill_price=price)

    async def cancel_order(self, order_id: str, symbol: str | None = None) -> OrderResult:
        return OrderResult(order_id=order_id, order=None, error=None)  # nothing stays open

    async def cancel_all_orders(self, symbol: str | None = None) -> OrderResult:
        return OrderResult(order_id=None, order=None, error=None)

    # --------------------------------------------------------------- read-only

    async def order_status(
        self,
        order_id: str | None = None,
        client_order_id: str | None = None,
        symbol: str | None = None,
    ) -> OrderStatus | None:
        if order_id and order_id in self._statuses:
            return self._statuses[order_id]
        if client_order_id and client_order_id in self._by_link:
            return self._statuses[self._by_link[client_order_id]]
        return None

    async def order_history(self, start: datetime, end: datetime) -> list[OrderStatus]:
        return [s for s in self._history if s.created_at and start <= s.created_at <= end]

    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        return []

    async def position(self, symbol: str) -> Position | None:
        if self.market == PERP:
            qty, avg = self._perp.get(symbol, (Decimal(0), Decimal(0)))
            if qty == 0:
                return None
            mark = self.sim.mark(symbol) or avg
            return PerpPosition(
                symbol=symbol, quantity=qty, avg_entry_price=avg,
                unrealized_pnl=(mark - avg) * qty, realized_pnl=Decimal(0), mark_price=mark,
            )
        balance = self._spot.get(base_coin(symbol), Decimal(0))
        if balance <= 0:
            return None
        return Position(
            symbol=symbol, quantity=balance, avg_entry_price=self.sim.mark(symbol) or Decimal(0),
            unrealized_pnl=Decimal(0), realized_pnl=Decimal(0),
        )

    async def list_positions(self) -> list[Position]:
        symbols = (
            list(self._perp) if self.market == PERP else [f"{coin}USDT" for coin in self._spot]
        )
        return [p for s in sorted(symbols) if (p := await self.position(s)) is not None]

    async def instrument_rules(self) -> list[InstrumentRules]:
        return [r for (ex, _), r in sorted(self.sim.rules.items()) if ex == self.exchange]

    async def funding_settlements(
        self, start: datetime, end: datetime
    ) -> list[FundingSettlement]:
        return [s for s in self._settlements if start <= s.settled_at <= end]

    async def aclose(self) -> None:
        pass

    # ----------------------------------------------------------------- funding

    def settle_funding(
        self, symbol: str, rate: Decimal, at: datetime
    ) -> FundingSettlement | None:
        """Pay funding on the perp position held now: ``-position × mark ×
        rate`` (positive = received).  ``None`` if flat, spot, or no price."""
        if self.market != PERP:
            return None
        qty, _ = self._perp.get(symbol, (Decimal(0), Decimal(0)))
        mark = self.sim.mark(symbol)
        if qty == 0 or mark is None:
            return None
        settlement = FundingSettlement(
            settlement_id=f"sim-{symbol}-{at:%Y%m%dT%H%M}",
            symbol=symbol, settled_at=at, amount=-qty * mark * rate, position=qty,
        )
        self._settlements.append(settlement)
        return settlement
