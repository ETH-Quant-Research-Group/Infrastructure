from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from engine.strategy.guard import StrategyGuard
from engine.strategy.pnl_calc import PnLCalc

log = logging.getLogger(__name__)


@dataclass
class _Entry:
    guard: StrategyGuard
    pnl_calc: PnLCalc = field(default_factory=PnLCalc)


class StrategyGuardRegistry:
    """Per-strategy risk enforcement, hosted in the consolidator instead of
    inside each strategy's own container.

    A deployed strategy image can skip its own in-process StrategyGuard
    entirely — nothing stops it publishing straight to NATS — so that guard
    is cooperative, not a real boundary. This registry re-derives each
    strategy's realized PnL from the SAME fills the consolidator itself
    produces (ground truth, since the consolidator is what actually calls
    the broker), and refuses to act on signals for any strategy that has
    breached its configured ``max_loss`` — regardless of what the strategy's
    own code does or doesn't do internally.

    ``max_loss`` must be supplied via :meth:`configure` from a trusted
    source only (the manager, reflecting what the admin set on the
    /internal Deploy page) — never from the strategy itself. A strategy
    with no configured max_loss still gets ``default_max_loss`` the moment
    its first signal arrives, so nothing is ever fully unguarded.
    """

    def __init__(self, default_max_loss: Decimal) -> None:
        self._default_max_loss = default_max_loss
        self._entries: dict[str, _Entry] = {}
        self._notified: set[str] = set()  # already reported as tripped, once
        self._pending: list[str] = []  # tripped since the last pop_newly_tripped()

    def configure(self, strategy_id: str, max_loss: Decimal) -> None:
        """Set (or update) the admin-configured max_loss for a strategy."""
        entry = self._entries.get(strategy_id)
        if entry is None:
            self._entries[strategy_id] = _Entry(guard=StrategyGuard(max_loss=max_loss))
        else:
            entry.guard.max_loss = max_loss

    def _entry(self, strategy_id: str) -> _Entry:
        entry = self._entries.get(strategy_id)
        if entry is None:
            entry = _Entry(guard=StrategyGuard(max_loss=self._default_max_loss))
            self._entries[strategy_id] = entry
            log.info(
                "auto-registered guard for unconfigured strategy_id=%r (max_loss=%s)",
                strategy_id,
                self._default_max_loss,
            )
        return entry

    @property
    def default_max_loss(self) -> Decimal:
        return self._default_max_loss

    def is_active(self, strategy_id: str) -> bool:
        return self._entry(strategy_id).guard.is_active

    def active_ids(self) -> list[str]:
        """Every known strategy_id that is not currently halted — drives the
        periodic ``strategy.guard.<id>`` broadcast in consolidator_worker.py,
        which the Network page uses for its real ACTIVE/HALTED badge."""
        return [sid for sid, entry in self._entries.items() if entry.guard.is_active]

    def record_fill(
        self,
        strategy_id: str,
        symbol: str,
        exchange: str,
        qty_delta: Decimal,
        fill_price: Decimal,
    ) -> None:
        """Feed a fill into this strategy's own realized-PnL tracking and
        guard — independent of anything the strategy's own container does."""
        entry = self._entry(strategy_id)
        before = entry.pnl_calc.total_realized
        entry.pnl_calc.on_fill(symbol, qty_delta, fill_price, exchange=exchange)
        delta = entry.pnl_calc.total_realized - before
        if delta != Decimal(0):
            entry.guard.record_pnl(delta)

        if not entry.guard.is_active and strategy_id not in self._notified:
            self._notified.add(strategy_id)
            self._pending.append(strategy_id)
            log.warning(
                "guard TRIPPED for strategy_id=%r — halting further signals",
                strategy_id,
            )

    def pop_newly_tripped(self) -> list[str]:
        """Strategy IDs that tripped since the last call — for the caller to
        act on (e.g. stop the container) exactly once each."""
        pending, self._pending = self._pending, []
        return pending
