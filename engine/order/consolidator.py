from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING

from execution.types import OrderSide, OrderType, PerpOrder

if TYPE_CHECKING:
    import asyncio

    from interfaces.broker import BaseBroker
    from interfaces.signals import TargetPosition


class OrderConsolidator:
    """Single async task that nets strategy targets and places broker orders.

    Receives :class:`~interfaces.signals.TargetPosition` from all
    :class:`~engine.strategy.runner.StrategyRunner` instances, maintains per-strategy
    desired quantities, sums them into a net position, diffs against the
    current broker position, and places a market order for the delta.

    Orders smaller than *min_order_size* are silently skipped to avoid
    rounding noise.
    """

    def __init__(
        self,
        target_queue: asyncio.Queue[TargetPosition],
        broker: BaseBroker,
        min_order_size: Decimal = Decimal("0.001"),
    ) -> None:
        self._target_queue = target_queue
        self._broker = broker
        self._min_order_size = min_order_size
        # strategy_id → {symbol → desired_quantity}
        self._desired: dict[str, dict[str, Decimal]] = defaultdict(dict)

    async def run(self) -> None:
        """Consume targets from the queue and reconcile until cancelled."""
        while True:
            target = await self._target_queue.get()
            await self._reconcile(target)

    async def _reconcile(self, target: TargetPosition) -> None:
        self._desired[target.strategy_id][target.symbol] = target.quantity
        symbol = target.symbol

        net_qty = sum(
            strategy_targets.get(symbol, Decimal(0))
            for strategy_targets in self._desired.values()
        )

        position = await self._broker.position(symbol)
        current_qty = position.quantity if position is not None else Decimal(0)

        delta = net_qty - current_qty
        if abs(delta) < self._min_order_size:
            return

        side = OrderSide.BUY if delta > Decimal(0) else OrderSide.SELL
        order = PerpOrder(
            symbol=symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=abs(delta),
            price=Decimal(0),
        )
        await self._broker.place_order(order)
