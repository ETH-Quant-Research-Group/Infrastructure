from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from engine.order.consolidator import OrderConsolidator
from engine.order.instruments import (
    InstrumentCache,
    OrderSizing,
    check_order,
    round_down,
)
from engine.order.ledger import LedgerDB
from execution.brokers._bybit_common import parse_instrument_rules
from execution.brokers.bybit import BybitBroker
from execution.types import (
    FillConfirmation,
    FillStatus,
    InstrumentRules,
    Order,
    OrderSide,
    OrderType,
)
from tests.fakes import FakeBroker
from interfaces.signals import TargetPosition
from workers import consolidator_worker, strategy_worker

if TYPE_CHECKING:
    from pathlib import Path

_LINEAR = {
    "symbol": "SOLUSDT", "status": "Trading",
    "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1", "maxOrderQty": "79770",
                      "maxMktOrderQty": "5000", "minNotionalValue": "5"},
}
_SPOT = {
    "symbol": "SOLUSDT", "status": "Trading",
    "lotSizeFilter": {"basePrecision": "0.001", "minOrderQty": "0.011", "maxOrderQty": "2700",
                      "minOrderAmt": "1", "maxOrderAmt": "200000"},
}


def _sol(step: str = "0.1", min_qty: str | None = "0.1", max_qty: str | None = "5000",
         min_notional: str | None = "5", trading: bool = True) -> InstrumentRules:
    return InstrumentRules(
        symbol="SOLUSDT", qty_step=Decimal(step),
        min_qty=Decimal(min_qty) if min_qty else None,
        max_market_qty=Decimal(max_qty) if max_qty else None,
        min_notional=Decimal(min_notional) if min_notional else None,
        trading=trading,
    )


# ------------------------------------------------------------------ parsing


def test_linear_and_spot_rules_are_parsed() -> None:
    assert parse_instrument_rules(_LINEAR) == _sol()
    assert parse_instrument_rules(_SPOT) == InstrumentRules(
        symbol="SOLUSDT", qty_step=Decimal("0.001"), min_qty=Decimal("0.011"),
        max_market_qty=Decimal("2700"), min_notional=Decimal("1"),
    )


def test_no_step_means_no_rules_and_status_is_kept() -> None:
    assert parse_instrument_rules({"symbol": "X", "lotSizeFilter": {}}) is None
    halted = parse_instrument_rules({**_LINEAR, "status": "PreLaunch"})
    assert halted is not None and not halted.trading


def test_broker_fetches_every_page_of_rules() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        first = "cursor" not in request.url.params
        item = _LINEAR if first else {**_LINEAR, "symbol": "DOGEUSDT"}
        page: dict[str, Any] = {"list": [item]}
        if first:
            page["nextPageCursor"] = "p2"
        return httpx.Response(200, json={"retCode": 0, "result": page})

    broker = BybitBroker(api_key="k", api_secret="s", demo=True)
    broker._client = httpx.AsyncClient(
        base_url="https://api-demo.bybit.com", transport=httpx.MockTransport(handler)
    )
    rules = asyncio.run(broker.instrument_rules())

    assert [r.symbol for r in rules] == ["SOLUSDT", "DOGEUSDT"]
    assert seen[0].url.path == "/v5/market/instruments-info"
    assert seen[0].url.params["category"] == "linear"
    assert seen[1].url.params["cursor"] == "p2"


# ----------------------------------------------------------------- the rules


@pytest.mark.parametrize(("qty", "step", "expected"), [
    ("1.239", "0.01", "1.23"), ("17", "5", "15"), ("-0.37", "0.1", "-0.3"), ("0.04", "0.1", "0"),
])
def test_round_down(qty: str, step: str, expected: str) -> None:
    assert round_down(Decimal(qty), Decimal(step)) == Decimal(expected)


@pytest.mark.parametrize(("rules", "qty", "price", "reason"), [
    (None, "1", "100", "no order-size rules known for SOLUSDT"),
    (_sol(trading=False), "1", "100", "SOLUSDT is not trading right now"),
    (_sol(), "0.05", "100", "quantity 0.05 below the minimum order size 0.1 for SOLUSDT"),
    (_sol(), "6000", "100", "quantity 6000 above the largest market order 5000 for SOLUSDT"),
    (_sol(), "0.1", "20", "order value 2.0 below the minimum 5 for SOLUSDT"),
    (_sol(), "0.1", "0", None),  # no price: value not checked
    (_sol(), "1", "100", None),
])
def test_check_order(rules: InstrumentRules | None, qty: str, price: str, reason: str | None) -> None:
    assert check_order(rules, "SOLUSDT", Decimal(qty), Decimal(price)) == reason


