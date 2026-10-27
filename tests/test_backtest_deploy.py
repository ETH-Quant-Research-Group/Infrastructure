"""Backtests from the Deploy page: the month-piece history cache and its
coverage, offline runs, ``--markets`` / ``--live-settings``, and the guard's
close-on-trip inside a backtest."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from engine.backtest.data import MissingHistory, coverage, load_history, months
from engine.backtest.engine import run_backtest
from engine.backtest.history_db import HistoryDB
from tests.test_backtest import (
    _8H,
    _RULES,
    _T0,
    _bar,
    _Client,
    _eth_data,
    _rate,
    _Script,
)
from workers.backtest import live_settings, main, strategy_markets

if TYPE_CHECKING:
    from pathlib import Path

    from execution.types import InstrumentRules


async def _rules() -> dict[str, list[InstrumentRules]]:
    return {
        ex: [r for (e, _), r in _RULES.items() if e == ex]
        for ex in ("bybit_demo", "bybit_spot")
    }


def _load(cache: Path, client: _Client | None, end: datetime, **kw: object):  # type: ignore[no-untyped-def]
    return asyncio.run(
        load_history(
            ["ETHUSDT"],
            "8h",
            _T0,
            end,
            warmup_bars=1,
            cache_dir=cache,
            client=client,
            rules_loader=_rules,
            **kw,
        )
    )


# ------------------------------------------------------------ month pieces


def test_months() -> None:
    assert months(
        datetime(2025, 11, 20, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    ) == [
        datetime(2025, 11, 1, tzinfo=UTC),
        datetime(2025, 12, 1, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 2, 1, tzinfo=UTC),
    ]


def test_another_period_in_the_same_months_downloads_nothing(tmp_path: Path) -> None:
    client = _Client([_bar(i, "100") for i in range(-1, 9)], [_rate(1, "0.0001")])

    _load(tmp_path, client, _T0 + 3 * _8H)
    data = _load(tmp_path, client, _T0 + 8 * _8H)  # longer, same months

    assert client.bar_calls == 1
    assert len(data.bars["ETHUSDT"]) == 10  # warm-up bar + 9
    db = HistoryDB(tmp_path / "history.db", readonly=True)
    assert (
        db.fetched("ETHUSDT", "bars_8h", datetime(2026, 8, 1, tzinfo=UTC)) is not None
    )
    assert db.fetched("ETHUSDT", "funding", _T0) is not None
    db.close()


def test_offline_lists_what_is_missing_then_works_once_downloaded(
    tmp_path: Path,
) -> None:
    with pytest.raises(MissingHistory) as missing:
        _load(tmp_path, None, _T0 + 3 * _8H, offline=True)
    assert missing.value.missing == [
        "ETHUSDT 8h bars 2026-08",
        "ETHUSDT 8h bars 2026-09",
        "ETHUSDT funding 2026-09",
        "order-size rules",
    ]

    _load(tmp_path, _Client([_bar(i, "100") for i in range(-1, 3)], []), _T0 + 3 * _8H)
    data = _load(tmp_path, None, _T0 + 3 * _8H, offline=True)  # no client, no network
    assert len(data.bars["ETHUSDT"]) == 4


def test_a_month_fetched_before_it_ended_is_fetched_again_when_needed(
    tmp_path: Path,
) -> None:
    db = HistoryDB(tmp_path / "history.db")
    db.write_funding("bybit", "ETHUSDT", [_rate(1, "0.0001")])
    db.mark_fetched(
        "ETHUSDT", "funding", _T0, _T0 + 2 * _8H, complete=False
    )  # mid-September
    db.close()

    cov = coverage(
        ["ETHUSDT"], "8h", _T0, _T0 + 5 * _8H, warmup_bars=0, cache_dir=tmp_path
    )
    assert cov["symbols"]["ETHUSDT"]["funding"] == [
        {"month": "2026-09", "status": "partial", "count": 0}
    ]
    cov = coverage(["ETHUSDT"], "8h", _T0, _T0 + _8H, warmup_bars=0, cache_dir=tmp_path)
    assert (
        cov["symbols"]["ETHUSDT"]["funding"][0]["status"] == "cached"
    )  # covers this one


def test_coverage_per_month_and_listing(tmp_path: Path) -> None:
    end = _T0 + 3 * _8H
    before = coverage(
        ["ETHUSDT", "NEWUSDT"],
        "8h",
        _T0,
        end,
        warmup_bars=1,
        cache_dir=tmp_path,
        listings={"NEWUSDT": datetime(2026, 9, 15, tzinfo=UTC)},
    )
    assert not before["ready"]
    assert [r["status"] for r in before["symbols"]["ETHUSDT"]["bars"]] == [
        "missing",
        "missing",
    ]
    # NEWUSDT was listed mid-September: August has nothing to download
    assert [r["status"] for r in before["symbols"]["NEWUSDT"]["bars"]] == [
        "before listing",
        "missing",
    ]
    assert before["symbols"]["NEWUSDT"]["listed"] == "2026-09-15T00:00:00+00:00"

    _load(
        tmp_path,
        _Client([_bar(i, "100") for i in range(-1, 3)], [_rate(1, "0.0001")]),
        end,
    )
    after = coverage(["ETHUSDT"], "8h", _T0, end, warmup_bars=1, cache_dir=tmp_path)
    assert after["ready"] and after["rules"]
    assert after["symbols"]["ETHUSDT"]["bars"] == [
        {"month": "2026-08", "status": "cached", "count": 1},
        {"month": "2026-09", "status": "cached", "count": 3},
    ]


# ------------------------------------------------------------- command line


def test_markets_prints_what_the_strategy_trades(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--strategy", "FundingArbBybitStrategy", "--markets"]) == 0
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["strategy"] == "FundingArbBybitStrategy"
    assert "LINKUSDT" in printed["symbols"]
    assert printed["intervals"]


def test_strategy_name_comes_from_the_image_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STRATEGY_NAME", "FundingArbBybitStrategy")
    assert main(["--markets"]) == 0
    assert "FundingArbBybitStrategy" in capsys.readouterr().out


def test_offline_run_without_data_fails_clearly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "--strategy",
            "FundingArbBybitStrategy",
            "--start",
            "2026-09-01",
            "--end",
            "2026-09-02",
            "--offline",
            "--cache",
            str(tmp_path),
        ]
    )
    assert code == 2
    assert "History missing" in capsys.readouterr().out


def test_a_timeframe_the_strategy_doesnt_get_is_refused() -> None:
    with pytest.raises(ValueError, match="would see no bars"):
        strategy_markets(["futures.ETHUSDT.bars.8h"], "1h")


def test_live_settings_like_the_consolidator(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MAX_DRAWDOWN",
        "DEFAULT_MAX_LOSS",
        "CLOSE_ON_TRIP",
        "GUARD_CLOSE_ON_TRIP",
        "GUARD_INCLUDE_UNREALIZED",
        "GUARD_UNREALIZED_EVERY",
    ):
        monkeypatch.delenv(name, raising=False)
    assert live_settings() == {
        "max_loss": Decimal(1000),
        "close_on_trip": False,
        "include_unrealized": False,
        "unrealized_every": 0,
    }

    monkeypatch.setenv("DEFAULT_MAX_LOSS", "800")
    monkeypatch.setenv("GUARD_CLOSE_ON_TRIP", "true")
    monkeypatch.setenv("GUARD_UNREALIZED_EVERY", "10")
    assert live_settings()["max_loss"] == Decimal(800)
    assert live_settings()["close_on_trip"] is True
    assert live_settings()["unrealized_every"] == 10

    monkeypatch.setenv("MAX_DRAWDOWN", "250")  # the deploy's own setting wins
    monkeypatch.setenv("CLOSE_ON_TRIP", "false")
    assert live_settings()["max_loss"] == Decimal(250)
    assert live_settings()["close_on_trip"] is False


# ------------------------------------------------- close on trip, backtested


@pytest.mark.parametrize("close", [False, True])
def test_close_on_trip_in_a_backtest(tmp_path: Path, close: bool) -> None:
    strategy = _Script()
    strategy.qty = Decimal(-1)  # short at 110 while the price climbs
    result = asyncio.run(
        run_backtest(
            strategy,
            _eth_data(),
            tmp_path,
            max_loss=Decimal(5),
            unrealized_every=1,
            close_on_trip=close,
        )
    )

    assert result.guard_tripped
    held = sum((h.quantity for h in result.holdings), Decimal(0))
    assert held == (0 if close else -1)
    assert result.outcomes.get("blocked") == 1  # its own exit later was blocked
    assert result.outcomes["filled"] == (2 if close else 1)
