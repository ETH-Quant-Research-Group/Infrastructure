"""Bybit linear futures client — wraps the connector + normalizer pipeline.

Drop-in replacement for ``BinanceFuturesClient``.  Extends
:class:`~interfaces.client.BaseCryptoFuturesClient` so the datafeed server
discovers it automatically via ``discover_subclasses``.

Usage::

    STRATEGY_NAME=FundingArbBybitStrategy python -m workers.strategy_worker

The datafeed server will stream 8h bars and funding rates from Bybit
instead of Binance — same canonical types, same NATS subjects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from data.connectors.bybit_linear import BybitLinearConnector
from data.connectors.types import KlineInterval
from data.normalizers.bybit_linear import to_current_funding_rate, to_funding_rate, to_time_bar
from interfaces.client import BaseCryptoFuturesClient

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from datetime import datetime
    from decimal import Decimal

    from data.types import DollarBar, FundingRate, TickBar, TimeBar, Trade, VolumeBar


class BybitLinearClient(BaseCryptoFuturesClient):
    """User-facing Bybit linear futures client.

    Replaces BinanceFuturesClient for all data feeds:
    - Historical + live klines (including synthesised 8h bars)
    - Historical + live funding rates (via tickers WebSocket)
    """

    symbols: ClassVar[list[str]] = ["ETHUSDT", "LINKUSDT"]
    intervals: ClassVar[list[KlineInterval]] = [KlineInterval.H8]

    def __init__(self) -> None:
        self._connector = BybitLinearConnector()

    # ---------------------------------------------------------------- historical

    async def time_bars(
        self,
        symbol: str,
        interval: KlineInterval,
        *,
        start: datetime,
        end: datetime,
    ) -> list[TimeBar]:
        from datetime import UTC

        bars: list[TimeBar] = []
        async for raw in self._connector.fetch_klines(
            symbol, interval,
            start_ms=_to_ms(start),
            end_ms=_to_ms(end),
        ):
            bars.append(to_time_bar(raw, symbol=symbol, interval=interval))
        return bars

    async def funding_rates(
        self,
        symbol: str,
        *,
        start: datetime,
        end: datetime | None = None,
    ) -> list[FundingRate]:
        rates: list[FundingRate] = []
        async for raw in self._connector.fetch_funding_rates(
            symbol,
            start_ms=_to_ms(start),
            end_ms=_to_ms(end) if end is not None else None,
        ):
            rates.append(to_funding_rate(raw))
        return rates

    async def volume_bars(self, symbol: str, threshold: Decimal, *, limit: int = 1_000) -> list[VolumeBar]:
        return []  # not needed for funding arb

    async def tick_bars(self, symbol: str, threshold: int, *, limit: int = 1_000) -> list[TickBar]:
        return []  # not needed for funding arb

    async def dollar_bars(self, symbol: str, threshold: Decimal, *, limit: int = 1_000) -> list[DollarBar]:
        return []  # not needed for funding arb

    # ---------------------------------------------------------------------- live

    async def live_time_bars(
        self,
        symbol: str,
        interval: KlineInterval,
    ) -> AsyncGenerator[TimeBar, None]:
        async for raw in self._connector.stream_klines(symbol, interval):
            yield to_time_bar(raw, symbol=symbol, interval=interval)

    async def live_funding_rates(
        self,
        symbol: str,
    ) -> AsyncGenerator[FundingRate, None]:
        async for raw in self._connector.stream_tickers(symbol):
            yield to_current_funding_rate(raw)

    async def live_trades(
        self,
        symbol: str,
    ) -> AsyncGenerator[Trade, None]:
        # Not implemented — funding arb doesn't use trade streams.
        return
        yield  # make it an async generator

    # ----------------------------------------------------------------- lifecycle

    async def aclose(self) -> None:
        await self._connector.aclose()


def _to_ms(dt: datetime) -> int:
    from datetime import UTC

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1_000)
