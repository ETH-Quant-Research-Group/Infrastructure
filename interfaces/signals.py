from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal


@dataclass(frozen=True, kw_only=True)
class TargetPosition:
    """What a strategy emits and what flows through the engine.

    ``quantity`` is signed: positive = long, negative = short, zero = flat.
    ``strategy_id`` is stamped by :class:`~engine.runner.StrategyRunner`,
    not by the strategy itself.
    """

    symbol: str
    quantity: Decimal
    strategy_id: str = ""
