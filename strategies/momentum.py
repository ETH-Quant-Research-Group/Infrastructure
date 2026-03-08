from __future__ import annotations

import logging
import os
from collections import deque
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

from interfaces.strategy import BaseStrategy

if TYPE_CHECKING:
    from data.types import AnyBar
    from interfaces.signals import TargetPosition

log = logging.getLogger(__name__)

_SYMBOL: str = os.getenv("STRATEGY_SYMBOL", "BTCUSDT")


class MomentumStrategy(BaseStrategy):
    """Dead-simple momentum strategy for pipeline testing.

    Goes long when the latest close is above the N-bar SMA, short when below.
    Only emits a TargetPosition delta when the position direction changes.

    Set STRATEGY_SYMBOL env var to target a different symbol (default BTCUSDT).
    """

    topics: ClassVar[list[str]] = [f"futures.{_SYMBOL}.bars.1m"]
    max_loss: ClassVar[Decimal] = Decimal("500")

    def __init__(
        self,
        symbol: str = _SYMBOL,
        lookback: int = 3,
        quantity: Decimal = Decimal("0.001"),
    ) -> None:
        self.symbol = symbol
        self._lookback = lookback
        self._quantity = quantity
        self._closes: deque[Decimal] = deque(maxlen=lookback + 1)
        self._current_side: int = 0  # 1 = long, -1 = short, 0 = flat

    def on_start(self) -> None:
        log.info(
            "[Momentum:%s] started  lookback=%d  qty=%s",
            self.symbol,
            self._lookback,
            self._quantity,
        )

    def on_stop(self) -> None:
        log.info("[Momentum:%s] stopped", self.symbol)

    def on_bar(self, bar: AnyBar) -> TargetPosition | None:
        if bar.symbol != self.symbol:
            return None

        self._closes.append(bar.close)
        log.info(
            "[Momentum:%s] BAR  close=%s  buffered=%d",
            self.symbol,
            bar.close,
            len(self._closes),
        )

        if len(self._closes) < self._lookback + 1:
            log.info(
                "[Momentum:%s] warming up (%d/%d bars)",
                self.symbol,
                len(self._closes),
                self._lookback + 1,
            )
            return None

        sma = sum(self._closes) / len(self._closes)
        latest = self._closes[-1]
        target_side = 1 if latest > sma else -1

        log.info(
            "[Momentum:%s] close=%s  sma=%s  signal=%s",
            self.symbol,
            latest,
            round(sma, 2),
            "LONG" if target_side == 1 else "SHORT",
        )

        if target_side == self._current_side:
            return None

        return self._flip(target_side)

    def _flip(self, target_side: int) -> TargetPosition:
        from interfaces.signals import TargetPosition

        # Delta needed: close old position + open new one
        delta = (
            Decimal(target_side) * self._quantity
            - Decimal(self._current_side) * self._quantity
        )
        self._current_side = target_side

        log.info(
            "[Momentum:%s] SIGNAL  side=%s  delta=%s",
            self.symbol,
            "LONG" if target_side == 1 else "SHORT",
            delta,
        )
        return TargetPosition(symbol=self.symbol, quantity=delta, exchange="bybit_demo")
