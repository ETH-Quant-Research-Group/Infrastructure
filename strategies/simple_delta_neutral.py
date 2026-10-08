"""Minimal delta-neutral position for infrastructure validation.

Opens one small perp-short + spot-long pair on a single symbol, then
holds indefinitely. No entry/exit logic, no funding thresholds, no
decisions beyond the initial hedge. The goal is a stable, long-lived
position whose fees, funding payments, and reporting behaviour can be
observed cleanly without strategy-level decisions muddying the signal.

Stop manually (``systemctl --user stop qrf-strategy``) when done.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

from interfaces.signals import TargetPosition
from interfaces.strategy import BaseStrategy

if TYPE_CHECKING:
    from data.types import AnyBar
    from execution.types import FillConfirmation

log = logging.getLogger(__name__)


class SimpleDeltaNeutralStrategy(BaseStrategy):
    # --- Config: tweak here, don't branch logic ---
    SYMBOL: ClassVar[str] = "LINKUSDT"
    QUANTITY: ClassVar[Decimal] = Decimal("50")   # ~$650 per leg at current LINK price
    display_name: ClassVar[str] = "SimpleDN-LINK"

    topics: ClassVar[list[str]] = [
        "futures.LINKUSDT.bars.1m",       # any periodic event works as a trigger
        "futures.LINKUSDT.funding_rate",  # for dashboard observability
    ]

    max_loss: ClassVar[Decimal] = Decimal("200")

    def __init__(self) -> None:
        self._perp_opened = False
        self._spot_opened = False

    # --- Lifecycle ---

    def on_start(self) -> None:
        log.info(
            "[SimpleDN] started — will open %s %s delta-neutral on first bar",
            self.QUANTITY, self.SYMBOL,
        )

    def on_bar(self, bar: AnyBar) -> TargetPosition | None:
        if self._perp_opened:
            return None
        self._perp_opened = True
        log.info(
            "[SimpleDN] opening perp short %s %s near %s",
            self.QUANTITY, self.SYMBOL, bar.close,
        )
        return TargetPosition(
            symbol=self.SYMBOL,
            quantity=-self.QUANTITY,
            exchange="bybit_demo",
        )

    def on_fill(self, fill: FillConfirmation) -> TargetPosition | None:
        # When the perp short lands, open the matching spot long to hedge.
        if fill.exchange == "bybit_demo" and not self._spot_opened:
            self._spot_opened = True
            log.info(
                "[SimpleDN] perp filled @ %s — placing spot long hedge",
                fill.fill_price,
            )
            return TargetPosition(
                symbol=self.SYMBOL,
                quantity=self.QUANTITY,
                exchange="bybit_spot",
            )
        if fill.exchange == "bybit_spot":
            log.info("[SimpleDN] hedged — holding indefinitely, no further decisions")
        return None

    # --- Restart safety: don't re-open if we already hold the position ---

    def restore_from_positions(self, perp_positions, spot_positions) -> None:
        for p in perp_positions:
            if p.symbol == self.SYMBOL and p.quantity != Decimal(0):
                self._perp_opened = True
                log.info("[SimpleDN] perp already open on restart (qty=%s) — not re-entering", p.quantity)
                break
        for p in spot_positions:
            if p.symbol == self.SYMBOL and p.quantity > Decimal(0):
                self._spot_opened = True
                log.info("[SimpleDN] spot already open on restart (qty=%s)", p.quantity)
                break

    # --- Dashboard observability ---

    def heartbeat_state(self) -> dict:
        return {
            "name": self.__class__.__name__,
            "display_name": self.display_name,
            "symbols": {
                self.SYMBOL: {
                    "fill_state": "hedged" if (self._perp_opened and self._spot_opened) else "entering" if self._perp_opened else "flat",
                    "perp_opened": self._perp_opened,
                    "spot_opened": self._spot_opened,
                    "target_qty": str(self.QUANTITY),
                },
            },
        }
