from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from data.types import FundingRate, TimeBar
from engine.backtest.data import HistoryData, load_history, with_marks
from engine.backtest.engine import _max_drawdown, run_backtest
from engine.backtest.sim_broker import PERP, SPOT, SimBroker, SimExchange
from engine.clock import SimClock
from engine.order.performance import PnLPoint
from execution.types import (
    FillConfirmation,
    FillStatus,
    InstrumentRules,
    Order,
    OrderSide,
    OrderType,
)
from interfaces.signals import TargetPosition
from interfaces.strategy import BaseStrategy
from workers.backtest import main, strategy_markets, strategy_params

if TYPE_CHECKING:
    from pathlib import Path

_T0 = datetime(2026, 9, 1, tzinfo=UTC)
_8H = timedelta(hours=8)
_MS = timedelta(milliseconds=1)


def _bar(i: int, open_: str, close: str | None = None, symbol: str = "ETHUSDT") -> TimeBar:
    start = _T0 + i * _8H
    o, c = Decimal(open_), Decimal(close or open_)
    return TimeBar(
        symbol=symbol, open=o, high=max(o, c), low=min(o, c), close=c,
        volume=Decimal(1), trade_count=1, timestamp=start, close_time=start + _8H - _MS,
        interval_seconds=28800,
    )


def _rate(i: int, rate: str, symbol: str = "ETHUSDT") -> FundingRate:
    return FundingRate(symbol=symbol, funding_rate=Decimal(rate), mark_price=Decimal(0),
                       timestamp=_T0 + i * _8H)


_RULES = {
    ("bybit_demo", "ETHUSDT"): InstrumentRules(symbol="ETHUSDT", qty_step=Decimal("0.01"),
                                               min_qty=Decimal("0.01")),
    ("bybit_spot", "ETHUSDT"): InstrumentRules(symbol="ETHUSDT", qty_step=Decimal("0.00001")),
    ("bybit_demo", "LINKUSDT"): InstrumentRules(symbol="LINKUSDT", qty_step=Decimal(1),
                                                min_qty=Decimal(1)),
    ("bybit_spot", "LINKUSDT"): InstrumentRules(symbol="LINKUSDT", qty_step=Decimal("0.001"),
                                                min_qty=Decimal("0.001")),
}


# --------------------------------------------------------------- sim broker


def _sim(bars: list[TimeBar], now: datetime) -> tuple[SimClock, SimExchange]:
    clock = SimClock(now)
    return clock, SimExchange(clock, {bars[0].symbol: bars}, rules=_RULES)


def _order(side: OrderSide, qty: str, symbol: str = "ETHUSDT") -> Order:
    return Order(symbol=symbol, side=side, order_type=OrderType.MARKET,
                 quantity=Decimal(qty), price=Decimal(0), client_order_id="1_1")


def test_market_order_fills_at_the_next_bar_open_with_fees() -> None:
    bars = [_bar(0, "100", "105"), _bar(1, "106", "110")]
    _, sim = _sim(bars, bars[0].close_time)  # just saw bar 0 close at 105
    perp = SimBroker(sim, "bybit_demo", PERP)

    result = asyncio.run(perp.place_order(_order(OrderSide.BUY, "2")))
    status = asyncio.run(perp.order_status(client_order_id="1_1"))

    assert result.fill_price == Decimal("106")  # next open, not 105
    assert status is not None and status.state.is_final
    assert status.fees == {"USDT": Decimal("2") * 106 * Decimal("0.00055")}
    pos = asyncio.run(perp.position("ETHUSDT"))
    assert pos is not None and (pos.quantity, pos.avg_entry_price) == (Decimal(2), Decimal(106))


def test_slippage_moves_the_price_against_you() -> None:
    bars = [_bar(0, "100"), _bar(1, "100")]
    clock = SimClock(bars[0].close_time)
    sim = SimExchange(clock, {"ETHUSDT": bars}, slippage_bps=Decimal(10))
    perp = SimBroker(sim, "bybit_demo", PERP)

    buy = asyncio.run(perp.place_order(_order(OrderSide.BUY, "1")))
    sell = asyncio.run(perp.place_order(_order(OrderSide.SELL, "1")))
    assert (buy.fill_price, sell.fill_price) == (Decimal("100.1"), Decimal("99.9"))


def test_spot_buy_pays_its_fee_in_coin_and_sells_need_the_balance() -> None:
    bars = [_bar(0, "100"), _bar(1, "100")]
    _, sim = _sim(bars, bars[0].close_time)
    spot = SimBroker(sim, "bybit_spot", SPOT)

    asyncio.run(spot.place_order(_order(OrderSide.BUY, "1")))
    status = asyncio.run(spot.order_status(client_order_id="1_1"))
    assert status is not None and status.fees == {"ETH": Decimal("0.001")}
    pos = asyncio.run(spot.position("ETHUSDT"))
    assert pos is not None and pos.quantity == Decimal("0.999")

    refused = asyncio.run(spot.place_order(_order(OrderSide.SELL, "1")))
    assert refused.error is not None and "insufficient ETH balance" in refused.error


