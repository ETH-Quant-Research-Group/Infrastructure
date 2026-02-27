from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from data.connectors.types import KlineInterval, RawFundingRate, RawKline, RawMarkPrice
from data.types import FundingRate, TimeBar

# Nominal duration for each kline interval — mirrors data.normalizers.binance
# but kept here so this module has no dependency on the spot normalizer.
_INTERVAL_SECONDS: dict[KlineInterval, int] = {
    KlineInterval.M1: 60,
    KlineInterval.M3: 180,
    KlineInterval.M5: 300,
    KlineInterval.M15: 900,
    KlineInterval.M30: 1_800,
    KlineInterval.H1: 3_600,
    KlineInterval.H2: 7_200,
    KlineInterval.H4: 14_400,
    KlineInterval.H6: 21_600,
    KlineInterval.H8: 28_800,
    KlineInterval.H12: 43_200,
    KlineInterval.D1: 86_400,
    KlineInterval.D3: 259_200,
    KlineInterval.W1: 604_800,
    KlineInterval.MO1: 2_592_000,
}


def to_futures_time_bar(
    raw: RawKline, *, symbol: str, interval: KlineInterval
) -> TimeBar:
    """Convert a raw Binance futures kline into a :class:`~data.types.TimeBar`.

    The futures kline payload is structurally identical to spot, so this
    function mirrors :func:`data.normalizers.binance.to_time_bar` exactly.
    It is provided as a separate entry-point so import paths make the data
    source explicit.
    """
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
        interval_seconds=_INTERVAL_SECONDS[interval],
    )


def to_funding_rate(raw: RawFundingRate) -> FundingRate:
    """Convert a historical funding-rate REST record into a canonical
    :class:`~data.types.FundingRate`.
    """
    return FundingRate(
        symbol=raw["symbol"],
        funding_rate=Decimal(raw["funding_rate"]),
        mark_price=Decimal(raw["mark_price"]),
        timestamp=_ms_to_utc(raw["funding_time_ms"]),
    )


def to_current_funding_rate(raw: RawMarkPrice) -> FundingRate:
    """Convert a live mark-price snapshot into a canonical
    :class:`~data.types.FundingRate`.

    Populates :attr:`~data.types.FundingRate.next_funding_time` from the
    ``next_funding_time_ms`` field, which is only present on live snapshots.
    """
    return FundingRate(
        symbol=raw["symbol"],
        funding_rate=Decimal(raw["last_funding_rate"]),
        mark_price=Decimal(raw["mark_price"]),
        timestamp=_ms_to_utc(raw["time_ms"]),
        next_funding_time=_ms_to_utc(raw["next_funding_time_ms"]),
    )


def _ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1_000, tz=UTC)
