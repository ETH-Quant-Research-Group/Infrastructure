from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest

from engine.order.ledger import LedgerDB
from engine.order.performance import (
    BybitPrices,
    PnLHistoryDB,
    PnLPoint,
    sharpe,
    strategy_pnl,
)
from execution.types import FillConfirmation, FillStatus, OrderSide
from workers import snapshot_all

if TYPE_CHECKING:
    from pathlib import Path

_DAY0 = datetime(2026, 10, 1, tzinfo=UTC)


def _trade(ledger: LedgerDB, strategy: str, qty: str, price: str) -> None:
    side = OrderSide.BUY if Decimal(qty) > 0 else OrderSide.SELL
    tag = ledger.begin_order(strategy, "bybit_demo", "ETHUSDT", side, abs(Decimal(qty)))
    ledger.record_result(FillConfirmation(
        strategy_id=strategy, symbol="ETHUSDT", quantity=Decimal(qty),
        fill_price=Decimal(price), exchange="bybit_demo", status=FillStatus.FILLED,
        fees={"USDT": Decimal("1")}, order_link_id=tag,
    ))


def _prices(price: str | None):  # type: ignore[no-untyped-def]
    async def get(exchange: str, symbol: str) -> Decimal | None:
        return Decimal(price) if price else None
    return get


def _point(at: datetime, net: str | None) -> PnLPoint:
    value = None if net is None else Decimal(net)
    return PnLPoint(
        strategy="alpha", strategy_num=1, taken_at=at, orders=1,
        realized_pnl=Decimal(0), unrealized_pnl=value, fees_paid=Decimal(0), net_pnl=value,
    )


def _days(*nets: str | None) -> list[PnLPoint]:
    return [_point(_DAY0 + timedelta(days=i), n) for i, n in enumerate(nets)]


# ----------------------------------------------------------------- valuation


def test_strategy_pnl_values_holdings_at_live_prices(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _trade(ledger, "alpha", "2", "100")
    _trade(ledger, "alpha", "-1", "110")  # realize 10, hold 1 @ 100

    point = asyncio.run(strategy_pnl(ledger, "alpha", _prices("125")))

    assert (point.strategy, point.strategy_num, point.orders) == ("alpha", 1, 2)
    assert point.realized_pnl == Decimal("10")
    assert point.unrealized_pnl == Decimal("25")
    assert point.fees_paid == Decimal("2")
    assert point.net_pnl == Decimal("33")
    assert point.complete


def test_missing_price_gives_an_incomplete_point(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _trade(ledger, "alpha", "1", "100")

    point = asyncio.run(strategy_pnl(ledger, "alpha", _prices(None)))

    assert point.unrealized_pnl is None and point.net_pnl is None
    assert not point.complete


def test_history_round_trip(tmp_path: Path) -> None:
    db = PnLHistoryDB(tmp_path / "pnl_history.db")
    points = _days("1.5", None)
    for p in points:
        db.record(p)
    db.close()

    reader = PnLHistoryDB(tmp_path / "pnl_history.db", readonly=True)
    assert reader.history("alpha") == points
    assert reader.last_id("alpha") == 2
    assert reader.last_id("beta") is None


# -------------------------------------------------------------------- sharpe


def test_sharpe_of_daily_changes_annualized_by_sqrt_365() -> None:
    result = sharpe(_days("0", "10", "15", "30"))  # returns 10, 5, 15

    assert result.returns == 3
    assert result.value == pytest.approx(10 / (50 / 3) ** 0.5 * 365**0.5, abs=1e-3)


def test_sharpe_needs_two_returns_and_some_variation() -> None:
    assert sharpe(_days("0", "10")).value is None  # one return
    assert sharpe(_days("0", "10", "20", "30")).value is None  # std 0


def test_missing_day_is_not_counted_as_one_return() -> None:
    points = _days("0", "10", "20")
    points.append(_point(_DAY0 + timedelta(days=5), "1000"))  # days 3-4 not recorded

    assert sharpe(points).returns == 2


def test_incomplete_point_is_skipped_on_both_sides() -> None:
    assert sharpe(_days("0", "10", None, "30", "35")).returns == 2  # 0→10, 30→35


def test_several_recordings_a_day_keep_the_last_one() -> None:
    hourly = [_point(_DAY0 + timedelta(hours=h), str(h)) for h in range(72)]

    daily = sharpe(hourly)
    eight_hour = sharpe(hourly, period=timedelta(hours=8))

    assert daily.returns == 2  # 23 → 47 → 71
    assert eight_hour.returns == 8
    assert eight_hour.period == timedelta(hours=8)


# -------------------------------------------------------------------- prices


def _bybit(handler: object) -> BybitPrices:
    return BybitPrices(httpx.AsyncClient(
        base_url="https://api.bybit.com", transport=httpx.MockTransport(handler)  # type: ignore[arg-type]
    ))


def test_bybit_prices_use_mark_for_perps_and_last_for_spot() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = {"symbol": "ETHUSDT", "lastPrice": "2500.1", "markPrice": "2500.5"}
        if request.url.params["category"] == "spot":
            item.pop("markPrice")
        return httpx.Response(200, json={"retCode": 0, "result": {"list": [item]}})

    prices = _bybit(handler)
    assert asyncio.run(prices("bybit_demo", "ETHUSDT")) == Decimal("2500.5")
    assert asyncio.run(prices("bybit_spot", "ETHUSDT")) == Decimal("2500.1")
    assert [r.url.params["category"] for r in seen] == ["linear", "spot"]
    assert asyncio.run(prices("lighter", "ETHUSDT")) is None  # no Bybit category


def test_bybit_price_failure_is_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    assert asyncio.run(_bybit(handler)("bybit_demo", "ETHUSDT")) is None


# --------------------------------------------------------------- daily run


def _run(tmp_path: Path, only: str | None = None, price: str | None = "120") -> int:
    return asyncio.run(snapshot_all.run(
        tmp_path / "ledger.db", tmp_path / "snapshots.db", tmp_path / "pnl_history.db",
        only, prices=_prices(price), brokers={},  # no funding source
    ))


def test_daily_run_records_pnl_for_every_strategy_even_without_trades(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _trade(ledger, "alpha", "1", "100")
    _trade(ledger, "beta", "-1", "100")
    ledger.close()

    assert _run(tmp_path) == 0
    assert _run(tmp_path, price="130") == 0  # no trades in between

    history = PnLHistoryDB(tmp_path / "pnl_history.db", readonly=True)
    assert [p.net_pnl for p in history.history("alpha")] == [19, 29]
    assert [p.net_pnl for p in history.history("beta")] == [-21, -31]


def test_only_pnl_or_only_snapshots(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _trade(ledger, "alpha", "1", "100")
    ledger.close()

    assert _run(tmp_path, only="pnl") == 0
    assert not (tmp_path / "snapshots.db").exists()
    assert _run(tmp_path, only="snapshots") == 0
    assert len(PnLHistoryDB(tmp_path / "pnl_history.db", readonly=True).history("alpha")) == 1

    out = capsys.readouterr().out
    assert "PnL alpha: net 19" in out
    assert "Snapshot alpha: snapshot saved (order 1)" in out


def test_daily_run_marks_a_missing_price_incomplete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _trade(ledger, "alpha", "1", "100")
    ledger.close()

    assert _run(tmp_path, only="pnl", price=None) == 0

    assert "PnL alpha: INCOMPLETE" in capsys.readouterr().out
    (point,) = PnLHistoryDB(tmp_path / "pnl_history.db", readonly=True).history("alpha")
    assert not point.complete
