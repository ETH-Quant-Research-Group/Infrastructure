from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from engine.order.ledger import LedgerDB
from engine.order.performance import PnLHistoryDB, PnLPoint
from execution.types import FillConfirmation, FillStatus, OrderSide
from workers.strategy_info import (
    StrategyInfoWorker,
    UnknownStrategyError,
    handle_add,
    handle_get,
)

if TYPE_CHECKING:
    from pathlib import Path


def _trade(ledger: LedgerDB, strategy: str, qty: str, price: str,
           exchange: str = "bybit_demo", symbol: str = "ETHUSDT") -> str:
    side = OrderSide.BUY if Decimal(qty) > 0 else OrderSide.SELL
    tag = ledger.begin_order(strategy, exchange, symbol, side, abs(Decimal(qty)))
    ledger.record_result(FillConfirmation(
        strategy_id=strategy, symbol=symbol, quantity=Decimal(qty),
        fill_price=Decimal(price), exchange=exchange, status=FillStatus.FILLED,
        fees={"USDT": Decimal("0.5")}, order_link_id=tag,
    ))
    return tag


class _Prices:
    def __init__(self, prices: dict[tuple[str, str], str | None]) -> None:
        self.prices = prices
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, exchange: str, symbol: str) -> Decimal | None:
        self.calls.append((exchange, symbol))
        raw = self.prices.get((exchange, symbol))
        return Decimal(raw) if raw else None


def _setup(tmp_path: Path) -> tuple[LedgerDB, LedgerDB]:
    """The consolidator's ledger (writer) and the worker's read-only copy."""
    live = LedgerDB(tmp_path / "ledger.db")
    _trade(live, "alpha", "2", "100")          # alpha = 1
    _trade(live, "beta", "-1", "50")           # beta = 2
    _trade(live, "gamma", "1", "10")           # gamma = 3
    return live, LedgerDB(tmp_path / "ledger.db", readonly=True)


def _worker(tmp_path: Path, reader: LedgerDB, prices: _Prices | None = None,
            names: tuple[str, ...] = (), ids: tuple[int, ...] = ()) -> StrategyInfoWorker:
    return StrategyInfoWorker(
        reader, prices or _Prices({}), tmp_path / "strategy_info.json", names, ids
    )


# --------------------------------------------------------------- resolving


def test_names_and_ids_resolve_to_ids_each_once(tmp_path: Path) -> None:
    _, reader = _setup(tmp_path)
    worker = _worker(tmp_path, reader, names=("beta", "alpha"), ids=(2, 3))

    assert worker.watched == [2, 1, 3]


def test_unknown_names_and_ids_are_all_reported(tmp_path: Path) -> None:
    _, reader = _setup(tmp_path)

    with pytest.raises(UnknownStrategyError) as err:
        _worker(tmp_path, reader, names=("alpha", "alpah"), ids=(3, 7, 9))

    assert (err.value.names, err.value.ids) == (["alpah"], [7, 9])
    assert str(err.value) == (
        "unknown strategy id 7, unknown strategy id 9, unknown strategy name 'alpah'"
    )


def test_add_is_all_or_nothing(tmp_path: Path) -> None:
    _, reader = _setup(tmp_path)
    worker = _worker(tmp_path, reader, ids=(1,))

    with pytest.raises(UnknownStrategyError):
        worker.add(names=["beta"], ids=[42])
    assert worker.watched == [1]

    assert worker.add(names=["beta"], ids=[1]) == [2]  # 1 already watched
    assert worker.watched == [1, 2]


def test_strategy_registered_after_start_can_be_added(tmp_path: Path) -> None:
    live, reader = _setup(tmp_path)
    worker = _worker(tmp_path, reader)
    _trade(live, "delta", "1", "10")  # the consolidator registers a new one

    assert worker.add(names=["delta"]) == [4]


# -------------------------------------------------------------------- info


