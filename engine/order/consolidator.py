from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

from execution.types import FillConfirmation, Order, OrderSide, OrderType, PerpOrder

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    import asyncio

    from interfaces.broker import BaseBroker
    from interfaces.signals import TargetPosition

OrderFactory = Callable[[str, OrderSide, Decimal, Decimal, str], Order]


def _default_order_factory(
    symbol: str, side: OrderSide, quantity: Decimal, _price: Decimal, _exchange: str = ""
) -> Order:
    return Order(
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        price=Decimal(0),
    )


class OrderConsolidator:
    """Nets strategy signals and routes broker orders to the correct exchange.

    Each :class:`~interfaces.signals.TargetPosition` carries an optional
    ``exchange`` field.  When set, the signal is routed to the broker
    registered under that name.  When empty, the ``default_exchange`` is used.

    Netting is per ``(exchange, symbol)`` pair — signals to different exchanges
    for the same symbol are NOT netted against each other, enabling
    cross-exchange arbitrage strategies.

    Example broker map::

        brokers = {
            "bybit_paper": BybitPaperBroker(),
            "lighter":     LighterBroker(...),
        }
        consolidator = OrderConsolidator(
            target_queue=queue,
            brokers=brokers,
            default_exchange="bybit_paper",
        )

    A strategy that wants Lighter simply sets ``exchange="lighter"`` on its
    :class:`~interfaces.signals.TargetPosition`.  Everything else goes to
    ``"bybit_paper"`` automatically.
    """

    def __init__(
        self,
        target_queue: asyncio.Queue[TargetPosition],
        brokers: dict[str, BaseBroker],
        default_exchange: str,
        min_order_size: Decimal = Decimal("0.001"),
        placed_queue: asyncio.Queue[tuple[str, Order]] | None = None,
        fills_queue: asyncio.Queue[FillConfirmation] | None = None,
        order_factory: OrderFactory = _default_order_factory,
    ) -> None:
        if default_exchange not in brokers:
            raise ValueError(
                f"default_exchange={default_exchange!r} not in brokers dict "
                f"(available: {list(brokers)})"
            )
        self._target_queue = target_queue
        self._brokers = brokers
        self._default_exchange = default_exchange
        self._min_order_size = min_order_size
        self._placed_queue = placed_queue
        self._fills_queue = fills_queue
        self._order_factory = order_factory
        # strategy_id → {(exchange, symbol) → cumulative qty}
        self._positions: dict[str, dict[tuple[str, str], Decimal]] = defaultdict(dict)

    @property
    def brokers(self) -> dict[str, BaseBroker]:
        return self._brokers

    @property
    def default_exchange(self) -> str:
        return self._default_exchange

    def tracked_symbols_for(self, exchange: str) -> set[str]:
        """All symbols that have seen at least one signal for *exchange*."""
        return {
            sym
            for pos in self._positions.values()
            for (ex, sym) in pos
            if ex == exchange
        }

    @property
    def tracked_symbols(self) -> set[str]:
        """All symbols across all exchanges (union)."""
        return {sym for pos in self._positions.values() for (_ex, sym) in pos}

    async def seed_from_broker(self) -> None:
        """Pre-populate internal position tracking from live broker state.

        Call this once before ``run()`` to prevent spurious orders when the
        consolidator restarts while broker positions are already open.

        Seeding works by storing current broker quantities under a sentinel
        strategy key ``"__broker_seed__"``.  The net position calculation sums
        over all strategy maps, so existing positions are correctly accounted
        for and no orders are placed until a strategy actually requests a
        change.
        """
        seed_key = "__broker_seed__"
        for exchange, broker in self._brokers.items():
            if not hasattr(broker, "list_positions"):
                continue
            try:
                positions = await broker.list_positions()
            except Exception as exc:
                log.warning("Could not seed consolidator from %s: %s", exchange, exc)
                continue
            for pos in positions:
                if pos.quantity == Decimal(0):
                    continue
                key = (exchange, pos.symbol)
                self._positions[seed_key][key] = pos.quantity
                log.info(
                    "Consolidator seeded: [%s] %s qty=%s",
                    exchange, pos.symbol, pos.quantity,
                )

    async def run(self) -> None:
        while True:
            target = await self._target_queue.get()
            await self._reconcile(target)

    async def _reconcile(self, target: TargetPosition) -> None:
        exchange = target.exchange or self._default_exchange
        broker = self._brokers.get(exchange)
        if broker is None:
            # Unknown exchange — fall back to default rather than dropping the signal
            exchange = self._default_exchange
            broker = self._brokers[exchange]

        symbol = target.symbol
        strategy_id = target.strategy_id
        qty_delta = target.quantity

        if qty_delta == Decimal(0):
            return

        key = (exchange, symbol)

        old_pos = self._positions[strategy_id].get(key, Decimal(0))
        self._positions[strategy_id][key] = old_pos + qty_delta

        net_qty = sum(
            strat_pos.get(key, Decimal(0)) for strat_pos in self._positions.values()
        )

        position = await broker.position(symbol)
        current_qty = position.quantity if position is not None else Decimal(0)

        broker_delta = net_qty - current_qty
        fill_price = target.price

        order_failed = False
        if abs(broker_delta) >= self._min_order_size:
            side = OrderSide.BUY if broker_delta > Decimal(0) else OrderSide.SELL
            order = self._order_factory(symbol, side, abs(broker_delta), target.price, exchange)

            # For perp orders: mark reduce_only when the delta reduces or closes
            # an existing broker position. This ensures Bybit records the order
            # as "Close Short" / "Close Long" rather than "Open Long" / "Open Short",
            # and prevents accidentally opening an opposite position in hedge mode.
            if isinstance(order, PerpOrder) and current_qty != Decimal(0):
                is_reducing = (
                    (current_qty > 0 and broker_delta < 0)
                    or (current_qty < 0 and broker_delta > 0)
                )
                if is_reducing:
                    order = replace(order, reduce_only=True)

            result = await broker.place_order(order)

            if result.error:
                log.error(
                    "order FAILED [%s] %s %s qty=%s: %s",
                    exchange, symbol, side, abs(broker_delta), result.error,
                )
                order_failed = True
            else:
                log.info(
                    "order OK [%s] %s %s qty=%s id=%s",
                    exchange, symbol, side, abs(broker_delta), result.order_id,
                )

            if self._placed_queue is not None:
                await self._placed_queue.put((exchange, order))

            if result.ok and result.fill_price is not None:
                fill_price = result.fill_price

        # Only publish fill confirmation when the order actually succeeded (or
        # when broker_delta == 0, meaning the broker already holds the target
        # position — phantom fill used for strategy state reconstruction after
        # consolidator restart).
        if self._fills_queue is not None and not order_failed:
            self._fills_queue.put_nowait(
                FillConfirmation(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    quantity=qty_delta,
                    fill_price=fill_price,
                    exchange=exchange,
                )
            )
