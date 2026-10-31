"""B12: start-up history goes to on_history_bar, never to on_bar."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from data.types import FundingRate, TimeBar
from engine.data import codec
from engine.strategy.guard import StrategyGuard
from engine.strategy.pnl_calc import PnLCalc
from engine.strategy.runner import StrategyRunner
from interfaces.signals import TargetPosition
from interfaces.strategy import BaseStrategy
from workers import datafeed_server, strategy_worker

_T0 = datetime(2026, 10, 18, 0, 0, tzinfo=UTC)
_8H = timedelta(hours=8)


def _bar(i: int, close: str = "100", symbol: str = "ETHUSDT") -> TimeBar:
    start = _T0 + i * _8H
    return TimeBar(
        symbol=symbol, open=Decimal(close), high=Decimal(close), low=Decimal(close),
        close=Decimal(close), volume=Decimal(1), trade_count=1,
        timestamp=start, close_time=start + _8H - timedelta(milliseconds=1),
        interval_seconds=28800,
    )


# ---------------------------------------------------------------- data feed


class _Client:
    def __init__(self, bars: list[TimeBar]) -> None:
        self.bars = bars
        self.asked: list[tuple[str, Any]] = []

    async def time_bars(self, symbol: str, interval: Any, *, start: datetime, end: datetime) -> list[TimeBar]:
        self.asked.append((symbol, interval))
        return self.bars


def test_history_is_only_finished_bars_oldest_first() -> None:
    now = _T0 + 3 * _8H + timedelta(hours=2)  # bar 3 is still open
    client = _Client([_bar(3), _bar(1), _bar(0), _bar(2)])

    bars = asyncio.run(datafeed_server.fetch_history(client, "ETHUSDT", "8h", 60, now=now))  # type: ignore[arg-type]

    assert [b.timestamp for b in bars] == [_T0, _T0 + _8H, _T0 + 2 * _8H]


def test_history_keeps_the_last_count_bars() -> None:
    now = _T0 + 10 * _8H
    client = _Client([_bar(i) for i in range(10)])

    bars = asyncio.run(datafeed_server.fetch_history(client, "ETHUSDT", "8h", 3, now=now))  # type: ignore[arg-type]

    assert [b.timestamp for b in bars] == [_T0 + i * _8H for i in (7, 8, 9)]


def test_history_reply_and_its_errors() -> None:
    client = _Client([_bar(-10)])  # well in the past: finished by the real clock
    clients = {"futures": client}

    ok = asyncio.run(datafeed_server.history_reply(clients, b'{"topic": "futures.ETHUSDT.bars.8h"}'))  # type: ignore[arg-type]
    assert ok["topic"] == "futures.ETHUSDT.bars.8h"
    (decoded,) = [codec.decode(json.dumps(b).encode()) for b in ok["bars"]]
    assert decoded == _bar(-10)

    for body, error in (
        (b'{"topic": "futures.ETHUSDT.funding_rate"}', "not a bar topic"),
        (b'{"topic": "spot.ETHUSDT.bars.8h"}', "no client for market 'spot'"),
        (b'{"topic": "futures.ETHUSDT.bars.7h"}', "unsupported interval"),
        (b"{}", "topic"),
    ):
        assert error in asyncio.run(datafeed_server.history_reply(clients, body))["error"]  # type: ignore[arg-type]


def test_live_stream_no_longer_gets_history_by_default() -> None:
    assert datafeed_server._SEED_LIVE is False


# ------------------------------------------------------------------- runner


class _Bus:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()

    def subscribe(self, *topics: str) -> asyncio.Queue[Any]:
        return self.queue

    def unsubscribe(self, queue: asyncio.Queue[Any]) -> None:
        pass


class _Recorder(BaseStrategy):
    topics = ["futures.ETHUSDT.bars.8h"]

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def on_start(self) -> None:
        self.calls.append(("start", None))

    def on_history_bar(self, bar: Any) -> None:
        self.calls.append(("history", bar.timestamp))
        return TargetPosition(symbol="ETHUSDT", quantity=Decimal(5))  # type: ignore[return-value]

    def on_bar(self, bar: Any) -> TargetPosition | None:
        self.calls.append(("live", bar.timestamp))
        return None

    def on_funding_rate(self, rate: FundingRate) -> TargetPosition | None:
        return TargetPosition(symbol="ETHUSDT", quantity=Decimal(1))


def _run(history: list[TimeBar], live: list[Any]) -> tuple[_Recorder, list[TargetPosition]]:
    strategy = _Recorder()
    bus = _Bus()
    targets: asyncio.Queue[TargetPosition] = asyncio.Queue()
    runner = StrategyRunner(
        strategy_id="alpha", strategy=strategy, bus=bus, topics=strategy.topics,
        target_queue=targets, guard=StrategyGuard(max_loss=Decimal(1000)), pnlcalc=PnLCalc(),
    )

    async def go() -> list[TargetPosition]:
        for event in live:
            bus.queue.put_nowait(event)
        task = asyncio.create_task(runner.run(history))
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        out = []
        while not targets.empty():
            out.append(targets.get_nowait())
        return out

    return strategy, asyncio.run(go())


def test_history_goes_to_on_history_bar_after_on_start_then_live_to_on_bar() -> None:
    strategy, _ = _run([_bar(0), _bar(1)], [_bar(2)])

    assert strategy.calls == [
        ("start", None), ("history", _T0), ("history", _T0 + _8H), ("live", _T0 + 2 * _8H),
    ]


def test_live_bars_queued_while_the_history_is_fetched_are_not_lost() -> None:
    """Like the NATS bus: a queue only gets events published after its
    subscribe — so the worker subscribes before fetching the history."""

    class _FreshQueueBus(_Bus):
        def __init__(self) -> None:
            self.queues: list[asyncio.Queue[Any]] = []

        def subscribe(self, *topics: str) -> asyncio.Queue[Any]:
            self.queues.append(asyncio.Queue())
            return self.queues[-1]

    strategy, bus = _Recorder(), _FreshQueueBus()
    runner = StrategyRunner(
        strategy_id="alpha", strategy=strategy, bus=bus, topics=strategy.topics,
        target_queue=asyncio.Queue(), guard=StrategyGuard(max_loss=Decimal(1000)),
        pnlcalc=PnLCalc(),
    )

    async def go() -> None:
        runner.subscribe()
        bus.queues[0].put_nowait(_bar(2))  # closed while the history was being fetched
        task = asyncio.create_task(runner.run([_bar(0), _bar(1)]))
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()

    asyncio.run(go())
    assert len(bus.queues) == 1  # run() kept the early subscription
    assert [c for c in strategy.calls if c[0] == "live"] == [("live", _T0 + 2 * _8H)]


def test_a_live_bar_the_history_already_covered_is_skipped() -> None:
    strategy, _ = _run([_bar(0), _bar(1)], [_bar(1), _bar(2)])

    assert [c for c in strategy.calls if c[0] == "live"] == [("live", _T0 + 2 * _8H)]


def test_history_is_never_traded_but_sets_the_price_for_live_signals() -> None:
    funding = FundingRate(symbol="ETHUSDT", funding_rate=Decimal("0.0001"),
                          mark_price=Decimal(123), timestamp=_T0)
    _, targets = _run([_bar(0, close="2500")], [funding])

    # on_history_bar returned a signal: ignored.  The funding tick's signal is
    # stamped with the last price, which came from the history.
    assert [(t.quantity, t.price) for t in targets] == [(Decimal(1), Decimal(2500))]


def test_on_history_bar_does_nothing_by_default() -> None:
    class Plain(BaseStrategy):
        def on_bar(self, bar: Any) -> None:
            return None

    assert Plain().on_history_bar(_bar(0)) is None  # type: ignore[func-returns-value]


# ----------------------------------------------------------- strategy worker


def test_worker_fetches_history_for_its_bar_topics_in_time_order() -> None:
    # Bars well in the past: the feed uses the real clock to leave out open bars.
    clients = {"futures": _Client([_bar(-10, symbol="ETHUSDT"), _bar(-9, symbol="ETHUSDT")])}
    link = _Client([_bar(-10, symbol="LINKUSDT")])
    asked: list[str] = []

    class _NC:
        async def request(self, subject: str, data: bytes, timeout: float) -> SimpleNamespace:
            topic = json.loads(data)["topic"]
            asked.append(topic)
            source = {"futures": link} if "LINK" in topic else clients
            reply = await datafeed_server.history_reply(source, data)  # type: ignore[arg-type]
            return SimpleNamespace(data=json.dumps(reply).encode())

    bars = asyncio.run(strategy_worker._fetch_history(_NC(), [  # type: ignore[arg-type]
        "futures.ETHUSDT.bars.8h", "futures.ETHUSDT.funding_rate", "futures.LINKUSDT.bars.8h",
    ]))

    assert asked == ["futures.ETHUSDT.bars.8h", "futures.LINKUSDT.bars.8h"]  # bar topics only
    assert [(b.symbol, b.timestamp) for b in bars] == [
        ("ETHUSDT", _T0 - 10 * _8H), ("LINKUSDT", _T0 - 10 * _8H), ("ETHUSDT", _T0 - 9 * _8H),
    ]


def test_worker_starts_without_history_if_the_feed_does_not_answer() -> None:
    class _NC:
        async def request(self, subject: str, data: bytes, timeout: float) -> None:
            raise TimeoutError("no responders")

    assert asyncio.run(strategy_worker._fetch_history(_NC(), ["futures.ETHUSDT.bars.8h"])) == []  # type: ignore[arg-type]


@pytest.mark.parametrize("error_reply", [{"error": "boom"}])
def test_worker_skips_a_topic_the_feed_refuses(error_reply: dict[str, str]) -> None:
    class _NC:
        async def request(self, subject: str, data: bytes, timeout: float) -> SimpleNamespace:
            return SimpleNamespace(data=json.dumps(error_reply).encode())

    assert asyncio.run(strategy_worker._fetch_history(_NC(), ["futures.ETHUSDT.bars.8h"])) == []  # type: ignore[arg-type]
