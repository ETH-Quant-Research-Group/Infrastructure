from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, kw_only=True)
class TargetPosition:
    """Trade signal emitted by a strategy.

    ``quantity`` is a signed trade **delta**: positive = buy, negative = sell,
    zero = no trade.  It is NOT a target position size — the consolidator
    accumulates these deltas to track each strategy's running position.

    ``price`` is the reference price at signal time, stamped by the runner
    from the last bar close.  Strategies can set it explicitly, but don't
    need to — the runner overwrites it before forwarding to the consolidator.
    Used for PnL attribution when trades internally offset and no broker
    order is placed.

    ``strategy_id`` is stamped by :class:`~engine.strategy.runner.StrategyRunner`,
    not by the strategy itself.
    """

    symbol: str
    quantity: Decimal
    price: Decimal = Decimal(0)
    strategy_id: str = ""
