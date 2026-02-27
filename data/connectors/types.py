from __future__ import annotations

from enum import StrEnum
from typing import TypedDict


class KlineInterval(StrEnum):
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H8 = "8h"
    H12 = "12h"
    D1 = "1d"
    D3 = "3d"
    W1 = "1w"
    MO1 = "1M"


class RawKline(TypedDict):
    """Raw kline as returned by the Binance REST and WebSocket APIs.

    Prices and volumes are kept as strings to preserve the exact precision
    sent by the exchange; convert to ``Decimal`` in the normalizer layer.
    """

    open_time_ms: int
    open: str
    high: str
    low: str
    close: str
    volume: str  # base-asset volume
    close_time_ms: int
    quote_volume: str  # quote-asset volume
    trade_count: int
    taker_buy_volume: str
    taker_buy_quote_volume: str


class RawTrade(TypedDict):
    """Raw trade as returned by the Binance REST and WebSocket APIs."""

    id: int
    price: str
    qty: str
    quote_qty: str
    time: int  # millisecond timestamp
    is_buyer_maker: bool


class RawFundingRate(TypedDict):
    """Raw funding rate record from the Binance Futures REST API
    (``/fapi/v1/fundingRate``).

    Prices and rates are kept as strings to preserve exchange precision.
    """

    symbol: str
    funding_time_ms: int  # settlement timestamp in milliseconds
    funding_rate: str  # e.g. "0.00010000"
    mark_price: str  # mark price at settlement


class RawMarkPrice(TypedDict):
    """Raw mark-price snapshot from ``/fapi/v1/premiumIndex``.

    Represents the *current* (unsettled) funding rate together with the
    live mark and index prices.
    """

    symbol: str
    mark_price: str
    index_price: str
    last_funding_rate: str  # most recently settled rate
    next_funding_time_ms: int
    time_ms: int