def test_info_has_live_unrealized_pnl_and_totals(tmp_path: Path) -> None:
    live, reader = _setup(tmp_path)
    _trade(live, "alpha", "-1", "110")  # realize +10, hold 1 @ 100
    prices = _Prices({("bybit_demo", "ETHUSDT"): "130"})
    worker = _worker(tmp_path, reader, prices)

    (alpha,) = asyncio.run(worker.info([1]))

    assert (alpha["id"], alpha["name"], alpha["orders"]) == (1, "alpha", 2)
    (holding,) = alpha["holdings"]
    assert holding["quantity"] == "1"
    assert holding["price"] == "130"
    assert holding["unrealized_pnl"] == "30"
    assert alpha["totals"] == {
        "realized_pnl": "10", "unrealized_pnl": "30", "funding": "0", "fees_paid": "1.0",
        "net_pnl": "39.0",
    }
    assert alpha["trades_by_status"] == {"filled": 2}
    assert [t["order_link_id"] for t in alpha["last_trades"]] == ["1_2", "1_1"]


def test_short_holding_gains_when_price_falls(tmp_path: Path) -> None:
    _, reader = _setup(tmp_path)
    worker = _worker(tmp_path, reader, _Prices({("bybit_demo", "ETHUSDT"): "40"}))

    (beta,) = asyncio.run(worker.info([2]))

    assert beta["holdings"][0]["unrealized_pnl"] == "10"  # short 1 from 50 to 40


def test_missing_price_makes_unrealized_unknown_not_zero(tmp_path: Path) -> None:
    _, reader = _setup(tmp_path)
    worker = _worker(tmp_path, reader)  # no prices at all

    (alpha,) = asyncio.run(worker.info([1]))

    assert alpha["holdings"][0]["price"] is None
    assert alpha["totals"]["unrealized_pnl"] is None
    assert alpha["totals"]["net_pnl"] is None
    assert alpha["totals"]["realized_pnl"] == "0"


def test_flat_holding_needs_no_price(tmp_path: Path) -> None:
    live, reader = _setup(tmp_path)
    _trade(live, "alpha", "-2", "120")  # closed
    prices = _Prices({})
    worker = _worker(tmp_path, reader, prices)

    (alpha,) = asyncio.run(worker.info([1]))

    assert alpha["totals"]["unrealized_pnl"] == "0"
    assert alpha["totals"]["net_pnl"] == "39.0"  # 40 realized − 1 fees
    assert prices.calls == []


def test_info_is_only_for_the_given_ids(tmp_path: Path) -> None:
    _, reader = _setup(tmp_path)
    worker = _worker(tmp_path, reader, ids=(1, 2, 3))

    assert [s["name"] for s in asyncio.run(worker.info([3, 1]))] == ["gamma", "alpha"]
    with pytest.raises(UnknownStrategyError, match="unknown strategy id 8"):
        asyncio.run(worker.info([1, 8]))


def test_each_price_is_fetched_once_per_round(tmp_path: Path) -> None:
    _, reader = _setup(tmp_path)
    prices = _Prices({("bybit_demo", "ETHUSDT"): "100"})
    worker = _worker(tmp_path, reader, prices, ids=(1, 2, 3))

    asyncio.run(worker.info([1, 2, 3]))

    assert prices.calls == [("bybit_demo", "ETHUSDT")]


# ------------------------------------------------------------------ sharpe


def _point(day: int, net: str | None) -> PnLPoint:
    return PnLPoint(
        strategy="alpha", strategy_num=1,
        taken_at=datetime(2026, 10, day, 0, 0, 5, tzinfo=UTC), orders=1,
        realized_pnl=Decimal(0), unrealized_pnl=None if net is None else Decimal(net),
        fees_paid=Decimal(0), net_pnl=None if net is None else Decimal(net),
    )


