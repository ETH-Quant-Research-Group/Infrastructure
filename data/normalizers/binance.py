from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from data.connectors.types import KlineInterval, RawKline, RawTrade
from data.types import TimeBar, Trade

# Nominal duration for each Binance kline interval.
# MO1 uses 30 days as an approximation.
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


def to_time_bar(raw: RawKline, *, symbol: str, interval: KlineInterval) -> TimeBar:
    """Convert a raw Binance kline into a canonical :class:`~data.types.TimeBar`.

    *symbol* must be supplied by the caller because Binance does not include
    it in the kline payload itself.
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


def to_trade(raw: RawTrade, *, symbol: str) -> Trade:
    """Convert a raw Binance trade into a canonical :class:`~data.types.Trade`."""
    return Trade(
        symbol=symbol,
        price=Decimal(raw["price"]),
        quantity=Decimal(raw["qty"]),
        timestamp=_ms_to_utc(raw["time"]),
        is_buyer_maker=raw["is_buyer_maker"],
    )


def _ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1_000, tz=UTC)