# -------------------------------------------------------------------- cache


class _RulesBroker:
    def __init__(self, rules: list[InstrumentRules] | Exception) -> None:
        self.rules = rules
        self.calls = 0

    async def instrument_rules(self) -> list[InstrumentRules]:
        self.calls += 1
        if isinstance(self.rules, Exception):
            raise self.rules
        return self.rules


def test_cache_loads_once_and_answers_from_memory() -> None:
    broker = _RulesBroker([_sol()])
    cache = InstrumentCache()

    assert asyncio.run(cache.load({"bybit_demo": broker})) == {"bybit_demo": 1}
    for _ in range(100):
        cache.check("bybit_demo", _order("1"), Decimal(100))
        cache.quantize("bybit_demo", "SOLUSDT", Decimal("1.27"))

    assert broker.calls == 1
    assert cache.quantize("bybit_demo", "SOLUSDT", Decimal("1.27")) == Decimal("1.2")


def test_fallback_steps_until_the_real_rules_load() -> None:
    cache = InstrumentCache(fallback_steps={"bybit_demo": {"ETHUSDT": Decimal("0.01")}})
    asyncio.run(cache.load({"bybit_demo": _RulesBroker(RuntimeError("down"))}))

    assert not cache.loaded
    assert cache.quantize("bybit_demo", "ETHUSDT", Decimal("0.017")) == Decimal("0.01")
    assert cache.check("bybit_demo", _order("0.01", "ETHUSDT"), Decimal(2500)) is None
    assert cache.check("bybit_demo", _order("1"), Decimal(100)) == "no order-size rules known for SOLUSDT"
    assert cache.quantize("bybit_demo", "SOLUSDT", Decimal("1.27")) == Decimal("1.27")  # unknown


def test_failed_refresh_keeps_the_previous_rules() -> None:
    cache = InstrumentCache()
    asyncio.run(cache.load({"bybit_demo": _RulesBroker([_sol()])}))
    asyncio.run(cache.load({"bybit_demo": _RulesBroker(RuntimeError("down"))}))

    assert cache.rules("bybit_demo", "SOLUSDT") == _sol()


def _order(qty: str, symbol: str = "SOLUSDT") -> Order:
    return Order(symbol=symbol, side=OrderSide.BUY, order_type=OrderType.MARKET,
                 quantity=Decimal(qty), price=Decimal(0))


# --------------------------------------------------------- in the consolidator


_Broker = FakeBroker  # fills everything at 100


def _consolidator(tmp_path: Path, cache: InstrumentCache) -> tuple[OrderConsolidator, _Broker, LedgerDB, asyncio.Queue[FillConfirmation]]:
    broker = _Broker()
    ledger = LedgerDB(tmp_path / "ledger.db")
    fills: asyncio.Queue[FillConfirmation] = asyncio.Queue()

    def factory(symbol: str, side: OrderSide, qty: Decimal, _p: Decimal, ex: str = "") -> Order:
        return Order(symbol=symbol, side=side, order_type=OrderType.MARKET,
                     quantity=cache.quantize(ex, symbol, qty), price=Decimal(0))

    consolidator = OrderConsolidator(
        target_queue=asyncio.Queue(), brokers={"bybit_demo": broker},
        default_exchange="bybit_demo", fills_queue=fills, order_factory=factory,
        ledger=ledger, order_check=cache.check,
    )
    return consolidator, broker, ledger, fills


def _signal(consolidator: OrderConsolidator, qty: str, symbol: str = "SOLUSDT") -> None:
    asyncio.run(consolidator._reconcile(TargetPosition(
        symbol=symbol, quantity=Decimal(qty), price=Decimal(100), strategy_id="alpha"
    )))


def test_order_for_an_unknown_symbol_is_dropped_not_sent(tmp_path: Path) -> None:
    consolidator, broker, ledger, fills = _consolidator(tmp_path, InstrumentCache())
    _signal(consolidator, "1")

    fill = fills.get_nowait()
    assert (fill.status, fill.quantity) == (FillStatus.DROPPED, Decimal(0))
    assert fill.reason == "no order-size rules known for SOLUSDT"
    assert broker.placed == []
    assert ledger.trades("alpha") == []  # no tag used up


def test_orders_are_rounded_to_the_real_step_and_checked(tmp_path: Path) -> None:
    cache = InstrumentCache()
    asyncio.run(cache.load({"bybit_demo": _RulesBroker([_sol()])}))
    consolidator, broker, _, fills = _consolidator(tmp_path, cache)

    _signal(consolidator, "1.27")
    _signal(consolidator, "0.03")  # rounds to 0
    _signal(consolidator, "-6000")  # above the largest market order

    assert [o.quantity for o in broker.placed] == [Decimal("1.2")]
    statuses = [fills.get_nowait() for _ in range(3)]
    assert [f.status for f in statuses] == [FillStatus.FILLED, FillStatus.DROPPED, FillStatus.DROPPED]
    assert "above the largest market order" in (statuses[2].reason or "")


