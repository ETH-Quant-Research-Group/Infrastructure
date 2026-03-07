from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING

from execution.types import FillConfirmation, Order, OrderSide, OrderType

if TYPE_CHECKING:
    import asyncio

    from interfaces.broker import BaseBroker
    from interfaces.signals import TargetPosition

OrderFactory = Callable[[str, OrderSide, Decimal, Decimal], Order]


def _default_order_factory(
    symbol: str, side: OrderSide, quantity: Decimal, price: Decimal
) -> Order:
    order_type = OrderType.LIMIT if price > Decimal(0) else OrderType.MARKET
    return Order(
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
    )


class OrderConsolidator:
    """Single async task that nets strategy trade signals and places broker orders.

    Each :class:`~interfaces.signals.TargetPosition` carries a signed quantity
    **delta** (positive = buy, negative = sell) and an optional limit price
    (``price=0`` means market order).

    The consolidator accumulates deltas into a running position per strategy,
    sums across strategies to get the net, diffs against the broker, and places
    one order for any remaining delta.

    **Internal netting**: when deltas cancel each other the net falls below
    *min_order_size* — no broker order, no fees.  A :class:`FillConfirmation`
    is still published for each strategy at the signal's reference price so
    their PnL trackers stay accurate.

    **Fill feedback**: after every non-zero signal a ``FillConfirmation`` is put
    on *fills_queue*.  The fill price is the broker's actual execution price when
    an order was sent, or the signal's reference price when internally netted.
    """

    def __init__(
        self,
        target_queue: asyncio.Queue[TargetPosition],
        broker: BaseBroker,
        min_order_size: Decimal = Decimal("0.001"),
        placed_queue: asyncio.Queue[Order] | None = None,
        fills_queue: asyncio.Queue[FillConfirmation] | None = None,
        order_factory: OrderFactory = _default_order_factory,
    ) -> None:
        self._target_queue = target_queue
        self._broker = broker
        self._min_order_size = min_order_size
        self._placed_queue = placed_queue
        self._fills_queue = fills_queue
        self._order_factory = order_factory
        # strategy_id → {symbol → cumulative position}
        self._positions: dict[str, dict[str, Decimal]] = defaultdict(dict)

    async def run(self) -> None:
        """Consume targets from the queue and reconcile until cancelled."""
        while True:
            target = await self._target_queue.get()
            await self._reconcile(target)

    async def _reconcile(self, target: TargetPosition) -> None:
        symbol = target.symbol
        strategy_id = target.strategy_id
        qty_delta = target.quantity

        if qty_delta == Decimal(0):
            return

        # Accumulate this strategy's running position
        old_pos = self._positions[strategy_id].get(symbol, Decimal(0))
        self._positions[strategy_id][symbol] = old_pos + qty_delta

        # Net position across all strategies
        net_qty = sum(
            strat_pos.get(symbol, Decimal(0)) for strat_pos in self._positions.values()
        )

        position = await self._broker.position(symbol)
        current_qty = position.quantity if position is not None else Decimal(0)

        broker_delta = net_qty - current_qty

        # Default: signal's reference price (bar close at signal time).
        # Correct for internal netting where no broker order is placed.
        fill_price = target.price

        if abs(broker_delta) >= self._min_order_size:
            side = OrderSide.BUY if broker_delta > Decimal(0) else OrderSide.SELL
            order = self._order_factory(symbol, side, abs(broker_delta), target.price)
            result = await self._broker.place_order(order)

            if self._placed_queue is not None:
                await self._placed_queue.put(order)

            # Use actual broker fill price if available — more accurate than reference
            if result.ok and result.fill_price is not None:
                fill_price = result.fill_price

        if self._fills_queue is not None:
            self._fills_queue.put_nowait(
                FillConfirmation(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    quantity=qty_delta,
                    fill_price=fill_price,
                )
            )
