from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data.connectors.types import (
        KlineInterval,
        RawFundingRate,
        RawKline,
        RawMarkPrice,
    )

from data.normalizers.binance import _ms_to_utc, to_time_bar
from data.types import FundingRate, TimeBar


def to_futures_time_bar(
    raw: RawKline, *, symbol: str, interval: KlineInterval
) -> TimeBar:
    """Convert a raw Binance futures kline into a :class:`~data.types.TimeBar`.

    The futures kline payload is structurally identical to spot, so this
    delegates directly to :func:`data.normalizers.binance.to_time_bar`.
    """
    return to_time_bar(raw, symbol=symbol, interval=interval)


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
