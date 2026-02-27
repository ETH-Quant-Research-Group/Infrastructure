from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from decimal import Decimal

from datetime import UTC, datetime

from data.connectors.binance import BinanceConnector
from data.normalizers.bars import build_dollar_bars, build_tick_bars, build_volume_bars
from data.normalizers.binance import to_time_bar, to_trade
from interfaces.client import BaseClient

if TYPE_CHECKING:
    from data.connectors.types import KlineInterval
    from data.types import DollarBar, TickBar, TimeBar, Trade, VolumeBar


class BinanceClient(BaseClient):
    """User-facing Binance client.

    Wraps the connector and normalizer pipeline so callers only import
    this class and work with canonical types.

    Usage::

        from clients.binance import BinanceClient
        from data.connectors.types import KlineInterval
        from datetime import datetime

        async with BinanceClient() as client:
            bars = await client.time_bars(
                "BTCUSDT",
                KlineInterval.H1,
                start=datetime(2024, 1, 1),
                end=datetime(2024, 2, 1),
            )
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._connector = BinanceConnector(api_key)

    # ---------------------------------------------------------------- historical

    async def time_bars(
        self,
        symbol: str,
        interval: KlineInterval,
        *,
        start: datetime,
        end: datetime,
    ) -> list[TimeBar]:
        """Fetch all closed time bars for *symbol* in [*start*, *end*]."""
        bars: list[TimeBar] = []
        async for raw in self._connector.fetch_klines(
            symbol,
            interval,
            start_ms=_to_ms(start),
            end_ms=_to_ms(end),
        ):
            bars.append(to_time_bar(raw, symbol=symbol, interval=interval))
        return bars

    async def volume_bars(
        self,
        symbol: str,
        threshold: Decimal,
        *,
        limit: int = 1_000,
    ) -> list[VolumeBar]:
        """Fetch recent trades and aggregate into volume bars."""
        trades = await self._recent_trades(symbol, limit=limit)
        return build_volume_bars(trades, threshold)

    async def tick_bars(
        self,
        symbol: str,
        threshold: int,
        *,
        limit: int = 1_000,
    ) -> list[TickBar]:
        """Fetch recent trades and aggregate into tick bars."""
        trades = await self._recent_trades(symbol, limit=limit)
        return build_tick_bars(trades, threshold)

    async def dollar_bars(
        self,
        symbol: str,
        threshold: Decimal,
        *,
        limit: int = 1_000,
    ) -> list[DollarBar]:
        """Fetch recent trades and aggregate into dollar bars."""
        trades = await self._recent_trades(symbol, limit=limit)
        return build_dollar_bars(trades, threshold)

    # ---------------------------------------------------------------------- live

    async def live_time_bars(
        self,
        symbol: str,
        interval: KlineInterval,
    ) -> AsyncGenerator[TimeBar, None]:
        """Stream live closed time bars over WebSocket."""
        async for raw in self._connector.stream_klines(symbol, interval):
            yield to_time_bar(raw, symbol=symbol, interval=interval)

    async def live_trades(
        self,
        symbol: str,
    ) -> AsyncGenerator[Trade, None]:
        """Stream live trades over WebSocket."""
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
