from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from decimal import Decimal

from datetime import UTC, datetime

from data.connectors.binance_futures import BinanceFuturesConnector
from data.normalizers.bars import build_dollar_bars, build_tick_bars, build_volume_bars
from data.normalizers.binance import to_trade
from data.normalizers.binance_futures import (
    to_current_funding_rate,
    to_funding_rate,
    to_futures_time_bar,
)
from interfaces.client import BaseClient

if TYPE_CHECKING:
    from data.connectors.types import KlineInterval
    from data.types import DollarBar, FundingRate, TickBar, TimeBar, Trade, VolumeBar


class BinanceFuturesClient(BaseClient):
    """User-facing Binance USD-M Futures client.

    Wraps the futures connector and normalizer pipeline so callers only
    import this class and work with canonical types.

    Extends :class:`~interfaces.client.BaseClient` with futures-specific
    methods: :meth:`funding_rates`, :meth:`current_funding_rate`, and
    :meth:`live_funding_rates`.

    Usage::

        from clients.binance_futures import BinanceFuturesClient
        from data.connectors.types import KlineInterval
        from datetime import datetime, UTC

        async with BinanceFuturesClient() as client:
            # Historical OHLCV
            bars = await client.time_bars(
                "BTCUSDT",
                KlineInterval.H8,
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2025, 1, 1, tzinfo=UTC),
            )

            # Historical funding rates
            rates = await client.funding_rates(
                "BTCUSDT",
                start=datetime(2024, 1, 1, tzinfo=UTC),
            )

            # Live funding rate stream
            async for rate in client.live_funding_rates("BTCUSDT"):
                print(rate.funding_rate, rate.next_funding_time)
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._connector = BinanceFuturesConnector(api_key)

    # ---------------------------------------------------------------- historical

    async def time_bars(
        self,
        symbol: str,
        interval: KlineInterval,
        *,
        start: datetime,
        end: datetime,
    ) -> list[TimeBar]:
        """Fetch all closed futures time bars for *symbol* in [*start*, *end*]."""
        bars: list[TimeBar] = []
        async for raw in self._connector.fetch_klines(
            symbol,
            interval,
            start_ms=_to_ms(start),
            end_ms=_to_ms(end),
        ):
            bars.append(to_futures_time_bar(raw, symbol=symbol, interval=interval))
        return bars

    async def funding_rates(
        self,
        symbol: str,
        *,
        start: datetime,
        end: datetime | None = None,
    ) -> list[FundingRate]:
        """Fetch historical funding-rate settlements for *symbol*.

        Returns one :class:`~data.types.FundingRate` per 8-hour settlement
        period, from *start* up to *end* (or the present if *end* is omitted).
        Results are ordered oldest-first.
        """
        rates: list[FundingRate] = []
        async for raw in self._connector.fetch_funding_rates(
            symbol,
            start_ms=_to_ms(start),
            end_ms=_to_ms(end) if end is not None else None,
        ):
            rates.append(to_funding_rate(raw))
        return rates

    async def current_funding_rate(self, symbol: str) -> FundingRate:
        """Fetch the live mark price and most-recently-settled funding rate."""
        raw = await self._connector.fetch_current_funding_rate(symbol)
        return to_current_funding_rate(raw)

    async def volume_bars(
        self,
        symbol: str,
        threshold: Decimal,
        *,
        limit: int = 1_000,
    ) -> list[VolumeBar]:
        """Fetch recent futures trades and aggregate into volume bars."""
        trades = await self._recent_trades(symbol, limit=limit)
        return build_volume_bars(trades, threshold)

    async def tick_bars(
        self,
        symbol: str,
        threshold: int,
        *,
        limit: int = 1_000,
    ) -> list[TickBar]:
        """Fetch recent futures trades and aggregate into tick bars."""
        trades = await self._recent_trades(symbol, limit=limit)
        return build_tick_bars(trades, threshold)

    async def dollar_bars(
        self,
        symbol: str,
        threshold: Decimal,
        *,
        limit: int = 1_000,
    ) -> list[DollarBar]:
        """Fetch recent futures trades and aggregate into dollar bars."""
        trades = await self._recent_trades(symbol, limit=limit)
        return build_dollar_bars(trades, threshold)

    # ---------------------------------------------------------------------- live

    async def live_time_bars(
        self,
        symbol: str,
        interval: KlineInterval,
    ) -> AsyncGenerator[TimeBar, None]:
        """Stream live closed futures time bars over WebSocket."""
        async for raw in self._connector.stream_klines(symbol, interval):
            yield to_futures_time_bar(raw, symbol=symbol, interval=interval)

    async def live_funding_rates(
        self,
        symbol: str,
        *,
        update_speed: int = 3,
    ) -> AsyncGenerator[FundingRate, None]:
        """Stream live mark-price / funding-rate updates over WebSocket.

        Emits a :class:`~data.types.FundingRate` every *update_speed*
        seconds (1 or 3).  Each value reflects the *current* live funding
        rate (not yet settled) plus the scheduled settlement time.

        This is the preferred feed for a live funding-arb strategy because
        it lets the strategy react to intra-period funding rate changes
        without polling the REST API.
        """
        async for raw in self._connector.stream_mark_price(
            symbol, update_speed=update_speed
        ):
            yield to_current_funding_rate(raw)

    async def live_trades(
        self,
        symbol: str,
    ) -> AsyncGenerator[Trade, None]:
        """Stream live futures trades over WebSocket."""
        async for raw in self._connector.stream_trades(symbol):
            yield to_trade(raw, symbol=symbol)

    # ----------------------------------------------------------------- helpers

    async def _recent_trades(self, symbol: str, *, limit: int) -> list[Trade]:
        trades: list[Trade] = []
        async for raw in self._connector.fetch_trades(symbol, limit=limit):
            trades.append(to_trade(raw, symbol=symbol))
        return trades

    # ----------------------------------------------------------------- lifecycle

    async def aclose(self) -> None:
        await self._connector.aclose()


def _to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1_000)