def test_sharpe_comes_from_the_pnl_history(tmp_path: Path) -> None:
    _, reader = _setup(tmp_path)
    worker = StrategyInfoWorker(
        reader, _Prices({}), tmp_path / "strategy_info.json", ids=(1,),
        pnl_history=tmp_path / "pnl_history.db",
    )
    assert asyncio.run(worker.info([1]))[0]["sharpe"] == {
        "value": None, "returns": 0, "period_days": 1.0,
    }  # no history file yet

    history = PnLHistoryDB(tmp_path / "pnl_history.db")
    for day, net in ((1, "0"), (2, "10"), (3, "15"), (4, "30")):
        history.record(_point(day, net))

    ratio = asyncio.run(worker.info([1]))[0]["sharpe"]
    assert ratio["returns"] == 3
    # returns 10, 5, 15: mean 10 / population std 4.082 × √365
    assert ratio["value"] == pytest.approx(46.80, abs=0.01)


# -------------------------------------------------------------------- JSON


def test_writes_all_watched_strategies_to_the_json_file(tmp_path: Path) -> None:
    _, reader = _setup(tmp_path)
    worker = _worker(tmp_path, reader, names=("gamma",), ids=(1,))

    asyncio.run(worker.write())

    data = json.loads((tmp_path / "strategy_info.json").read_text())
    assert [s["name"] for s in data["strategies"]] == ["gamma", "alpha"]
    assert "updated_at" in data
    assert not (tmp_path / "strategy_info.json.tmp").exists()


# -------------------------------------------------------- dashboard pnl.*


def test_write_publishes_each_watched_strategys_own_pnl(tmp_path: Path) -> None:
    live, reader = _setup(tmp_path)
    _trade(live, "alpha", "-1", "110")  # alpha: realized 10, fees 1, holds 1 @ 100
    prices = _Prices({("bybit_demo", "ETHUSDT"): "130"})
    worker = _worker(tmp_path, reader, prices, ids=(1, 2))
    sent: list[tuple[str, dict[str, str]]] = []

    async def publish(subject: str, data: bytes) -> None:
        sent.append((subject, json.loads(data)))

    worker.attach(publish)
    asyncio.run(worker.write())

    assert [subject for subject, _ in sent] == ["pnl.alpha", "pnl.beta"]  # watched only
    alpha = sent[0][1]
    assert alpha["strategy_id"] == "alpha"
    assert (alpha["total_realized"], alpha["total_unrealized"], alpha["total"]) == (
        "9.0", "30", "39.0"  # realized + funding − fees; unrealized; net
    )
    assert alpha["timestamp"]


def test_strategy_without_a_price_is_not_published(tmp_path: Path) -> None:
    _, reader = _setup(tmp_path)
    worker = _worker(tmp_path, reader, _Prices({}), ids=(1,))
    sent: list[str] = []

    async def publish(subject: str, data: bytes) -> None:
        sent.append(subject)

    worker.attach(publish)
    asyncio.run(worker.write())

    assert sent == []  # skipped, not sent as 0


def test_container_no_longer_republishes_account_pnl_by_default() -> None:
    from types import SimpleNamespace
    from typing import Any

    from engine.strategy.guard import StrategyGuard
    from workers import strategy_worker

    published: list[str] = []

    class _NC:
        cb: Any = None

        async def subscribe(self, subject: str, cb: Any) -> None:
            self.cb = cb

        async def publish(self, subject: str, data: bytes) -> None:
            published.append(subject)

    nc = _NC()

    async def _run() -> None:
        task = asyncio.create_task(strategy_worker._bridge_broker_pnl_to_strategy(
            nc, "alpha", StrategyGuard(max_loss=Decimal(1000)),  # type: ignore[arg-type]
            feed_guard=False, publish_pnl=strategy_worker._LEGACY_ACCOUNT_PNL,
        ))
        await asyncio.sleep(0)
        await nc.cb(SimpleNamespace(data=json.dumps({"total": "5"}).encode()))
        task.cancel()

    asyncio.run(_run())

    assert strategy_worker._LEGACY_ACCOUNT_PNL is False
    assert published == []


