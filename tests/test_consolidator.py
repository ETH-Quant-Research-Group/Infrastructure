from __future__ import annotations

import asyncio
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

import pytest

from engine.data import codec
from engine.order.consolidator import OrderConsolidator
from engine.order.ledger import LedgerDB
from execution.types import (
    FillConfirmation,
    FillStatus,
    Order,
    OrderResult,
    OrderSide,
    OrderState,
    OrderStatus,
    OrderType,
    Position,
)
from interfaces.broker import BaseBroker
from tests.fakes import FakeBroker
from interfaces.signals import TargetPosition

if TYPE_CHECKING:
    from pathlib import Path

# A scripted reply to one order_status call.
Reply = OrderStatus | None | Exception


def _status(
    order: Order,
    state: OrderState,
    filled: str,
    price: str | None = "2500",
    fees: dict[str, Decimal] | None = None,
    raw: str = "",
    reason: str | None = None,
) -> OrderStatus:
    return OrderStatus(
        order_id=f"bybit-{order.client_order_id}",
        client_order_id=order.client_order_id,
        symbol=order.symbol,
        side=order.side,
        state=state,
        quantity=order.quantity,
        filled_qty=Decimal(filled),
        leaves_qty=Decimal(0),
        avg_price=Decimal(price) if price else None,
        filled_value=Decimal(filled) * Decimal(price or 0),
        fees=fees if fees is not None else {"USDT": Decimal("0.1")},
        reject_reason=reason,
        raw_status=raw or state.value,
    )


def _filled(order: Order) -> list[Reply]:
    return [_status(order, OrderState.FILLED, str(order.quantity))]


class _Broker(FakeBroker):
    """Fake exchange.  ``script(order)`` returns the replies its order_status
    calls will get, in order; the last one repeats.  After a cancel the
    replies switch to ``after_cancel(order)``, if given."""

    def __init__(
        self,
        script: object = _filled,
        place_error: str | None = None,
        maybe_placed: bool = False,
        after_cancel: object = None,
        cancel_error: str | None = None,
    ) -> None:
        self.script = script
        self.place_error = place_error
        self.maybe_placed = maybe_placed
        self.after_cancel = after_cancel
        self.cancel_error = cancel_error
        self.placed: list[Order] = []
        self.cancelled: list[tuple[str, str | None]] = []
        self.lookups = 0
        self._replies: dict[str | None, list[Reply]] = {}
        self._orders: dict[str | None, Order] = {}

    async def place_order(self, order: Order) -> OrderResult:
        self.placed.append(order)
        self._orders[order.client_order_id] = order
        self._replies[order.client_order_id] = list(self.script(order))  # type: ignore[operator]
        if self.place_error:
            return OrderResult(
                order_id=None,
                order=order,
                error=self.place_error,
                maybe_placed=self.maybe_placed,
            )
        return OrderResult(
            order_id=f"bybit-{order.client_order_id}", order=order, error=None
        )

    async def order_status(
        self,
        order_id: str | None = None,
        client_order_id: str | None = None,
        symbol: str | None = None,
    ) -> OrderStatus | None:
        self.lookups += 1
        replies = self._replies[client_order_id]
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        await asyncio.sleep(0)
        return reply

    async def cancel_order(self, order_id: str, symbol: str | None = None) -> OrderResult:
        self.cancelled.append((order_id, symbol))
        tag = order_id.removeprefix("bybit-")
        if self.after_cancel is not None:
            self._replies[tag] = list(self.after_cancel(self._orders[tag]))  # type: ignore[operator]
        return OrderResult(order_id=order_id, order=None, error=self.cancel_error)

    async def cancel_all_orders(self, symbol: str | None = None) -> OrderResult:
        return OrderResult(order_id=None, order=None, error=None)

    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        return []

    async def position(self, symbol: str) -> Position | None:
        raise AssertionError("the consolidator must not read broker positions")

    async def aclose(self) -> None:
        pass


