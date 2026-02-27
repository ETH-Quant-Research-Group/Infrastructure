from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal
from enum import StrEnum
from typing import ClassVar


class BarType(StrEnum):
    TIME = "time"
    TICK = "tick"
    VOLUME = "volume"
    DOLLAR = "dollar"


@dataclass(frozen=True, kw_only=True)
class Trade:
    """Single executed trade — raw input for bar formation."""

    symbol: str
    price: Decimal
    quantity: Decimal
    timestamp: datetime
    is_buyer_maker: bool  # True → buyer is the maker (taker sold)


@dataclass(frozen=True, kw_only=True)
class Bar:
    """Base OHLCV bar.  Do not instantiate directly; use a concrete subclass."""

    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int
    timestamp: datetime  # bar open time (UTC)
    close_time: datetime  # bar close time (UTC)


@dataclass(frozen=True, kw_only=True)
class TimeBar(Bar):
    """Fixed-duration OHLCV bar (e.g. 60 s, 3 600 s).

    ``interval_seconds`` records the nominal duration so downstream code
    does not have to recompute it from ``timestamp`` / ``close_time``.
    """

    bar_type: ClassVar[BarType] = BarType.TIME
    interval_seconds: int


@dataclass(frozen=True, kw_only=True)
class TickBar(Bar):
    """OHLCV bar that closes after exactly ``tick_threshold`` trades."""

    bar_type: ClassVar[BarType] = BarType.TICK
    tick_threshold: int


@dataclass(frozen=True, kw_only=True)
class VolumeBar(Bar):
    """OHLCV bar that closes when cumulative base-asset volume crosses
    ``volume_threshold``."""

    bar_type: ClassVar[BarType] = BarType.VOLUME
    volume_threshold: Decimal


@dataclass(frozen=True, kw_only=True)
class DollarBar(Bar):
    """OHLCV bar that closes when cumulative dollar (quote-asset) volume
    crosses ``dollar_threshold``."""

    bar_type: ClassVar[BarType] = BarType.DOLLAR
    dollar_threshold: Decimal


AnyBar = TimeBar | TickBar | VolumeBar | DollarBar


@dataclass(frozen=True, kw_only=True)
class FundingRate:
    """A single funding-rate observation from a perpetual futures market.

    ``timestamp`` is the *settlement* time for historical records, or the
    event time for live snapshots from the mark-price WebSocket stream.
    ``next_funding_time`` is only populated for live snapshots.
    """

    symbol: str
    funding_rate: Decimal  # per-period rate, e.g. 0.0001 = 0.01 %
    mark_price: Decimal
    timestamp: datetime
    next_funding_time: datetime | None = None
