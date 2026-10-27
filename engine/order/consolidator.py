from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from engine.order.ledger import base_coin_fee
from execution.types import (
    FillConfirmation,
    FillStatus,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from interfaces.signals import TargetPosition

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from engine.order.guard_registry import StrategyGuardRegistry
    from engine.order.ledger import LedgerDB, Trade
    from engine.order.snapshots import SnapshotDB
    from interfaces.broker import BaseBroker

OrderFactory = Callable[[str, OrderSide, Decimal, Decimal, str], Order]
# (exchange, order, approximate price) → why it can't be sent, or None.
# E.g. InstrumentCache.check (engine/order/instruments.py): Bybit's lot size,
# minimum / maximum quantity and minimum order value.
OrderCheck = Callable[[str, Order, Decimal], "str | None"]


class _ClosePositions:
    """Lane item: close the lane's strategy's own holdings (guard trip)."""


_CLOSE = _ClosePositions()


def _default_order_factory(
    symbol: str,
    side: OrderSide,
    quantity: Decimal,
    _price: Decimal,
    _exchange: str = "",
) -> Order:
    return Order(
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        price=Decimal(0),
    )


class OrderConsolidator:
    """Turns strategy signals into broker orders and reports what really filled.

    Every :class:`~interfaces.signals.TargetPosition` becomes exactly one
    order that belongs to exactly one strategy — signals are **not** netted
    against each other or against the broker's position.  The order's
    quantity is the signal's own (rounded down to the lot size by the order
    factory).

    After placing an order the consolidator polls the broker's
    ``order_status`` until the order is final (up to ``fill_timeout``;
    an order still open by then is cancelled and read again), then
    publishes a :class:`~execution.types.FillConfirmation` with the real
    filled quantity, average price and fees — or with ``quantity=0`` and a
    non-``FILLED`` status when nothing executed.  The strategy's guard is
    fed the same real fill.

    Each strategy gets its own *lane*: its signals are handled one after
    another (so its fills arrive in order), while other strategies' signals
    are handled concurrently.

    Each signal carries an optional ``exchange`` field.  When set, the order
    goes to the broker registered under that name; otherwise to
    ``default_exchange``.

    Example broker map::

        brokers = {
            "bybit_demo": BybitBroker(demo=True),
            "bybit_spot": BybitSpotBroker(demo=True),
        }
        consolidator = OrderConsolidator(
            target_queue=queue,
            brokers=brokers,
            default_exchange="bybit_demo",
        )
    """

    def __init__(
        self,
        target_queue: asyncio.Queue[TargetPosition],
        brokers: dict[str, BaseBroker],
        default_exchange: str,
        min_order_size: Decimal = Decimal(0),
        placed_queue: asyncio.Queue[tuple[str, Order]] | None = None,
        fills_queue: asyncio.Queue[FillConfirmation] | None = None,
        order_factory: OrderFactory = _default_order_factory,
        guard_registry: StrategyGuardRegistry | None = None,
        ledger: LedgerDB | None = None,
        snapshots: SnapshotDB | None = None,
        order_check: OrderCheck | None = None,
        fill_timeout: float = 30.0,
        poll_interval: float = 0.2,
        max_poll_interval: float = 1.0,
        cancel_timeout: float = 5.0,
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
        self._guard_registry = guard_registry
        self._ledger = ledger
        self._snapshots = snapshots
        self._order_check = order_check
        self._fill_timeout = fill_timeout
        self._poll_interval = poll_interval
        self._max_poll_interval = max_poll_interval
        self._cancel_timeout = cancel_timeout
        # strategy_id → {(exchange, symbol) → cumulative filled qty}, from real
        # fills only.  Bookkeeping for tracked_symbols; never used for sizing.
        self._positions: dict[str, dict[tuple[str, str], Decimal]] = defaultdict(dict)
        # strategy_id → that strategy's pending signals (and position closes)
        self._lanes: dict[str, asyncio.Queue[TargetPosition | _ClosePositions]] = {}
        self._tg: asyncio.TaskGroup | None = None  # set while run() is running
        # Closes asked for without lanes (handle(), the backtest): done after
        # the current signal.
        self._closes_waiting: list[str] = []

    @property
    def brokers(self) -> dict[str, BaseBroker]:
        return self._brokers

    @property
    def default_exchange(self) -> str:
        return self._default_exchange

    def tracked_symbols_for(self, exchange: str) -> set[str]:
        """All symbols that have seen at least one fill on *exchange*."""
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

    # --------------------------------------------------------------- recovery

    async def recover_unfinished(self) -> None:
        """Finish trades a previous run left ``pending`` or ``unknown``.

        Call once at startup, before :meth:`run`.  Each such trade is looked
        up on its exchange by tag (cancelled if somehow still open) and its
        real outcome recorded in the ledger.  Strategies are **not** told:
        they may have restarted too and moved on.

        - found → the real fill is recorded (holdings updated);
        - not found, never confirmed accepted → ``rejected`` (it never
          reached the exchange);
        - not found, but was accepted → stays ``unknown``, logged as ERROR;
        - exchange unreachable → left as is, retried on the next start.
        """
        if self._ledger is None:
            return
        for trade in self._ledger.unfinished_trades():
            try:
                await self._recover_one(trade)
            except Exception:
                log.exception("recovering trade %s failed", trade.order_link_id)

    async def _recover_one(self, trade: Trade) -> None:
        assert self._ledger is not None
        broker = self._brokers.get(trade.exchange)
        if broker is None or not hasattr(broker, "order_status"):
            log.warning(
                "can't check unfinished trade %s on %r — left %s",
                trade.order_link_id, trade.exchange, trade.status,
            )
            return

        # A trade from just before a crash gets the rest of its normal wait;
        # older ones get a single lookup.
        age = (datetime.now(UTC) - trade.created_at).total_seconds()
        status, answered = await self._await_final(
            broker, trade.order_id, trade.order_link_id, trade.symbol,
            timeout=max(0.0, self._fill_timeout - age),
        )
        order = Order(
            symbol=trade.symbol,
            side=trade.side,
            order_type=OrderType.MARKET,
            quantity=trade.requested_qty,
            price=Decimal(0),
            client_order_id=trade.order_link_id,
        )
        sign = Decimal(1) if trade.side is OrderSide.BUY else Decimal(-1)

        if status is not None:
            fill = self.fill_from_status(
                trade.strategy, trade.exchange, order, status, sign
            )
        elif not answered:
            log.warning(
                "unfinished trade %s: %r unreachable — retrying next start",
                trade.order_link_id, trade.exchange,
            )
            return
        else:
            accepted = trade.order_id is not None
            fill = FillConfirmation(
                strategy_id=trade.strategy,
                symbol=trade.symbol,
                quantity=Decimal(0),
                fill_price=Decimal(0),
                exchange=trade.exchange,
                status=FillStatus.UNKNOWN if accepted else FillStatus.REJECTED,
                requested_qty=sign * trade.requested_qty,
                order_id=trade.order_id,
                order_link_id=trade.order_link_id,
                reason=(
                    "order accepted but not found on the exchange after restart"
                    if accepted
                    else "order not found on the exchange after restart"
                    " (never reached it)"
                ),
            )

        self._ledger.record_result(fill)
        log_fn = log.error if fill.status is FillStatus.UNKNOWN else log.warning
        log_fn(
            "recovered trade %s for %r: %s filled=%s @ %s%s",
            trade.order_link_id, trade.strategy, fill.status.upper(),
            fill.quantity, fill.fill_price,
            f": {fill.reason}" if fill.reason else "",
        )

    # ------------------------------------------------------------------ lanes

    async def run(self) -> None:
        """Route each signal to its strategy's lane, creating lanes on demand."""
        async with asyncio.TaskGroup() as tg:
            self._tg = tg
            try:
                while True:
                    target = await self._target_queue.get()
                    self._lane(target.strategy_id).put_nowait(target)
            finally:
                self._tg = None

    def _lane(self, strategy_id: str) -> asyncio.Queue[TargetPosition | _ClosePositions]:
        assert self._tg is not None
        lane = self._lanes.get(strategy_id)
        if lane is None:
            lane = self._lanes[strategy_id] = asyncio.Queue()
            self._tg.create_task(self._run_lane(strategy_id, lane))
        return lane

    async def _run_lane(
        self, strategy_id: str, lane: asyncio.Queue[TargetPosition | _ClosePositions]
    ) -> None:
        while True:
            item = await lane.get()
            try:
                if isinstance(item, _ClosePositions):
                    await self._close_positions(strategy_id)
                else:
                    await self._reconcile(item)
            except Exception:
                # One bad signal must not take down the lane (or every other
                # strategy's lane with it).
                log.exception(
                    "signal handling crashed for strategy_id=%r: %s",
                    strategy_id,
                    "closing positions" if isinstance(item, _ClosePositions)
                    else f"{item.symbol} qty={item.quantity}",
                )

    # ------------------------------------------------------- closing on trip

    def request_close(self, strategy_id: str) -> None:
        """Queue closing *strategy_id*'s own holdings (from the ledger) in its
        lane — after any order of it already in flight, so the holdings read
        are final.  Only that strategy's positions are closed; other
        strategies sharing the symbol keep theirs."""
        if self._ledger is None:
            log.error("can't close %r's positions: no ledger", strategy_id)
            return
        if self._tg is None:
            self._closes_waiting.append(strategy_id)
        else:
            self._lane(strategy_id).put_nowait(_CLOSE)

    def close_tripped(self) -> None:
        """Queue closes for every strategy whose guard just tripped with
        ``close_on_trip`` — called after each fill and each periodic check."""
        if self._guard_registry is None or not hasattr(self._guard_registry, "pop_to_close"):
            return
        for strategy_id in self._guard_registry.pop_to_close():
            self.request_close(strategy_id)

    async def _close_positions(self, strategy_id: str) -> None:
        """Send one order per open holding of *strategy_id* that brings it to
        0 — tagged as its orders, recorded in its ledger, let through its
        tripped guard.  Not reduce-only: the account's position is the sum
        of every strategy's, so a reduce-only order could be refused (or cut)
        when another strategy holds the other side."""
        assert self._ledger is not None
        if strategy_id not in self._ledger.strategy_ids():
            log.info("guard close for %r: no orders, nothing to close", strategy_id)
            return
        open_ = [h for h in self._ledger.snapshot(strategy_id).holdings if h.quantity != 0]
        if not open_:
            log.info("guard close for %r: no open positions", strategy_id)
            return
        log.warning(
            "guard close for %r: closing %s",
            strategy_id,
            ", ".join(f"{h.exchange}:{h.symbol} {h.quantity}" for h in open_),
        )
        for h in open_:
            await self._reconcile(
                TargetPosition(
                    symbol=h.symbol,
                    quantity=-h.quantity,
                    price=h.avg_entry_price,  # only for the min-value check
                    strategy_id=strategy_id,
                    exchange=h.exchange,
                ),
                guard_close=True,
            )
        left = [h for h in self._ledger.snapshot(strategy_id).holdings if h.quantity != 0]
        if left:
            # Spot sells leave dust below the minimum order size; anything
            # bigger means an order failed.
            log.error(
                "guard close for %r: still open afterwards: %s — check them; retry"
                " with `python -m workers.guard_control close %s`",
                strategy_id,
                ", ".join(f"{h.exchange}:{h.symbol} {h.quantity}" for h in left),
                strategy_id,
            )
        else:
            log.warning("guard close for %r: all positions closed", strategy_id)

    # ------------------------------------------------------------- one signal

    async def handle(self, target: TargetPosition) -> None:
        """Handle one signal now, without the lanes — for the backtest,
        which works off every signal in order itself.  Closes a trip asked
        for are done right after it."""
        await self._reconcile(target)
        await self.run_waiting_closes()

    async def run_waiting_closes(self) -> None:
        """Without lanes (the backtest): do the closes asked for so far."""
        while self._closes_waiting:
            await self._close_positions(self._closes_waiting.pop(0))

    async def _reconcile(self, target: TargetPosition, *, guard_close: bool = False) -> None:
        """Place one signal's order, wait for the outcome, and report it.
        *guard_close*: an order closing a tripped strategy's position — the
        trip doesn't block it."""
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

        if (
            not guard_close
            and self._guard_registry is not None
            and not self._guard_registry.is_active(strategy_id)
        ):
            # Guard tripped (or was never configured active) for this
            # strategy — the signal is not sent. Unlike the old in-process
            # guard, this cannot be bypassed by the strategy's own container.
            # It is still answered (BLOCKED), so the strategy isn't left
            # waiting for a fill that never comes (B25).
            describe = getattr(self._guard_registry, "describe_trip", None)
            why = describe(strategy_id) if describe is not None else None
            reason = f"{why or 'guard tripped'} - signal not sent"
            log.warning(
                "signal BLOCKED for halted strategy_id=%r: %s qty=%s (%s)",
                strategy_id, symbol, qty_delta, reason,
            )
            self._deliver(
                FillConfirmation(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    quantity=Decimal(0),
                    fill_price=Decimal(0),
                    exchange=exchange,
                    status=FillStatus.BLOCKED,
                    requested_qty=qty_delta,
                    reason=reason,
                )
            )
            return

        side = OrderSide.BUY if qty_delta > Decimal(0) else OrderSide.SELL
        order = self._order_factory(symbol, side, abs(qty_delta), target.price, exchange)
        sign = Decimal(1) if side is OrderSide.BUY else Decimal(-1)

        def report(
            status: FillStatus,
            reason: str | None = None,
            order_id: str | None = None,
        ) -> None:
            self._deliver(
                FillConfirmation(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    quantity=Decimal(0),
                    fill_price=Decimal(0),
                    exchange=exchange,
                    status=status,
                    requested_qty=sign * order.quantity,
                    order_id=order_id,
                    order_link_id=order.client_order_id,
                    reason=reason,
                )
            )

        if order.quantity <= Decimal(0) or order.quantity < self._min_order_size:
            log.warning(
                "order DROPPED [%s] %s %s: qty=%s rounds to %s (min %s)",
                exchange, symbol, side, abs(qty_delta), order.quantity,
                self._min_order_size,
            )
            report(
                FillStatus.DROPPED,
                f"quantity {abs(qty_delta)} rounds to {order.quantity}, "
                f"below the minimum order size",
            )
            return

        # The exchange's own rules: lot size, min / max quantity, min value.
        problem = (
            self._order_check(exchange, order, target.price)
            if self._order_check is not None
            else None
        )
        if problem is not None:
            log.warning(
                "order DROPPED [%s] %s %s qty=%s: %s",
                exchange, symbol, side, order.quantity, problem,
            )
            report(FillStatus.DROPPED, problem)
            return

        # Tag the order "{strategy_num}_{counter}" so the exchange-side
        # record (Bybit orderLinkId) traces back to this strategy, and so the
        # order can be looked up even if place_order never returns its id.
        if self._ledger is not None:
            # Logged as pending before it's sent: a crash from here on leaves
            # a row that the next start finishes (recover_unfinished).
            order = replace(
                order,
                client_order_id=self._ledger.begin_order(
                    strategy_id, exchange, symbol, side, order.quantity
                ),
            )
        tag = order.client_order_id

        result = await broker.place_order(order)
        if self._ledger is not None and tag and result.error is None and result.order_id:
            self._ledger.record_accepted(tag, result.order_id)

        if result.error and not result.maybe_placed:
            log.error(
                "order REJECTED [%s] %s %s qty=%s link=%s: %s",
                exchange, symbol, side, order.quantity, tag, result.error,
            )
            report(FillStatus.REJECTED, result.error)
            return

        if not hasattr(broker, "order_status"):
            # Broker can't be asked what happened (paper, Lighter): trust
            # the placement result as before.
            if result.error:
                log.error(
                    "order FAILED [%s] %s %s qty=%s: %s",
                    exchange, symbol, side, order.quantity, result.error,
                )
                report(FillStatus.UNKNOWN, result.error)
                return
            if self._placed_queue is not None:
                await self._placed_queue.put((exchange, order))
            self._deliver(
                FillConfirmation(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    quantity=sign * order.quantity,
                    fill_price=result.fill_price or target.price,
                    exchange=exchange,
                    requested_qty=sign * order.quantity,
                    order_id=result.order_id,
                    order_link_id=tag,
                )
            )
            return

        if result.error:
            log.warning(
                "order request failed in transit [%s] %s %s qty=%s link=%s: %s"
                " — checking whether Bybit has it",
                exchange, symbol, side, order.quantity, tag, result.error,
            )
            if not tag:
                report(FillStatus.UNKNOWN, f"{result.error} (order has no tag)")
                return

        status, answered = await self._await_final(
            broker, result.order_id, tag, symbol
        )

        if status is None:
            if answered and result.error is None:
                # Bybit accepted the order, so it exists — our lookup can't
                # see it (wrong query, or Bybit's read side is lagging).
                log.error(
                    "order ACCEPTED but NOT FOUND [%s] %s %s qty=%s link=%s id=%s"
                    " after %.0fs — check the order on Bybit by hand",
                    exchange, symbol, side, order.quantity, tag, result.order_id,
                    self._fill_timeout,
                )
                report(
                    FillStatus.UNKNOWN,
                    f"order accepted but not found on the exchange after "
                    f"{self._fill_timeout:.0f}s",
                    result.order_id,
                )
            elif answered:
                # Lost in transit and still not there: Bybit refuses requests
                # older than its 5s receive window, so it can't arrive now.
                log.error(
                    "order NOT FOUND [%s] %s %s qty=%s link=%s: %s",
                    exchange, symbol, side, order.quantity, tag, result.error,
                )
                report(
                    FillStatus.REJECTED,
                    f"request lost in transit ({result.error}), order not found "
                    f"on the exchange after {self._fill_timeout:.0f}s",
                )
            else:
                log.error(
                    "order outcome UNKNOWN [%s] %s %s qty=%s link=%s id=%s:"
                    " exchange did not answer within %.1fs",
                    exchange, symbol, side, order.quantity, tag, result.order_id,
                    self._fill_timeout,
                )
                report(
                    FillStatus.UNKNOWN,
                    "could not reach the exchange to confirm the order",
                    result.order_id,
                )
            return

        fill = self.fill_from_status(
            strategy_id, exchange, order, status, sign
        )
        if fill.status is FillStatus.FILLED:
            log_fn = log.info
        elif status.state.is_final:
            log_fn = log.warning
        else:
            # Still open even after the cancel attempt — may keep filling.
            log_fn = log.error
        log_fn(
            "order %s [%s] %s %s req=%s filled=%s @ %s fees=%s link=%s id=%s%s",
            fill.status.upper(), exchange, symbol, side, order.quantity,
            status.filled_qty, status.avg_price, status.fees, tag,
            status.order_id, f": {fill.reason}" if fill.reason else "",
        )

        if status.filled_qty > Decimal(0) and self._placed_queue is not None:
            await self._placed_queue.put(
                (
                    exchange,
                    replace(
                        order,
                        quantity=status.filled_qty,
                        price=status.avg_price or Decimal(0),
                    ),
                )
            )
        self._deliver(fill)

    async def _await_final(
        self,
        broker: BaseBroker,
        order_id: str | None,
        tag: str | None,
        symbol: str,
        timeout: float | None = None,
    ) -> tuple[OrderStatus | None, bool]:
        """Wait for the order to become final, cancelling it if it's still
        open after *timeout* (default ``fill_timeout``).

        After a cancel the status is read again until final, so the filled
        quantity reported can't grow afterwards.  The status is re-read even
        when the cancel fails: "too late to cancel" means it just finished.

        Returns the last status seen (``None`` if the exchange never reported
        the order) and whether the exchange answered at all.
        """
        timeout = self._fill_timeout if timeout is None else timeout
        status, answered = await self._poll_until_final(
            broker, order_id, tag, symbol, timeout
        )
        if status is None or status.state.is_final:
            return status, answered

        log.warning(
            "order still %s after %.0fs link=%s id=%s — cancelling",
            status.raw_status, timeout, tag, status.order_id,
        )
        try:
            cancel = await broker.cancel_order(
                status.order_id, symbol=symbol
            )
            if cancel.error:
                log.warning("cancel failed link=%s: %s", tag, cancel.error)
        except Exception as exc:
            log.warning("cancel failed link=%s: %s", tag, exc)

        after, _ = await self._poll_until_final(
            broker, status.order_id, tag, symbol, self._cancel_timeout
        )
        return after or status, answered

    async def _poll_until_final(
        self,
        broker: BaseBroker,
        order_id: str | None,
        tag: str | None,
        symbol: str,
        timeout: float,
    ) -> tuple[OrderStatus | None, bool]:
        """Poll ``broker.order_status`` until the order is final or *timeout*
        passes, starting at ``poll_interval`` and slowing down to
        ``max_poll_interval``.

        Lookup errors are retried — "couldn't ask" is never treated as
        "doesn't exist".
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        interval = self._poll_interval
        status: OrderStatus | None = None
        answered = False
        while True:
            try:
                status = await broker.order_status(
                    order_id=order_id, client_order_id=tag, symbol=symbol
                )
                answered = True
            except Exception as exc:
                log.warning(
                    "order_status lookup failed link=%s id=%s: %s", tag, order_id, exc
                )
            if status is not None and status.state.is_final:
                return status, answered
            if loop.time() >= deadline:
                return status, answered
            await asyncio.sleep(interval)
            interval = min(interval * 1.5, self._max_poll_interval)

    @staticmethod
    def fill_from_status(
        strategy_id: str,
        exchange: str,
        order: Order,
        status: OrderStatus,
        sign: Decimal,
    ) -> FillConfirmation:
        """The :class:`FillConfirmation` for *order* given its exchange
        *status* — also used by the ledger recovery, so a rebuilt ledger
        gets exactly the numbers the consolidator would have recorded."""
        filled = status.filled_qty
        # Spot buys can be charged their fee in the base coin: the strategy
        # holds filled_qty minus that fee.
        received = filled - base_coin_fee(order.symbol, status.fees) if (
            order.side is OrderSide.BUY
        ) else filled

        reason: str | None = None
        if filled == Decimal(0):
            if status.state.is_final:
                fill_status = FillStatus.REJECTED
                reason = status.reject_reason or f"order {status.raw_status}"
            else:
                fill_status = FillStatus.UNKNOWN
                reason = f"order still {status.raw_status} after timeout"
        elif filled < order.quantity:
            fill_status = FillStatus.PARTIAL
            reason = (
                f"order {status.raw_status}, {filled} of {order.quantity} filled"
                if status.state.is_final
                else f"order still {status.raw_status} after timeout, "
                f"{filled} of {order.quantity} filled so far"
            )
        else:
            fill_status = FillStatus.FILLED

        return FillConfirmation(
            strategy_id=strategy_id,
            symbol=order.symbol,
            quantity=sign * received if filled > Decimal(0) else Decimal(0),
            fill_price=status.avg_price or Decimal(0),
            exchange=exchange,
            status=fill_status,
            requested_qty=sign * order.quantity,
            fees=status.fees,
            order_id=status.order_id,
            order_link_id=status.client_order_id or order.client_order_id,
            reason=reason,
        )

    def _maybe_snapshot(self, strategy_id: str) -> None:
        """Save a snapshot of *strategy_id* if 50 orders have passed since its
        last one.  (The daily ``workers/snapshot_all.py`` covers strategies
        that trade less.)

        Called right after one of its orders is recorded; its orders are
        handled one at a time, so none is in flight then.  A failure is
        logged, never raised.
        """
        if self._snapshots is None or self._ledger is None:
            return
        try:
            counter = self._ledger.counter(strategy_id)
            if not self._snapshots.due(strategy_id, counter):
                return
            snap = self._ledger.snapshot(strategy_id)
            self._snapshots.save(snap)
        except Exception:
            log.exception("snapshot failed for strategy_id=%r", strategy_id)
            return
        log.info(
            "snapshot saved for %r at order %d: realized=%s fees=%s net=%s"
            " holdings=%d unfinished=%d",
            strategy_id, snap.counter, snap.realized_pnl, snap.fees_paid,
            snap.net_pnl, len(snap.holdings), len(snap.unfinished),
        )

    def _deliver(self, fill: FillConfirmation) -> None:
        """Record *fill* in the ledger, then send it to the strategy; feed
        real executions to the guard.

        The ledger comes first: it's the durable record, while the message
        to the strategy can be lost.  A ledger failure is logged but doesn't
        stop the report — the strategy must still hear what happened.
        """
        if self._ledger is not None:
            try:
                self._ledger.record_result(fill)
            except Exception:
                log.exception(
                    "LEDGER WRITE FAILED for %s — trade log and holdings are now"
                    " out of date: %s", fill.order_link_id, fill,
                )
            else:
                if fill.order_link_id:
                    self._maybe_snapshot(fill.strategy_id)
        if fill.quantity != Decimal(0):
            key = (fill.exchange, fill.symbol)
            pos = self._positions[fill.strategy_id]
            pos[key] = pos.get(key, Decimal(0)) + fill.quantity
            if self._guard_registry is not None:
                self._guard_registry.record_fill(
                    fill.strategy_id,
                    fill.symbol,
                    fill.exchange,
                    fill.quantity,
                    fill.fill_price,
                )
            self.close_tripped()
        if self._fills_queue is not None:
            self._fills_queue.put_nowait(fill)