class _LegacyBroker(_Broker):
    """A broker without order_status (like paper / Lighter)."""

    def __getattribute__(self, name: str) -> object:
        if name == "order_status":
            raise AttributeError(name)
        return super().__getattribute__(name)

    async def place_order(self, order: Order) -> OrderResult:
        self.placed.append(order)
        return OrderResult(
            order_id="paper-1", order=order, error=None, fill_price=Decimal("99")
        )


class _Guard:
    def __init__(self, halted: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.halted = halted
        self.fills: list[tuple[str, str, str, Decimal, Decimal]] = []

    def is_active(self, strategy_id: str) -> bool:
        return strategy_id not in self.halted

    def record_fill(
        self, sid: str, symbol: str, exchange: str, qty: Decimal, price: Decimal
    ) -> None:
        self.fills.append((sid, symbol, exchange, qty, price))


def _lot_factory(
    symbol: str, side: OrderSide, quantity: Decimal, _price: Decimal, _ex: str = ""
) -> Order:
    return Order(
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity.quantize(Decimal("0.01"), rounding=ROUND_DOWN),
        price=Decimal(0),
    )


class _Setup:
    def __init__(
        self,
        tmp_path: Path,
        broker: BaseBroker | None = None,
        guard: _Guard | None = None,
        **brokers: BaseBroker,
    ) -> None:
        self.broker = broker or _Broker()
        self.guard = guard or _Guard()
        self.ledger = LedgerDB(tmp_path / "ledger.db")
        self.targets: asyncio.Queue[TargetPosition] = asyncio.Queue()
        self.fills: asyncio.Queue[FillConfirmation] = asyncio.Queue()
        self.placed: asyncio.Queue[tuple[str, Order]] = asyncio.Queue()
        self.consolidator = OrderConsolidator(
            target_queue=self.targets,
            brokers={"bybit_demo": self.broker, **brokers},
            default_exchange="bybit_demo",
            placed_queue=self.placed,
            fills_queue=self.fills,
            order_factory=_lot_factory,
            guard_registry=self.guard,  # type: ignore[arg-type]
            ledger=self.ledger,
            fill_timeout=0.05,
            poll_interval=0.005,
            max_poll_interval=0.01,
            cancel_timeout=0.02,
        )

    def signal(
        self, qty: str, sid: str = "alpha", symbol: str = "ETHUSDT", exchange: str = ""
    ) -> None:
        asyncio.run(self.consolidator._reconcile(self._target(qty, sid, symbol, exchange)))

    @staticmethod
    def _target(qty: str, sid: str, symbol: str = "ETHUSDT", exchange: str = "") -> TargetPosition:
        return TargetPosition(
            symbol=symbol,
            quantity=Decimal(qty),
            price=Decimal("2400"),
            strategy_id=sid,
            exchange=exchange,
        )

    def fill(self) -> FillConfirmation:
        return self.fills.get_nowait()


# --------------------------------------------------------------- happy path


def test_filled_order_reports_real_qty_price_and_fees(tmp_path: Path) -> None:
    s = _Setup(tmp_path)
    s.signal("-0.5")

    fill = s.fill()
    assert fill.status is FillStatus.FILLED
    assert fill.quantity == Decimal("-0.50")
    assert fill.fill_price == Decimal("2500")  # Bybit's price, not the signal's 2400
    assert fill.fees == {"USDT": Decimal("0.1")}
    assert fill.order_link_id == "1_1"
    assert fill.order_id == "bybit-1_1"
    assert s.guard.fills == [("alpha", "ETHUSDT", "bybit_demo", Decimal("-0.50"), Decimal("2500"))]

    exchange, placed = s.placed.get_nowait()
    assert (exchange, placed.quantity, placed.price) == ("bybit_demo", Decimal("0.50"), Decimal("2500"))


def test_waits_until_the_order_is_final(tmp_path: Path) -> None:
    def script(order: Order) -> list[Reply]:
        return [
            _status(order, OrderState.OPEN, "0", price=None),
            _status(order, OrderState.OPEN, "0", price=None),
            _status(order, OrderState.FILLED, "0.5"),
        ]

    broker = _Broker(script)
    s = _Setup(tmp_path, broker)
    s.consolidator._fill_timeout = 1.0  # not about the deadline: Windows timers tick ~15 ms
    s.signal("0.5")

    assert s.fill().status is FillStatus.FILLED
    assert broker.lookups == 3


# ------------------------------------------------------------- not filled


def test_partial_ioc_fill_reports_only_what_filled(tmp_path: Path) -> None:
    broker = _Broker(lambda o: [_status(o, OrderState.CANCELLED, "0.2", raw="Cancelled")])
    s = _Setup(tmp_path, broker)
    s.signal("-0.5")

    fill = s.fill()
    assert fill.status is FillStatus.PARTIAL
    assert fill.quantity == Decimal("-0.2")
    assert fill.requested_qty == Decimal("-0.50")
    assert s.guard.fills[0][3] == Decimal("-0.2")
    assert s.placed.get_nowait()[1].quantity == Decimal("0.2")


def test_refused_order_is_rejected_without_lookup(tmp_path: Path) -> None:
    broker = _Broker(place_error="ab not enough for new order")
    s = _Setup(tmp_path, broker)
    s.signal("0.5")

    fill = s.fill()
    assert fill.status is FillStatus.REJECTED
    assert fill.quantity == 0
    assert fill.reason == "ab not enough for new order"
    assert broker.lookups == 0
    assert s.guard.fills == []
    assert s.placed.empty()


def test_accepted_then_rejected_reports_zero(tmp_path: Path) -> None:
    broker = _Broker(
        lambda o: [
            _status(o, OrderState.REJECTED, "0", price=None, fees={},
                    reason="EC_NoImmediateQtyToFill")
        ]
    )
    s = _Setup(tmp_path, broker)
    s.signal("0.5")

    fill = s.fill()
    assert fill.status is FillStatus.REJECTED
    assert fill.quantity == 0
    assert fill.reason == "EC_NoImmediateQtyToFill"
    assert s.guard.fills == []
    assert s.placed.empty()


def test_quantity_below_lot_size_is_dropped(tmp_path: Path) -> None:
    broker = _Broker()
    s = _Setup(tmp_path, broker)
    s.signal("0.004")

    fill = s.fill()
    assert fill.status is FillStatus.DROPPED
    assert fill.quantity == 0
    assert broker.placed == []
    # no tag was used up
    s.signal("0.5")
    assert broker.placed[0].client_order_id == "1_1"


def test_halted_strategy_places_nothing_but_is_told(tmp_path: Path) -> None:
    broker = _Broker()
    s = _Setup(tmp_path, broker, _Guard(halted={"alpha"}))
    s.signal("-0.5")

    assert broker.placed == []
    fill = s.fill()
    assert (fill.status, fill.quantity, fill.requested_qty) == (
        FillStatus.BLOCKED, Decimal(0), Decimal("-0.5")
    )
    assert fill.reason == "guard tripped - signal not sent"  # this fake keeps no details
    assert fill.order_link_id is None
    assert s.ledger.trades("alpha") == []  # no order, no tag used up
    assert s.guard.fills == []


# ------------------------------------------------- lost / failed requests


def test_request_lost_in_transit_but_order_filled(tmp_path: Path) -> None:
    broker = _Broker(place_error="ReadTimeout", maybe_placed=True)
    s = _Setup(tmp_path, broker)
    s.signal("0.5")

    fill = s.fill()
    assert fill.status is FillStatus.FILLED
    assert fill.quantity == Decimal("0.5")
    assert s.guard.fills[0][3] == Decimal("0.5")


def test_request_lost_in_transit_and_order_never_arrived(tmp_path: Path) -> None:
    broker = _Broker(lambda o: [None], place_error="ConnectError", maybe_placed=True)
    s = _Setup(tmp_path, broker)
    s.signal("0.5")

    fill = s.fill()
    assert fill.status is FillStatus.REJECTED
    assert fill.quantity == 0
    assert fill.reason is not None
    assert fill.reason.startswith("request lost in transit (ConnectError)")
    assert broker.lookups > 1  # kept checking until the timeout


def test_accepted_order_that_shows_up_late_is_filled(tmp_path: Path) -> None:
    broker = _Broker(lambda o: [None, None, None, *_filled(o)])
    s = _Setup(tmp_path, broker)
    s.consolidator._fill_timeout = 1.0  # not about the deadline: Windows timers tick ~15 ms
    s.signal("0.5")

    fill = s.fill()
    assert fill.status is FillStatus.FILLED
    assert fill.quantity == Decimal("0.50")


def test_accepted_order_never_found_is_unknown_not_rejected(tmp_path: Path) -> None:
    broker = _Broker(lambda o: [None])
    s = _Setup(tmp_path, broker)
    s.signal("0.5")

    fill = s.fill()
    assert fill.status is FillStatus.UNKNOWN
    assert fill.quantity == 0
    assert fill.order_id == "bybit-1_1"
    assert "accepted but not found" in (fill.reason or "")
    assert s.guard.fills == []


def test_lookup_errors_are_retried(tmp_path: Path) -> None:
    broker = _Broker(
        lambda o: [RuntimeError("502"), RuntimeError("502"), *_filled(o)]
    )
    s = _Setup(tmp_path, broker)
    s.consolidator._fill_timeout = 1.0  # not about the deadline: Windows timers tick ~15 ms
    s.signal("0.5")

    assert s.fill().status is FillStatus.FILLED


def test_exchange_never_answers_is_unknown_not_rejected(tmp_path: Path) -> None:
    broker = _Broker(lambda o: [RuntimeError("502")])
    s = _Setup(tmp_path, broker)
    s.signal("0.5")

    fill = s.fill()
    assert fill.status is FillStatus.UNKNOWN
    assert fill.quantity == 0
    assert s.guard.fills == []


def test_still_open_after_timeout_and_cancel_reports_what_filled_so_far(
    tmp_path: Path,
) -> None:
    broker = _Broker(lambda o: [_status(o, OrderState.PARTIALLY_FILLED, "0.1")])
    s = _Setup(tmp_path, broker)
    s.signal("0.5")

    fill = s.fill()
    assert fill.status is FillStatus.PARTIAL
    assert fill.quantity == Decimal("0.1")
    assert "after timeout" in (fill.reason or "")
    assert broker.cancelled == [("bybit-1_1", "ETHUSDT")]


def test_still_open_after_timeout_is_cancelled_and_final_fill_reported(
    tmp_path: Path,
) -> None:
    broker = _Broker(
        lambda o: [_status(o, OrderState.PARTIALLY_FILLED, "0.1")],
        after_cancel=lambda o: [_status(o, OrderState.CANCELLED, "0.3", raw="Cancelled")],
    )
    s = _Setup(tmp_path, broker)
    s.signal("0.5")

    fill = s.fill()
    assert fill.status is FillStatus.PARTIAL
    assert fill.quantity == Decimal("0.3")  # the final amount, not the 0.1 seen before
    assert s.guard.fills[0][3] == Decimal("0.3")
    assert broker.cancelled == [("bybit-1_1", "ETHUSDT")]


def test_cancel_too_late_because_it_filled_reports_filled(tmp_path: Path) -> None:
    broker = _Broker(
        lambda o: [_status(o, OrderState.OPEN, "0", price=None)],
        after_cancel=_filled,
        cancel_error="order not exists or too late to cancel",
    )
    s = _Setup(tmp_path, broker)
    s.signal("0.5")

    fill = s.fill()
    assert fill.status is FillStatus.FILLED
    assert fill.quantity == Decimal("0.50")


def test_polling_slows_down_to_the_maximum_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def recording_sleep(delay: float) -> None:
        if delay > 0:
            delays.append(delay)
        await real_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    broker = _Broker(lambda o: [_status(o, OrderState.OPEN, "0", price=None)])
    s = _Setup(tmp_path, broker)
    s.consolidator._fill_timeout = 0.2
    s.consolidator._cancel_timeout = 0.0
    s.signal("0.5")

    assert delays[0] == pytest.approx(0.005)
    assert delays == sorted(delays)
    assert max(delays) == pytest.approx(0.01)


# ------------------------------------------------------------------- spot


def test_spot_buy_reports_coin_received_after_base_fee(tmp_path: Path) -> None:
    spot = _Broker(
        lambda o: [
            _status(o, OrderState.FILLED, "1.00", fees={"ETH": Decimal("0.001")})
        ]
    )
    s = _Setup(tmp_path, bybit_spot=spot)
    s.signal("1", exchange="bybit_spot")

    fill = s.fill()
    assert fill.exchange == "bybit_spot"
    assert fill.quantity == Decimal("0.999")
    assert fill.fees == {"ETH": Decimal("0.001")}


def test_partial_spot_buy_reports_coin_received_after_base_fee(tmp_path: Path) -> None:
    spot = _Broker(
        lambda o: [
            _status(o, OrderState.CANCELLED, "0.50", fees={"ETH": Decimal("0.0005")},
                    raw="PartiallyFilledCanceled")
        ]
    )
    s = _Setup(tmp_path, bybit_spot=spot)
    s.signal("1", exchange="bybit_spot")

    fill = s.fill()
    assert fill.status is FillStatus.PARTIAL
    assert fill.quantity == Decimal("0.4995")
    assert fill.requested_qty == Decimal("1.00")
    assert s.guard.fills[0][3] == Decimal("0.4995")


# ------------------------------------------------------------- no netting


def test_opposite_signals_from_two_strategies_are_two_orders(tmp_path: Path) -> None:
    broker = _Broker()
    s = _Setup(tmp_path, broker)
    s.signal("0.5", sid="alpha")
    s.signal("-0.5", sid="beta")

    assert [(o.side, o.quantity, o.client_order_id) for o in broker.placed] == [
        (OrderSide.BUY, Decimal("0.50"), "1_1"),
        (OrderSide.SELL, Decimal("0.50"), "2_1"),
    ]
    assert [s.fill().quantity, s.fill().quantity] == [Decimal("0.50"), Decimal("-0.50")]


def test_broker_without_order_status_keeps_old_behaviour(tmp_path: Path) -> None:
    s = _Setup(tmp_path, _LegacyBroker())
    s.signal("0.5")

    fill = s.fill()
    assert fill.status is FillStatus.FILLED
    assert fill.quantity == Decimal("0.50")
    assert fill.fill_price == Decimal("99")


# ------------------------------------------------------------------ lanes


def test_slow_order_does_not_block_other_strategies(tmp_path: Path) -> None:
    """alpha's order stays open for a while; beta's signal, sent after it,
    must be filled and reported first."""
    release = asyncio.Event()

    class _SlowForAlpha(_Broker):
        async def order_status(self, order_id=None, client_order_id=None, symbol=None):  # type: ignore[no-untyped-def]
            if client_order_id and client_order_id.startswith("1_"):
                await release.wait()
            return await super().order_status(order_id, client_order_id, symbol)

    broker = _SlowForAlpha()
    s = _Setup(tmp_path, broker)
    s.consolidator._fill_timeout = 5.0

    async def _run() -> list[str]:
        task = asyncio.create_task(s.consolidator.run())
        s.targets.put_nowait(s._target("0.5", "alpha"))
        s.targets.put_nowait(s._target("0.5", "beta"))
        order = [(await asyncio.wait_for(s.fills.get(), 1)).strategy_id]
        release.set()
        order.append((await asyncio.wait_for(s.fills.get(), 1)).strategy_id)
        task.cancel()
        return order

    assert asyncio.run(_run()) == ["beta", "alpha"]


def test_one_strategys_signals_stay_in_order(tmp_path: Path) -> None:
    def script(order: Order) -> list[Reply]:
        # the first order is slower than the second
        slow = order.client_order_id == "1_1"
        return [_status(order, OrderState.OPEN, "0", price=None)] * (5 if slow else 0) + _filled(order)

    s = _Setup(tmp_path, _Broker(script))

    async def _run() -> list[str | None]:
        task = asyncio.create_task(s.consolidator.run())
        s.targets.put_nowait(s._target("0.5", "alpha"))
        s.targets.put_nowait(s._target("-0.5", "alpha"))
        got = [(await asyncio.wait_for(s.fills.get(), 1)).order_link_id for _ in range(2)]
        task.cancel()
        return got

    assert asyncio.run(_run()) == ["1_1", "1_2"]


def test_a_crashing_signal_does_not_kill_the_lane(tmp_path: Path) -> None:
    broker = _Broker()
    s = _Setup(tmp_path, broker)
    calls = 0
    real_place = broker.place_order

    async def flaky(order: Order) -> OrderResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("boom")
        return await real_place(order)

    broker.place_order = flaky  # type: ignore[method-assign]

    async def _run() -> FillConfirmation:
        task = asyncio.create_task(s.consolidator.run())
        s.targets.put_nowait(s._target("0.5", "alpha"))
        s.targets.put_nowait(s._target("0.5", "alpha"))
        fill = await asyncio.wait_for(s.fills.get(), 1)
        task.cancel()
        return fill

    assert asyncio.run(_run()).status is FillStatus.FILLED


# ----------------------------------------------------------------- ledger


def test_trade_is_logged_pending_before_the_order_is_sent(tmp_path: Path) -> None:
    seen: list[str] = []

    class _Checking(_Broker):
        async def place_order(self, order: Order) -> OrderResult:
            seen.extend(t.status for t in s.ledger.trades("alpha"))
            return await super().place_order(order)

    s = _Setup(tmp_path, _Checking())
    s.signal("0.5")

    assert seen == ["pending"]


def test_filled_order_is_logged_and_updates_holdings(tmp_path: Path) -> None:
    s = _Setup(tmp_path)
    s.signal("-0.5")

    (trade,) = s.ledger.trades("alpha")
    assert (trade.status, trade.order_link_id, trade.order_id) == ("filled", "1_1", "bybit-1_1")
    assert (trade.side, trade.requested_qty, trade.filled_qty) == (
        OrderSide.SELL, Decimal("0.50"), Decimal("-0.50")
    )
    (holding,) = s.ledger.holdings("alpha")
    assert (holding.quantity, holding.avg_entry_price) == (Decimal("-0.50"), Decimal("2500"))
    assert holding.fees_paid == Decimal("0.1")


def test_ledger_is_written_before_the_strategy_is_told(tmp_path: Path) -> None:
    s = _Setup(tmp_path)
    statuses: list[str] = []

    class _Watching(asyncio.Queue[FillConfirmation]):
        def put_nowait(self, item: FillConfirmation) -> None:
            statuses.extend(t.status for t in s.ledger.trades("alpha"))
            super().put_nowait(item)

    s.consolidator._fills_queue = _Watching()
    s.signal("0.5")

    assert statuses == ["filled"]


def test_refused_order_is_logged_rejected(tmp_path: Path) -> None:
    s = _Setup(tmp_path, _Broker(place_error="ab not enough for new order"))
    s.signal("0.5")

    (trade,) = s.ledger.trades("alpha")
    assert (trade.status, trade.reason) == ("rejected", "ab not enough for new order")
    assert s.ledger.holdings("alpha") == []


def test_dropped_signal_is_not_in_the_trade_log(tmp_path: Path) -> None:
    s = _Setup(tmp_path)
    s.signal("0.004")

    assert s.ledger.trades("alpha") == []


def test_ledger_failure_still_reports_to_the_strategy(tmp_path: Path) -> None:
    s = _Setup(tmp_path)

    def broken(_fill: FillConfirmation) -> bool:
        raise RuntimeError("disk full")

    s.ledger.record_result = broken  # type: ignore[method-assign,assignment]
    s.signal("0.5")

    assert s.fill().status is FillStatus.FILLED


# --------------------------------------------------------------- recovery


def _leftover(s: _Setup, order_id: str | None = None) -> Order:
    """A trade a crashed run left pending: logged, maybe accepted, never finished."""
    tag = s.ledger.next_order_link_id("alpha")
    s.ledger.record_pending(tag, "alpha", "bybit_demo", "ETHUSDT", OrderSide.BUY, Decimal("0.5"))
    if order_id:
        s.ledger.record_accepted(tag, order_id)
    return Order(
        symbol="ETHUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.5"),
        price=Decimal(0),
        client_order_id=tag,
    )


