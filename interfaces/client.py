from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from datetime import datetime
    from decimal import Decimal

    from data.connectors.types import KlineInterval
    from data.types import DollarBar, FundingRate, TickBar, TimeBar, Trade, VolumeBar


class BaseCryptoClient(ABC):
    """Contract that every exchange data client must fulfill.

    Concrete implementations (e.g. ``BinanceClient``) hide all
    connector / normalizer wiring so callers only deal with canonical
    types from ``data.types``.
    """

    symbols: ClassVar[list[str]] = []
    """Symbols this client streams live data for (e.g. ``["BTCUSDT"]``)."""

    intervals: ClassVar[list[KlineInterval]] = []
    """Bar intervals to stream for each symbol (e.g. ``[KlineInterval.M1]``)."""

    # ---------------------------------------------------------------- historical

    @abstractmethod
    async def time_bars(
        self,
        symbol: str,
        interval: KlineInterval,
        *,
        start: datetime,
        end: datetime,
    ) -> list[TimeBar]: ...

    @abstractmethod
    async def volume_bars(
        self,
        symbol: str,
        threshold: Decimal,
        *,
        limit: int = 1_000,
    ) -> list[VolumeBar]: ...

    @abstractmethod
    async def tick_bars(
        self,
        symbol: str,
        threshold: int,
        *,
        limit: int = 1_000,
    ) -> list[TickBar]: ...

    @abstractmethod
    async def dollar_bars(
        self,
        symbol: str,
        threshold: Decimal,
        *,
        limit: int = 1_000,
    ) -> list[DollarBar]: ...

    # ---------------------------------------------------------------------- live

    @abstractmethod
    def live_time_bars(
        self,
        symbol: str,
        interval: KlineInterval,
    ) -> AsyncGenerator[TimeBar, None]: ...

    @abstractmethod
    def live_trades(
        self,
        symbol: str,
    ) -> AsyncGenerator[Trade, None]: ...

    # ----------------------------------------------------------------- lifecycle

    @abstractmethod
    async def aclose(self) -> None: ...

    async def __aenter__(self) -> BaseCryptoClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


class BaseCryptoFuturesClient(BaseCryptoClient):
    """Extends ``BaseCryptoClient`` with perpetual-futures-specific streams.

    Subclass this for any futures exchange client (e.g. ``BinanceFuturesClient``).
    Spot clients that don't have funding rates should subclass
    ``BaseCryptoClient`` directly.
    """

    @abstractmethod
    def live_funding_rates(
        self,
        symbol: str,
    ) -> AsyncGenerator[FundingRate, None]: ...
