"""Normalizers for Bybit linear futures data → canonical types."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from data.types import FundingRate, TimeBar

if TYPE_CHECKING:
    from data.connectors.types import KlineInterval, RawFundingRate, RawKline, RawMarkPrice

# Nominal duration for each interval in seconds.
_INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1_800,
    "1h": 3_600, "2h": 7_200, "4h": 14_400, "6h": 21_600,
    "8h": 28_800, "12h": 43_200, "1d": 86_400, "1w": 604_800,
}


def _ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1_000, tz=UTC)


def to_time_bar(raw: RawKline, *, symbol: str, interval: KlineInterval) -> TimeBar:
    """Convert a raw Bybit kline into a canonical TimeBar."""
    return TimeBar(
        symbol=symbol,
        open=Decimal(raw["open"]),
        high=Decimal(raw["high"]),
        low=Decimal(raw["low"]),
        close=Decimal(raw["close"]),
        volume=Decimal(raw["volume"]),
        trade_count=raw["trade_count"],
        timestamp=_ms_to_utc(raw["open_time_ms"]),
        close_time=_ms_to_utc(raw["close_time_ms"]),
        interval_seconds=_INTERVAL_SECONDS.get(interval.value, 28_800),
    )


def to_funding_rate(raw: RawFundingRate) -> FundingRate:
    """Convert a historical Bybit funding rate record into a canonical FundingRate."""
    return FundingRate(
        symbol=raw["symbol"],
        funding_rate=Decimal(raw["funding_rate"]),
        mark_price=Decimal(raw["mark_price"]) if raw["mark_price"] != "0" else Decimal(0),
        timestamp=_ms_to_utc(raw["funding_time_ms"]),
    )


def to_current_funding_rate(raw: RawMarkPrice) -> FundingRate:
    """Convert a live Bybit mark-price/ticker snapshot into a canonical FundingRate."""
    return FundingRate(
        symbol=raw["symbol"],
        funding_rate=Decimal(raw["last_funding_rate"]),
        mark_price=Decimal(raw["mark_price"]),
        timestamp=_ms_to_utc(raw["time_ms"]),
        next_funding_time=_ms_to_utc(raw["next_funding_time_ms"])
        if raw["next_funding_time_ms"]
        else None,
    )