def test_recovery_records_a_fill_that_happened_during_the_crash(tmp_path: Path) -> None:
    s = _Setup(tmp_path)
    order = _leftover(s)
    s.broker._replies[order.client_order_id] = _filled(order)  # type: ignore[attr-defined]

    asyncio.run(s.consolidator.recover_unfinished())

    assert s.ledger.trades("alpha")[0].status == "filled"
    assert s.ledger.holdings("alpha")[0].quantity == Decimal("0.5")
    assert s.fills.empty()  # strategies are not told


def test_recovery_rejects_an_order_that_never_reached_the_exchange(tmp_path: Path) -> None:
    s = _Setup(tmp_path)
    order = _leftover(s)
    s.broker._replies[order.client_order_id] = [None]  # type: ignore[attr-defined]

    asyncio.run(s.consolidator.recover_unfinished())

    (trade,) = s.ledger.trades("alpha")
    assert trade.status == "rejected"
    assert "never reached it" in (trade.reason or "")


def test_recovery_keeps_an_accepted_but_missing_order_unknown(tmp_path: Path) -> None:
    s = _Setup(tmp_path)
    order = _leftover(s, order_id="bybit-x")
    s.broker._replies[order.client_order_id] = [None]  # type: ignore[attr-defined]

    asyncio.run(s.consolidator.recover_unfinished())

    (trade,) = s.ledger.trades("alpha")
    assert trade.status == "unknown"
    assert [t.order_link_id for t in s.ledger.unfinished_trades()] == [order.client_order_id]