def test_no_bar_after_now_refuses_the_order() -> None:
    bars = [_bar(0, "100")]
    _, sim = _sim(bars, bars[0].close_time)

    result = asyncio.run(SimBroker(sim, "bybit_demo", PERP).place_order(_order(OrderSide.BUY, "1")))
    assert result.error is not None and "no price data" in result.error


def test_funding_paid_on_the_held_position() -> None:
    bars = [_bar(0, "100", "200"), _bar(1, "200")]
    clock, sim = _sim(bars, bars[0].close_time - 2 * _8H)
    perp = SimBroker(sim, "bybit_demo", PERP)
    clock.set(_T0 - _MS)  # before bar 0 opens: the order fills at its open (100)
    asyncio.run(perp.place_order(_order(OrderSide.SELL, "2")))

    clock.set(_T0 + _8H)  # settlement; mark = bar 0 close
    settlement = perp.settle_funding("ETHUSDT", Decimal("0.0001"), clock.now())

    assert settlement is not None
    assert settlement.amount == Decimal("0.04")  # short 2 × 200 × 0.0001, received
    assert settlement.position == Decimal(-2)
    assert asyncio.run(perp.funding_settlements(_T0, _T0 + _8H)) == [settlement]
    assert SimBroker(sim, "bybit_spot", SPOT).settle_funding("ETHUSDT", Decimal(1), _T0) is None


def test_sim_clock_never_goes_back() -> None:
    clock = SimClock(_T0)
    clock.set(_T0 + _8H)
    with pytest.raises(ValueError):
        clock.set(_T0)


# -------------------------------------------------------------- backtests


class _Script(BaseStrategy):
    """Buys 1 ETH on the 1st live bar, sells it on the 3rd."""

    topics = ["futures.ETHUSDT.bars.8h", "futures.ETHUSDT.funding_rate"]
    max_loss = Decimal(1_000_000)

    def __init__(self) -> None:
        self.history: list[datetime] = []
        self.live: list[datetime] = []
        self.clock_seen: list[datetime] = []
        self.fills: list[FillConfirmation] = []
        self.qty = Decimal(1)

    def on_history_bar(self, bar: Any) -> None:
        self.history.append(bar.timestamp)

    def on_bar(self, bar: Any) -> TargetPosition | None:
        self.live.append(bar.timestamp)
        self.clock_seen.append(self.now())
        if len(self.live) == 1:
            return TargetPosition(symbol="ETHUSDT", quantity=self.qty, exchange="bybit_demo")
        if len(self.live) == 3:
            return TargetPosition(symbol="ETHUSDT", quantity=-self.qty, exchange="bybit_demo")
        return None

    def on_fill(self, fill: FillConfirmation) -> TargetPosition | None:
        self.fills.append(fill)
        return None


def _eth_data(funding: list[FundingRate] | None = None) -> HistoryData:
    # bars -2, -1: warm-up; 0..4 replayed.  Opens 100, 110, 120, …
    bars = [_bar(i, str(100 + 10 * i), str(105 + 10 * i)) for i in range(-2, 5)]
    start, end = _T0, _T0 + 5 * _8H
    return HistoryData(
        interval="8h", start=start, end=end, bars={"ETHUSDT": bars},
        funding={"ETHUSDT": with_marks(funding or [], bars)}, rules=_RULES,
    )


def test_strategy_runs_through_history_with_simulated_time(tmp_path: Path) -> None:
    strategy = _Script()
    result = asyncio.run(run_backtest(strategy, _eth_data(), tmp_path))

    assert strategy.history == [_T0 - 2 * _8H, _T0 - _8H]  # warm-up → on_history_bar only
    assert strategy.live == [_T0 + i * _8H for i in range(5)]
    assert strategy.clock_seen == [_T0 + (i + 1) * _8H for i in range(5)]  # the boundary each bar closes on

    # bought at bar 1's open (110), sold at bar 3's open (130)
    assert [(f.quantity, f.fill_price) for f in strategy.fills] == [
        (Decimal(1), Decimal(110)), (Decimal(-1), Decimal(130)),
    ]
    assert result.realized_pnl == Decimal(20)
    assert result.fees == (Decimal(110) + Decimal(130)) * Decimal("0.00055")
    assert result.net_pnl == Decimal(20) - result.fees
    assert [t.status for t in result.trades] == ["filled", "filled"]
    assert result.trades[0].created_at == _T0 + _8H  # simulated, not wall-clock
    assert result.outcomes == {"filled": 2}
    assert "net PnL" in result.summary()


