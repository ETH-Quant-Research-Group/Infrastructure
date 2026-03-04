from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeAlias

if TYPE_CHECKING:
    import asyncio

from data.types import DollarBar, FundingRate, TickBar, TimeBar, Trade, VolumeBar

BusEvent: TypeAlias = TimeBar | TickBar | VolumeBar | DollarBar | Trade | FundingRate


class BusProtocol(Protocol):
    """Structural interface satisfied by any bus implementation (e.g. NatsBus)."""

    def subscribe(self, *topics: str) -> asyncio.Queue[BusEvent]: ...

    def unsubscribe(self, queue: asyncio.Queue[BusEvent]) -> None: ...
