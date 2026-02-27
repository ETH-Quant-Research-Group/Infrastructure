from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from datetime import datetime
    from decimal import Decimal

    from data.connectors.types import KlineInterval
    from data.types import DollarBar, TickBar, TimeBar, Trade, VolumeBar


class BaseClient(ABC):
    """Contract that every exchange data client must fulfill.

    Concrete implementations (e.g. ``BinanceClient``) hide all
    connector / normalizer wiring so callers only deal with canonical
    types from ``data.types``.
    """

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

    async def __aenter__(self) -> BaseClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