def test_same_inputs_give_the_same_result(tmp_path: Path) -> None:
    a = asyncio.run(run_backtest(_Script(), _eth_data(), tmp_path / "a"))
    b = asyncio.run(run_backtest(_Script(), _eth_data(), tmp_path / "b"))
    assert a.to_dict()["pnl"] == b.to_dict()["pnl"]
    assert a.net_pnl == b.net_pnl


def test_guard_blocks_signals_in_a_backtest(tmp_path: Path) -> None:
    strategy = _Script()
    strategy.qty = Decimal(-1)  # short 1 at 110, buy back at 130: −20
    result = asyncio.run(run_backtest(strategy, _eth_data(), tmp_path, max_loss=Decimal(5)))

    assert result.guard_tripped
    assert result.outcomes == {"filled": 2}  # the trip happens on the closing fill
    assert "GUARD TRIPPED" in result.summary()


def test_funding_is_paid_and_split_to_the_strategy(tmp_path: Path) -> None:
    strategy = _Script()
    strategy.qty = Decimal(-1)  # short from bar 1's open, closed at bar 3's open
    funding = [_rate(i, "0.001") for i in range(1, 5)]
    result = asyncio.run(run_backtest(strategy, _eth_data(funding), tmp_path))

    # held short 1 at the settlements at bars 2 and 3 (marks 115 and 125):
    # bar 1's settlement is before the fill; bar 4's after it's closed.
    assert result.funding == Decimal("0.115") + Decimal("0.125")
    assert result.unattributed_funding == 0


def test_real_funding_arb_strategy_runs_unchanged(tmp_path: Path) -> None:
    """Plug-and-play: the live FundingArbBybitStrategy class, untouched."""
    from strategies.funding_arb_bybit import FundingArbBybitStrategy

    n = 12
    bars = [_bar(i, "10", symbol="LINKUSDT") for i in range(-3, n + 1)]
    rates = [_rate(i, "0.0005" if i < 8 else "0.00001", "LINKUSDT") for i in range(n)]
    data = HistoryData(
        interval="8h", start=_T0, end=_T0 + n * _8H, bars={"LINKUSDT": bars},
        funding={"LINKUSDT": with_marks(rates, bars)}, rules=_RULES,
    )

    result = asyncio.run(run_backtest(FundingArbBybitStrategy(), data, tmp_path))

    by_exchange = {(t.exchange, t.side.value) for t in result.trades}
    assert {("bybit_demo", "sell"), ("bybit_spot", "buy")} <= by_exchange  # entered, hedged
    assert ("bybit_demo", "buy") in by_exchange  # exited when funding dropped
    assert result.funding > 0
    assert result.outcomes.get("filled", 0) >= 4


def test_max_drawdown() -> None:
    def p(net: str | None) -> PnLPoint:
        return PnLPoint(strategy="a", strategy_num=1, taken_at=_T0, orders=0,
                        realized_pnl=Decimal(0), unrealized_pnl=None, fees_paid=Decimal(0),
                        net_pnl=None if net is None else Decimal(net))

    assert _max_drawdown([p("0"), p("10"), p("4"), p(None), p("12"), p("1"), p("3")]) == 11


# --------------------------------------------------------------------- data


class _Client:
    def __init__(self, bars: list[TimeBar], rates: list[FundingRate]) -> None:
        self.bars, self.rates = bars, rates
        self.bar_calls = 0
        self.funding_calls: list[tuple[datetime, datetime]] = []

    async def time_bars(self, symbol: str, interval: Any, *, start: datetime, end: datetime) -> list[TimeBar]:
        self.bar_calls += 1
        return self.bars

    async def funding_rates(self, symbol: str, *, start: datetime, end: datetime) -> list[FundingRate]:
        self.funding_calls.append((start, end))
        return [r for r in self.rates if start <= r.timestamp < end]


def test_history_is_fetched_once_then_read_from_the_cache(tmp_path: Path) -> None:
    bars = [_bar(i, "100", "101") for i in range(-1, 3)]
    client = _Client(bars, [_rate(1, "0.0001")])

    async def rules() -> dict[str, list[InstrumentRules]]:
        return {"bybit_demo": [_RULES[("bybit_demo", "ETHUSDT")]]}

    first = asyncio.run(load_history(["ETHUSDT"], "8h", _T0, _T0 + 3 * _8H, warmup_bars=1,
                                     cache_dir=tmp_path, client=client, rules_loader=rules))
    second = asyncio.run(load_history(["ETHUSDT"], "8h", _T0, _T0 + 3 * _8H, warmup_bars=1,
                                      cache_dir=tmp_path, client=client, rules_loader=rules))

    assert client.bar_calls == 1  # second load came from the cache
    assert second.bars == first.bars
    assert second.funding["ETHUSDT"][0].mark_price == Decimal(101)  # last close before it
    assert second.rules[("bybit_demo", "ETHUSDT")] == _RULES[("bybit_demo", "ETHUSDT")]


