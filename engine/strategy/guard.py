from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class StrategyGuard:
    """Per-strategy risk monitor.

    Tracks cumulative realised P&L and halts the strategy when ``max_loss``
    is breached.

    Example::

        guard = StrategyGuard(max_loss=Decimal("500"))
        guard.record_pnl(Decimal("-600"))
        assert not guard.is_active
    """

    max_loss: Decimal  # positive threshold, e.g. Decimal("500")
    _total_pnl: Decimal = field(default=Decimal(0), init=False, repr=False)

    def record_pnl(self, delta: Decimal) -> None:
        """Accumulate *delta* into the running P&L total."""
        self._total_pnl += delta

    @property
    def is_active(self) -> bool:
        """``False`` once cumulative losses exceed ``max_loss``."""
        return self._total_pnl >= -self.max_loss
