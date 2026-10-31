"""The backtest history database: storage, longer timeframes built from
shorter ones, user uploads (format detection, cleaning), datasets in a
backtest, and the starter set."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from data.types import TimeBar
from engine.backtest.data import (
    MissingHistory,
    coverage,
    download_history,
    load_history,
)
from engine.backtest.history_db import BYBIT, HistoryDB, aggregate, sources_for
from engine.backtest.seed import top_symbols
from engine.backtest.upload import (
    UploadError,
    parse_bars,
    parse_funding,
    parse_time,
    to_fraction,
)
from execution.types import InstrumentRules
from tests.test_backtest import _8H, _RULES, _T0, _bar, _Client, _rate

if TYPE_CHECKING:
    from pathlib import Path

_1H = timedelta(hours=1)


def _hour(i: int, price: str = "100", symbol: str = "ETHUSDT") -> TimeBar:
    start = _T0 + i * _1H
    p = Decimal(price)
    return TimeBar(
        symbol=symbol,
        open=p,
        high=p + 2,
        low=p - 1,
        close=p + 1,
        volume=Decimal(1),
        trade_count=1,
        timestamp=start,
        close_time=start + _1H - timedelta(milliseconds=1),
        interval_seconds=3600,
    )


async def _rules() -> dict[str, list[InstrumentRules]]:
    return {
        ex: [r for (e, _), r in _RULES.items() if e == ex]
        for ex in ("bybit_demo", "bybit_spot")
    }


# ------------------------------------------------------------- timeframes


def test_8h_bars_built_from_1h_exactly() -> None:
    hours = [_hour(i, str(100 + i)) for i in range(16)]

    eight = aggregate(hours, "8h")

    assert [b.timestamp for b in eight] == [_T0, _T0 + _8H]
    first = eight[0]
    assert (first.open, first.close) == (Decimal(100), Decimal(108))
    assert (first.high, first.low) == (Decimal(109), Decimal(99))
    assert first.volume == 8 and first.trade_count == 8
    assert first.interval_seconds == 28800


def test_an_incomplete_bucket_is_left_out() -> None:
    hours = [_hour(i) for i in range(16) if i != 3]  # hour 3 missing
    assert [b.timestamp for b in aggregate(hours, "8h")] == [_T0 + _8H]


def test_which_stored_timeframes_can_give_an_interval() -> None:
    assert sources_for("8h", ["1h", "8h", "5m", "3h"]) == ["8h", "1h", "5m"]
    assert sources_for("1h", ["4h"]) == []


def test_the_1h_starter_set_serves_8h_backtests(tmp_path: Path) -> None:
    db = HistoryDB(tmp_path / "history.db")
    hours = [_hour(i) for i in range(-8, 24)]
    db.write_bars(BYBIT, "ETHUSDT", "1h", hours)
    for month in (datetime(2026, 8, 1, tzinfo=UTC), _T0):
        db.mark_fetched(
            "ETHUSDT", "bars_1h", month, month + timedelta(days=31), complete=True
        )
    db.mark_fetched("ETHUSDT", "funding", _T0, _T0 + timedelta(days=30), complete=True)
    db.close()

    client = _Client([], [])
    data = asyncio.run(
        load_history(
            ["ETHUSDT"],
            "8h",
            _T0,
            _T0 + 2 * _8H,
            warmup_bars=1,
            cache_dir=tmp_path,
            client=client,
            rules_loader=_rules,
        )
    )

    assert client.bar_calls == 0  # nothing downloaded
    assert [b.timestamp for b in data.bars["ETHUSDT"]] == [
        _T0 - _8H,
        _T0,
        _T0 + _8H,
        _T0 + 2 * _8H,
    ]
    cov = coverage(
        ["ETHUSDT"], "8h", _T0, _T0 + 2 * _8H, warmup_bars=1, cache_dir=tmp_path
    )
    assert cov["symbols"]["ETHUSDT"]["bars"][1]["from"] == "1h"


# ---------------------------------------------------------------- uploads


def test_upload_formats_are_recognised() -> None:
    assert parse_time("1735689600")[0] == datetime(2025, 1, 1, tzinfo=UTC)
    assert parse_time("1735689600000")[1] == "unix ms"
    assert parse_time("1735689600000000")[1] == "unix us"
    assert parse_time("2025-01-01 08:00:00")[0] == datetime(2025, 1, 1, 8, tzinfo=UTC)
    assert parse_time("2025-01-01T08:00:00Z")[0] == datetime(2025, 1, 1, 8, tzinfo=UTC)
    assert parse_time("2025/01/01 08:00")[0] == datetime(2025, 1, 1, 8, tzinfo=UTC)


def test_bars_with_a_header_any_order_and_dirty_rows() -> None:
    text = (
        "Close;Open Time;HIGH;low;Open;Volume\n"
        "101,5;1735689600000;102;99;100;10\n"
        "102;1735693200000;103;100;101,5;11\n"
        "102;1735693200000;103;100;101,5;12\n"  # duplicate time: last wins
        "50;1735696800000;40;30;45;1\n"  # high below close: dropped
        "104;1735704000000;105;102;103;13\n"  # 02:00 (dropped) and 03:00 missing
    )
    parsed = parse_bars(text, "ETHUSDT")

    assert parsed.interval == "1h"
    assert parsed.detected["separator"] == ";"
    assert parsed.detected["time_format"] == "unix ms"
    assert [b.close for b in parsed.rows] == [
        Decimal("101.5"),
        Decimal(102),
        Decimal(104),
    ]
    assert parsed.rows[1].volume == 12
    issues = " ".join(parsed.issues)
    assert (
        "1 duplicate" in issues
        and "1 rows dropped" in issues
        and "missing 1h bars" in issues
    )


def test_bars_without_a_header_use_the_kline_order() -> None:
    text = "2025-01-01 00:00:00,1,2,0.5,1.5,7\n2025-01-01 00:05:00,1.5,2,1,1.8,3\n"
    parsed = parse_bars(text, "XUSDT")
    assert parsed.interval == "5m" and not parsed.detected["header"]
    assert parsed.rows[0].high == 2 and parsed.rows[0].volume == 7


def test_unreadable_uploads_say_why() -> None:
    with pytest.raises(UploadError, match="no close column"):
        parse_bars("time,open,high,low\n1,1,1,1\n2,1,1,1\n", "X")
    with pytest.raises(UploadError, match="supported timeframes"):
        parse_bars("time,open,high,low,close\n0,1,1,1,1\n7,1,1,1,1\n14,1,1,1,1\n", "X")


def test_funding_upload_and_percent() -> None:
    parsed = parse_funding(
        "fundingRateTimestamp,fundingRate\n1735689600000,0.01%\n1735718400000,0.02%\n",
        "ETHUSDT",
    )
    assert "percent" in " ".join(parsed.issues)
    assert [r.funding_rate for r in to_fraction(parsed).rows] == [
        Decimal("0.0001"),
        Decimal("0.0002"),
    ]


# --------------------------------------------------- datasets in a backtest


def _upload(tmp_path: Path, *, with_funding: bool, saved: bool = True) -> None:
    db = HistoryDB(tmp_path / "history.db")
    db.dataset_id("mine", saved=saved)
    db.write_bars("mine", "ETHUSDT", "1h", [_hour(i, "200") for i in range(0, 48)])
    if with_funding:
        db.write_funding("mine", "ETHUSDT", [_rate(1, "0.001")])
    db.write_rules(
        "mine", "bybit_demo", InstrumentRules(symbol="ETHUSDT", qty_step=Decimal(1))
    )
    db.close()


def test_an_upload_replaces_bybit_for_its_symbols_only(tmp_path: Path) -> None:
    _upload(tmp_path, with_funding=True)
    client = _Client(
        [_bar(i, "10", symbol="LINKUSDT") for i in range(-1, 6)],
        [_rate(1, "0.0002", "LINKUSDT")],
    )

    data = asyncio.run(
        load_history(
            ["ETHUSDT", "LINKUSDT"],
            "8h",
            _T0,
            _T0 + _8H,
            warmup_bars=0,
            cache_dir=tmp_path,
            client=client,
            rules_loader=_rules,
            dataset="mine",
        )
    )

    assert data.sources["ETHUSDT"] == {"bars": "mine", "funding": "mine"}
    assert data.sources["LINKUSDT"] == {"bars": "bybit", "funding": "bybit"}
    assert data.bars["ETHUSDT"][0].open == 200  # 8h built from the uploaded 1h
    assert data.funding["ETHUSDT"][0].funding_rate == Decimal("0.001")
    assert data.rules[("bybit_demo", "ETHUSDT")].qty_step == 1  # the upload's own rules
    assert client.bar_calls == 1  # LINKUSDT only


def test_an_upload_without_funding_takes_bybit_funding(tmp_path: Path) -> None:
    _upload(tmp_path, with_funding=False)
    cov = coverage(
        ["ETHUSDT"],
        "8h",
        _T0,
        _T0 + _8H,
        warmup_bars=0,
        cache_dir=tmp_path,
        dataset="mine",
    )
    assert cov["symbols"]["ETHUSDT"]["bars_source"] == "mine"
    assert cov["symbols"]["ETHUSDT"]["funding_source"] == "bybit"
    assert cov["symbols"]["ETHUSDT"]["own_rules"]


def test_a_period_outside_the_upload_is_missing(tmp_path: Path) -> None:
    _upload(tmp_path, with_funding=True)
    with pytest.raises(
        MissingHistory, match=r"ETHUSDT 8h bars 2026-10 \(not in mine\)"
    ):
        asyncio.run(
            load_history(
                ["ETHUSDT"],
                "8h",
                _T0,
                datetime(2026, 10, 5, tzinfo=UTC),
                warmup_bars=0,
                cache_dir=tmp_path,
                offline=True,
                dataset="mine",
            )
        )
    with pytest.raises(MissingHistory, match="dataset 'nope'"):
        asyncio.run(
            load_history(
                ["ETHUSDT"],
                "8h",
                _T0,
                _T0 + _8H,
                cache_dir=tmp_path,
                offline=True,
                dataset="nope",
            )
        )


def test_temporary_datasets_expire_and_bybit_stays(tmp_path: Path) -> None:
    _upload(tmp_path, with_funding=False, saved=False)
    db = HistoryDB(tmp_path / "history.db")
    db._conn.execute(
        "UPDATE datasets SET expires_at = '2000-01-01T00:00:00+00:00'"
        " WHERE name = 'mine'"
    )
    db._conn.commit()

    assert db.drop_expired() == ["mine"]
    assert [d.name for d in db.datasets()] == [BYBIT]
    assert db.rules("mine") == {}  # its rows went with it
    with pytest.raises(ValueError):
        db.delete(BYBIT)
    db.close()


def test_read_only_never_creates_anything(tmp_path: Path) -> None:
    assert HistoryDB.open(tmp_path, readonly=True) is None
    HistoryDB.open(tmp_path).close()  # type: ignore[union-attr]
    ro = HistoryDB.open(tmp_path, readonly=True)
    assert ro is not None
    with pytest.raises(KeyError):
        ro.dataset_id("new")
    ro.close()


# ------------------------------------------------------------ starter set


def test_top_symbols_by_turnover() -> None:
    async def tickers() -> list[dict[str, str]]:
        return [
            {"symbol": "AUSDT", "turnover24h": "5"},
            {"symbol": "BUSDC", "turnover24h": "99"},
            {"symbol": "CUSDT", "turnover24h": "50"},
            {"symbol": "DUSDT", "turnover24h": "1"},
        ]

    assert asyncio.run(top_symbols(2, loader=tickers)) == ["CUSDT", "AUSDT"]


def test_starter_set_skips_months_before_listing(tmp_path: Path) -> None:
    client = _Client([_bar(i, "10") for i in range(0, 3)], [])
    asyncio.run(
        download_history(
            ["ETHUSDT"],
            "8h",
            datetime(2026, 6, 1, tzinfo=UTC),
            _T0 + 3 * _8H,
            cache_dir=tmp_path,
            client=client,
            listings={"ETHUSDT": datetime(2026, 8, 20, tzinfo=UTC)},
        )
    )

    db = HistoryDB(tmp_path / "history.db", readonly=True)
    assert (
        db.fetched("ETHUSDT", "bars_8h", datetime(2026, 7, 1, tzinfo=UTC)) is None
    )  # skipped
    assert (
        db.fetched("ETHUSDT", "bars_8h", datetime(2026, 8, 1, tzinfo=UTC)) is not None
    )
    assert client.funding_calls[0][0] == datetime(2026, 8, 1, tzinfo=UTC)
    db.close()