def test_recovery_leaves_trade_pending_when_exchange_unreachable(tmp_path: Path) -> None:
    s = _Setup(tmp_path)
    order = _leftover(s)
    s.broker._replies[order.client_order_id] = [RuntimeError("502")]  # type: ignore[attr-defined]

    asyncio.run(s.consolidator.recover_unfinished())

    assert s.ledger.trades("alpha")[0].status == "pending"


# ------------------------------------------------------------------ codec


def test_blocked_report_survives_the_codec() -> None:
    fill = FillConfirmation(
        strategy_id="alpha", symbol="ETHUSDT", quantity=Decimal(0), fill_price=Decimal(0),
        status=FillStatus.BLOCKED, requested_qty=Decimal(1), reason="guard tripped - signal not sent",
    )
    assert codec.decode_fill(codec.encode_fill(fill)) == fill


def test_fill_codec_round_trip() -> None:
    fill = FillConfirmation(
        strategy_id="alpha",
        symbol="ETHUSDT",
        quantity=Decimal("-0.2"),
        fill_price=Decimal("2500.5"),
        exchange="bybit_demo",
        status=FillStatus.PARTIAL,
        requested_qty=Decimal("-0.5"),
        fees={"USDT": Decimal("0.13")},
        order_id="abc",
        order_link_id="1_7",
        reason="order Cancelled, 0.2 of 0.5 filled",
    )
    assert codec.decode_fill(codec.encode_fill(fill)) == fill


def test_old_fill_messages_still_decode() -> None:
    old = b'{"strategy_id":"a","symbol":"ETHUSDT","quantity":"1","fill_price":"2","exchange":""}'
    fill = codec.decode_fill(old)
    assert fill.status is FillStatus.FILLED
    assert fill.quantity == Decimal("1")
    assert fill.fees == {}
    assert fill.requested_qty is None
    assert fill.order_link_id is None
    assert fill.reason is None