# ------------------------------------------- container PnLCalc seeding


def test_consolidator_answers_with_the_strategys_own_holdings(tmp_path: Path) -> None:
    from workers.consolidator_worker import ledger_holdings_reply

    live, _ = _setup(tmp_path)

    reply = ledger_holdings_reply(live, b'{"strategy": "alpha"}')
    assert reply["strategy"] == "alpha"
    assert [(h["exchange"], h["symbol"], h["quantity"], h["avg_entry_price"])
            for h in reply["holdings"]] == [("bybit_demo", "ETHUSDT", "2", "100")]
    assert ledger_holdings_reply(live, b'{"strategy": "new"}')["holdings"] == []
    assert "error" in ledger_holdings_reply(live, b"{}")


def test_container_seeds_its_pnlcalc_with_its_own_holdings_only(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from engine.strategy.pnl_calc import PnLCalc
    from workers.consolidator_worker import ledger_holdings_reply
    from workers.strategy_worker import _seed_from_ledger

    live, _ = _setup(tmp_path)  # alpha long 2, beta short 1, gamma long 1
    asked: list[bytes] = []

    class _NC:
        async def request(self, subject: str, data: bytes, timeout: float) -> SimpleNamespace:
            asked.append(data)
            return SimpleNamespace(data=json.dumps(ledger_holdings_reply(live, data)).encode())

    pnl = PnLCalc()
    assert asyncio.run(_seed_from_ledger(_NC(), "beta", pnl)) == 1  # type: ignore[arg-type]

    assert json.loads(asked[0]) == {"strategy": "beta"}
    pnl.update_market_price("ETHUSDT", Decimal("40"))
    assert pnl.total_unrealized == Decimal("10")  # beta's short 1 from 50 — nobody else's


def test_container_starts_unseeded_if_the_consolidator_does_not_answer() -> None:
    from engine.strategy.pnl_calc import PnLCalc
    from workers.strategy_worker import _LEGACY_ACCOUNT_SEED, _seed_from_ledger

    class _NC:
        async def request(self, subject: str, data: bytes, timeout: float) -> None:
            raise TimeoutError("no responders")

    pnl = PnLCalc()
    assert asyncio.run(_seed_from_ledger(_NC(), "beta", pnl)) == 0  # type: ignore[arg-type]
    assert pnl.total_unrealized == 0
    assert _LEGACY_ACCOUNT_SEED is False  # the account-wide seeding is off by default


# -------------------------------------------------------------------- NATS


def test_nats_add_adds_and_rewrites_the_file(tmp_path: Path) -> None:
    _, reader = _setup(tmp_path)
    worker = _worker(tmp_path, reader, ids=(1,))

    reply = asyncio.run(handle_add(worker, b'{"names": ["beta"], "ids": [3]}'))

    assert reply == {"added": [2, 3], "watched": [1, 2, 3]}
    data = json.loads((tmp_path / "strategy_info.json").read_text())
    assert [s["id"] for s in data["strategies"]] == [1, 2, 3]


def test_nats_add_reports_unknown_and_bad_requests(tmp_path: Path) -> None:
    _, reader = _setup(tmp_path)
    worker = _worker(tmp_path, reader)

    assert asyncio.run(handle_add(worker, b'{"ids": [5]}')) == {"error": "unknown strategy id 5"}
    assert "error" in asyncio.run(handle_add(worker, b'{"ids": ["x"]}'))
    assert "error" in asyncio.run(handle_add(worker, b"not json"))
    assert worker.watched == []


def test_nats_get_returns_info_or_error(tmp_path: Path) -> None:
    _, reader = _setup(tmp_path)
    worker = _worker(tmp_path, reader)

    reply = asyncio.run(handle_get(worker, b'{"ids": [2]}'))
    assert [s["name"] for s in reply["strategies"]] == ["beta"]
    assert asyncio.run(handle_get(worker, b'{"ids": [2, 6]}')) == {"error": "unknown strategy id 6"}
