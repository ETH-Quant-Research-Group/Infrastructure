"""Where "now" comes from: the wall clock live, a simulated clock in a backtest.

Code that needs the current time and should behave the same in a backtest
asks a :class:`Clock` instead of calling ``datetime.now()`` — e.g.
strategies through :meth:`~interfaces.strategy.BaseStrategy.now`, and the
ledger through its ``now`` argument.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class WallClock:
    """The real time (UTC)."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class SimClock:
    """Simulated time: whatever the backtest sets.  It only moves forward,
    and only when the backtest moves it (event to event, no real waiting)."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        if when < self._now:
            raise ValueError(f"simulated time can't go back: {when} < {self._now}")
        self._now = when