def test_worker_factory_uses_the_cache_not_a_guess() -> None:
    order = consolidator_worker._order_factory(
        "ETHUSDT", OrderSide.BUY, Decimal("0.017"), Decimal(0), "bybit_demo"
    )
    assert order.quantity == Decimal("0.01")  # fallback table: real step
    unknown = consolidator_worker._order_factory(
        "SOLUSDT", OrderSide.BUY, Decimal("1.27"), Decimal(0), "bybit_spot"
    )
    assert unknown.quantity == Decimal("1.27")  # not guessed; then dropped by the check


# ------------------------------------------------------------- for strategies


def test_consolidator_answers_rules_from_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = InstrumentCache()
    asyncio.run(cache.load({"bybit_demo": _RulesBroker([_sol()]),
                            "bybit_spot": _RulesBroker([InstrumentRules(symbol="ETHUSDT", qty_step=Decimal("0.00001"))])}))
    monkeypatch.setattr(consolidator_worker, "INSTRUMENTS", cache)

    reply = consolidator_worker.instruments_reply(b'{"symbols": ["SOLUSDT"]}')
    assert [(r["exchange"], r["symbol"], r["qty_step"]) for r in reply["rules"]] == [
        ("bybit_demo", "SOLUSDT", "0.1")
    ]
    assert set(reply["loaded_at"]) == {"bybit_demo", "bybit_spot"}
    assert len(consolidator_worker.instruments_reply(b"{}")["rules"]) == 2
    assert "error" in consolidator_worker.instruments_reply(b"not json")


def test_strategy_order_sizing_is_local_and_matches_the_consolidator() -> None:
    from engine.order.instruments import rules_to_dict

    sizing = OrderSizing.from_dicts([rules_to_dict("bybit_demo", _sol())])

    assert sizing.round_qty("bybit_demo", "SOLUSDT", Decimal("1.27")) == Decimal("1.2")
    assert sizing.round_qty("bybit_demo", "SOLUSDT", Decimal("-1.27")) == Decimal("-1.2")
    assert sizing.round_qty("bybit_demo", "SOLUSDT", Decimal("0.05")) == 0  # below min
    assert sizing.round_qty("bybit_spot", "SOLUSDT", Decimal("1")) == 0  # unknown there
    assert sizing.check("bybit_demo", "SOLUSDT", Decimal("1.27"), Decimal(100)) is None
    assert sizing.check("bybit_demo", "SOLUSDT", Decimal("0.11"), Decimal(20)) == (
        "order value 2.0 below the minimum 5 for SOLUSDT"  # checked after rounding to 0.1
    )
    assert sizing.rules("bybit_demo", "SOLUSDT") == _sol()


def test_strategy_worker_fills_order_sizing_from_the_consolidator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = InstrumentCache()
    asyncio.run(cache.load({"bybit_demo": _RulesBroker([_sol()])}))
    monkeypatch.setattr(consolidator_worker, "INSTRUMENTS", cache)
    asked: list[dict[str, Any]] = []

    class _NC:
        async def request(self, subject: str, data: bytes, timeout: float) -> SimpleNamespace:
            asked.append(json.loads(data))
            return SimpleNamespace(data=json.dumps(consolidator_worker.instruments_reply(data)).encode())

    sizing = OrderSizing()
    symbols = strategy_worker._topic_symbols(
        ["futures.SOLUSDT.funding_rate", "futures.SOLUSDT.bars.8h", "futures.ETHUSDT.bars.1m"]
    )
    assert asyncio.run(strategy_worker._fetch_order_sizing(_NC(), symbols, sizing))  # type: ignore[arg-type]

    assert asked == [{"symbols": ["ETHUSDT", "SOLUSDT"]}]
    assert sizing.round_qty("bybit_demo", "SOLUSDT", Decimal("1.27")) == Decimal("1.2")


def test_strategy_keeps_its_rules_if_the_consolidator_does_not_answer() -> None:
    from engine.order.instruments import rules_to_dict

    class _NC:
        async def request(self, subject: str, data: bytes, timeout: float) -> None:
            raise TimeoutError("no responders")

    sizing = OrderSizing.from_dicts([rules_to_dict("bybit_demo", _sol())])
    assert not asyncio.run(strategy_worker._fetch_order_sizing(_NC(), ["SOLUSDT"], sizing))  # type: ignore[arg-type]
    assert len(sizing) == 1
