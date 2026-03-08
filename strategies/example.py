from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

from interfaces.strategy import BaseStrategy

if TYPE_CHECKING:
    from data.types import AnyBar, FundingRate
    from interfaces.signals import TargetPosition

log = logging.getLogger(__name__)


class ExampleStrategy(BaseStrategy):
    topics: ClassVar[list[str]] = [
        "futures.BTCUSDT.bars.1m",
        "futures.BTCUSDT.funding_rate",
    ]

    max_loss: ClassVar[Decimal] = Decimal("500")

    def __init__(self) -> None:
        self._long = True  # alternates each bar

    def on_bar(self, bar: AnyBar) -> TargetPosition | None:
        from decimal import Decimal

        from interfaces.signals import TargetPosition

        qty = Decimal("0.001") if self._long else Decimal("-0.001")
        self._long = not self._long

        log.info(
            "[%s] BAR      close=%-12s  target_qty=%s",
            self.__class__.__name__,
            bar.close,
            qty,
        )
        return TargetPosition(symbol="BTCUSDT", quantity=qty, exchange="bybit_demo")

    def on_funding_rate(self, rate: FundingRate) -> TargetPosition | None:
        log.info(
            "[%s] FUNDING  rate=%-10s  next=%s",
            self.__class__.__name__,
            rate.funding_rate,
            rate.next_funding_time,
        )
        return None
