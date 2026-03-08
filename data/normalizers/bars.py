from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

from decimal import Decimal

from data.types import DollarBar, TickBar, Trade, VolumeBar


def build_volume_bars(
    trades: Iterable[Trade],
    threshold: Decimal,
) -> list[VolumeBar]:
    """Aggregate *trades* into volume bars.

    A new bar is started whenever the cumulative base-asset volume of the
    current bar reaches *threshold*.  Any remaining (unconsumed) trades at
    the end of the iterable are discarded — callers that need partial bars
    should handle that at a higher level.
    """
    bars: list[VolumeBar] = []
    bucket: list[Trade] = []
    cum_volume = Decimal(0)

    for trade in trades:
        bucket.append(trade)
        cum_volume += trade.quantity
        if cum_volume >= threshold:
            bars.append(_make_volume_bar(bucket, threshold))
            bucket = []
            cum_volume = Decimal(0)

    return bars


def build_tick_bars(
    trades: Iterable[Trade],
    threshold: int,
) -> list[TickBar]:
    """Aggregate *trades* into tick bars (one bar per *threshold* trades)."""
    bars: list[TickBar] = []
    bucket: list[Trade] = []

    for trade in trades:
        bucket.append(trade)
        if len(bucket) >= threshold:
            bars.append(_make_tick_bar(bucket, threshold))
            bucket = []

    return bars


def build_dollar_bars(
    trades: Iterable[Trade],
    threshold: Decimal,
) -> list[DollarBar]:
    """Aggregate *trades* into dollar bars.

    A new bar starts whenever the cumulative quote-asset value (price × qty)
    of the current bar reaches *threshold*.
    """
    bars: list[DollarBar] = []
    bucket: list[Trade] = []
    cum_dollar = Decimal(0)

    for trade in trades:
        bucket.append(trade)
        cum_dollar += trade.price * trade.quantity
        if cum_dollar >= threshold:
            bars.append(_make_dollar_bar(bucket, threshold))
            bucket = []
            cum_dollar = Decimal(0)

    return bars


# ------------------------------------------------------------------ helpers


def _make_volume_bar(trades: list[Trade], threshold: Decimal) -> VolumeBar:
    return VolumeBar(
        symbol=trades[0].symbol,
        open=trades[0].price,
        high=max(t.price for t in trades),
        low=min(t.price for t in trades),
        close=trades[-1].price,
        volume=sum((t.quantity for t in trades), Decimal(0)),
        trade_count=len(trades),
        timestamp=trades[0].timestamp,
        close_time=trades[-1].timestamp,
        volume_threshold=threshold,
    )


def _make_tick_bar(trades: list[Trade], threshold: int) -> TickBar:
    return TickBar(
        symbol=trades[0].symbol,
        open=trades[0].price,
        high=max(t.price for t in trades),
        low=min(t.price for t in trades),
        close=trades[-1].price,
        volume=sum((t.quantity for t in trades), Decimal(0)),
        trade_count=len(trades),
        timestamp=trades[0].timestamp,
        close_time=trades[-1].timestamp,
        tick_threshold=threshold,
    )


def _make_dollar_bar(trades: list[Trade], threshold: Decimal) -> DollarBar:
    return DollarBar(
        symbol=trades[0].symbol,
        open=trades[0].price,
        high=max(t.price for t in trades),
        low=min(t.price for t in trades),
        close=trades[-1].price,
        volume=sum((t.quantity for t in trades), Decimal(0)),
        trade_count=len(trades),
        timestamp=trades[0].timestamp,
        close_time=trades[-1].timestamp,
        dollar_threshold=threshold,
    )