def test_funding_is_fetched_in_windows(tmp_path: Path) -> None:
    client = _Client([_bar(0, "1")], [])

    async def rules() -> dict[str, list[InstrumentRules]]:
        return {}

    asyncio.run(load_history(["ETHUSDT"], "8h", _T0, _T0 + timedelta(days=150),
                             cache_dir=tmp_path, client=client, rules_loader=rules))
    assert len(client.funding_calls) == 3  # 60 + 60 + 30 days
    assert client.funding_calls[0] == (_T0, _T0 + timedelta(days=60))


def test_strategy_markets_from_its_topics() -> None:
    assert strategy_markets(
        ["futures.ETHUSDT.bars.8h", "futures.LINKUSDT.funding_rate"], None
    ) == (["ETHUSDT", "LINKUSDT"], "8h")
    assert strategy_markets(["futures.ETHUSDT.funding_rate"], None) == (["ETHUSDT"], "1h")
    with pytest.raises(ValueError, match="several bar intervals"):
        strategy_markets(["futures.ETHUSDT.bars.8h", "futures.ETHUSDT.bars.1h"], None)


def test_strategy_params_from_the_command_line() -> None:
    assert strategy_params(
        ["entry_threshold_annualized=0.08", "min_consecutive_positive=2",
         "symbols=ETHUSDT,SOLUSDT", "note=plain"]
    ) == {"entry_threshold_annualized": 0.08, "min_consecutive_positive": 2,
          "symbols": ("ETHUSDT", "SOLUSDT"), "note": "plain"}
    with pytest.raises(ValueError, match="key=value"):
        strategy_params(["oops"])


def test_command_line_runs_a_backtest_from_the_cache(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole CLI, offline: the cache is filled first with a fake client."""
    from strategies.funding_arb_bybit import FundingArbBybitStrategy

    n, cache = 12, tmp_path / "cache"
    symbols, _ = strategy_markets(list(FundingArbBybitStrategy.topics), None)
    for symbol in symbols:
        hot = symbol == "LINKUSDT"
        client = _Client(
            [_bar(i, "10", symbol=symbol) for i in range(-3, n + 1)],
            [_rate(i, "0.0005" if hot and i < 8 else "0.00001", symbol) for i in range(n)],
        )

        async def rules() -> dict[str, list[InstrumentRules]]:
            return {ex: [r for (e, _), r in _RULES.items() if e == ex]
                    for ex in ("bybit_demo", "bybit_spot")}

        asyncio.run(load_history([symbol], "8h", _T0, _T0 + n * _8H, warmup_bars=3,
                                 cache_dir=cache, client=client, rules_loader=rules))

    out = tmp_path / "result.json"
    code = main([
        "--strategy", "FundingArbBybitStrategy", "--start", "2026-09-01",
        "--end", (_T0 + n * _8H).strftime("%Y-%m-%dT%H:%M"), "--warmup", "3",
        "--cache", str(cache), "--workdir", str(tmp_path / "work"), "--out", str(out),
        "--param", "max_volatility=5",
    ])

    assert code == 0
    printed = capsys.readouterr().out
    assert "Backtest FundingArbBybitStrategy" in printed
    import json

    result = json.loads(out.read_text())
    assert Decimal(result["funding"]) > 0
    assert any(t["symbol"] == "LINKUSDT" for t in result["trades"])


def test_command_line_refuses_an_empty_period(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--strategy", "FundingArbBybitStrategy",
                 "--start", "2026-09-02", "--end", "2026-09-01"]) == 2
    assert "--end must be after --start" in capsys.readouterr().out


def test_funding_events_carry_next_funding_time() -> None:
    """Like the live ticker: each settlement says when the next one is, so a
    strategy can learn a 4h symbol's interval (funding-arb does)."""
    bars = [_bar(i, str(100 + i)) for i in range(4)]
    four_h = [replace(_rate(1, "0.0001"), timestamp=_T0 + _8H + k * timedelta(hours=4))
              for k in range(3)]
    marked = with_marks(four_h, bars)
    assert [r.next_funding_time for r in marked] == [
        _T0 + 12 * timedelta(hours=1), _T0 + 16 * timedelta(hours=1), None,
    ]
    assert [r.mark_price for r in marked] == [Decimal(100), Decimal(100), Decimal(101)]
