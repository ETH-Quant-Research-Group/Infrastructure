from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from engine.order.funding import FundingDB
from engine.order.performance import unrealized_at
from engine.strategy.guard import StrategyGuard
from engine.strategy.pnl_calc import PnLCalc

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from engine.order.ledger import GuardTrip, LedgerDB

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
    is cooperative, not a real boundary. This registry refuses to act on
    signals for any strategy that has breached its configured ``max_loss``
    — regardless of what the strategy's own code does or doesn't do
    internally.

    ``max_loss`` must be supplied via :meth:`configure` from a trusted
    source only (the manager, reflecting what the admin set on the
    /internal Deploy page) — never from the strategy itself. A strategy
    with no configured max_loss still gets ``default_max_loss`` the moment
    its first signal arrives, so nothing is ever fully unguarded.

    **Ledger mode** (``ledger`` given — what the consolidator worker uses):

    - The PnL checked is the strategy's own, from the ledger:
      realized + funding − fees, plus unrealized at live prices when
      ``include_unrealized`` (env ``GUARD_INCLUDE_UNREALIZED``).  Funding
      comes from ``funding.db``, so it lags by up to a day (the daily job).
    - It trips when that PnL, counted from the last reset (0 if never
      reset), falls below ``-max_loss``.
    - Checked after each of its fills (:meth:`record_fill`, at the prices
      of the last periodic check) and periodically (:meth:`check_all`,
      which fetches fresh prices).  If a needed price is missing, the check
      is skipped — it neither trips nor relaxes on an unknown number.
    - A trip **latches**: it's stored in the ledger (``guard_trips``),
      survives restarts, and only :meth:`reset` lifts it.  A reset re-arms
      the guard from the current PnL.

    **Legacy mode** (no ledger): realized PnL only, from an in-memory
    PnLCalc fed by fills, lost on restart — kept as it was.
    """

    def __init__(
        self,
        default_max_loss: Decimal,
        *,
        ledger: LedgerDB | None = None,
        funding_path: Path | None = None,
        prices: Callable[[str, str], Awaitable[Decimal | None]] | None = None,
        include_unrealized: bool = False,
    ) -> None:
        self._default_max_loss = default_max_loss
        self._entries: dict[str, _Entry] = {}
        self._notified: set[str] = set()  # already reported as tripped, once
        self._pending: list[str] = []  # tripped since the last pop_newly_tripped()

        self._ledger = ledger
        self._funding_path = funding_path
        self._funding: FundingDB | None = None
        self._prices = prices
        self._include_unrealized = include_unrealized
        self._price_cache: dict[tuple[str, str], Decimal | None] = {}
        self._tripped: dict[str, GuardTrip] = {}
        self._baselines: dict[str, Decimal] = {}
        if ledger is not None:
            self._tripped = ledger.active_trips()
            self._baselines = ledger.guard_baselines()
            # Already handled before the restart: don't stop containers again.
            self._notified = set(self._tripped)
            if self._tripped:
                log.warning("guard still TRIPPED for %s (from before restart)", list(self._tripped))
            if include_unrealized and prices is None:
                raise ValueError("include_unrealized needs a price source")

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
        entry = self._entry(strategy_id)
        if self._ledger is not None:
            return strategy_id not in self._tripped
        return entry.guard.is_active

    def active_ids(self) -> list[str]:
        """Every known strategy_id that is not currently halted — drives the
        periodic ``strategy.guard.<id>`` broadcast in consolidator_worker.py,
        which the Network page uses for its real ACTIVE/HALTED badge."""
        return [sid for sid in self._entries if self.is_active(sid)]

    def record_fill(
        self,
        strategy_id: str,
        symbol: str,
        exchange: str,
        qty_delta: Decimal,
        fill_price: Decimal,
    ) -> None:
        """A fill for *strategy_id* was recorded: check its guard.

        Ledger mode: re-check from the ledger (the fill is already in it).
        Legacy mode: feed the fill into the in-memory realized-PnL tracking.
        """
        if self._ledger is not None:
            self.check(strategy_id)
            return

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

    # --------------------------------------------------------- ledger mode

    def pnl(self, strategy_id: str) -> Decimal | None:
        """The PnL the guard checks for *strategy_id*, counted from its last
        reset; ``None`` if a needed price is missing."""
        assert self._ledger is not None
        if strategy_id not in self._ledger.strategy_ids():
            return Decimal(0)  # no orders yet
        snap = self._ledger.snapshot(strategy_id)
        funding = self._funding_db()
        pnl = (
            snap.realized_pnl
            + (funding.total(strategy_id) if funding else Decimal(0))
            - snap.fees_paid
        )
        if self._include_unrealized:
            unrealized = unrealized_at(snap.holdings, self._price_cache)
            if unrealized is None:
                return None
            pnl += unrealized
        return pnl - self._baselines.get(strategy_id, Decimal(0))

    def check(self, strategy_id: str) -> Decimal | None:
        """Check one strategy's guard now; returns the PnL it checked."""
        pnl = self.pnl(strategy_id)
        if pnl is None:
            log.debug("guard check skipped for %r: a price is missing", strategy_id)
            return None
        limit = self._entry(strategy_id).guard.max_loss
        if pnl < -limit and strategy_id not in self._tripped:
            self._trip(strategy_id, pnl, limit)
        return pnl

    async def check_all(self) -> None:
        """Fetch fresh prices (if unrealized counts) and check every
        strategy — the periodic check, so open positions losing value are
        caught without waiting for a fill."""
        assert self._ledger is not None
        names = set(self._entries) | set(self._ledger.strategies())
        if self._include_unrealized and self._prices is not None:
            wanted = {
                (h.exchange, h.symbol)
                for name in names
                if name in self._ledger.strategy_ids()
                for h in self._ledger.snapshot(name).holdings
                if h.quantity != 0
            }
            self._price_cache = {key: await self._prices(*key) for key in sorted(wanted)}
        for name in sorted(names):
            self.check(name)

    def _trip(self, strategy_id: str, pnl: Decimal, limit: Decimal) -> None:
        assert self._ledger is not None
        self._tripped[strategy_id] = self._ledger.record_trip(
            strategy_id, pnl, limit, self._include_unrealized
        )
        what = "net incl. unrealized" if self._include_unrealized else "realized + funding - fees"
        log.warning(
            "guard TRIPPED for strategy_id=%r: PnL %s (%s) below -%s — halting further signals",
            strategy_id, pnl, what, limit,
        )
        if strategy_id not in self._notified:
            self._notified.add(strategy_id)
            self._pending.append(strategy_id)

    def reset(self, strategy_id: str) -> Decimal:
        """Lift *strategy_id*'s trip; its guard re-arms from its current PnL.

        Returns the new baseline.  Raises ``ValueError`` if it isn't tripped
        or its PnL can't be priced right now.
        """
        assert self._ledger is not None
        if strategy_id not in self._tripped:
            raise ValueError(f"guard for {strategy_id!r} is not tripped")
        current = self.pnl(strategy_id)
        if current is None:
            raise ValueError(f"can't price {strategy_id!r} right now - try again")
        baseline = self._baselines.get(strategy_id, Decimal(0)) + current
        self._ledger.reset_trip(strategy_id, baseline)
        self._baselines[strategy_id] = baseline
        del self._tripped[strategy_id]
        self._notified.discard(strategy_id)
        log.warning("guard RESET for strategy_id=%r, re-armed from PnL %s", strategy_id, baseline)
        return baseline

    def describe_trip(self, strategy_id: str) -> str | None:
        """Why *strategy_id* is halted, for the ``BLOCKED`` report — or
        ``None`` if it isn't (or in legacy mode, which keeps no details)."""
        trip = self._tripped.get(strategy_id)
        if trip is None:
            return None
        what = "incl. unrealized" if trip.include_unrealized else "realized + funding - fees"
        return (
            f"guard tripped for {strategy_id} at {trip.tripped_at:%Y-%m-%d %H:%M} UTC"
            f" (PnL {trip.pnl}, {what}, below -{trip.max_loss})"
        )

    def status(self) -> list[dict[str, Any]]:
        """Every known strategy's guard: PnL checked, limit, trip — for
        ``workers/guard_control.py``."""
        assert self._ledger is not None
        out = []
        for name in sorted(set(self._entries) | set(self._ledger.strategies())):
            pnl = self.pnl(name)
            trip = self._tripped.get(name)
            out.append({
                "strategy": name,
                "pnl": None if pnl is None else str(pnl),
                "max_loss": str(self._entry(name).guard.max_loss),
                "baseline": str(self._baselines.get(name, Decimal(0))),
                "include_unrealized": self._include_unrealized,
                "tripped": trip is not None,
                "tripped_at": trip.tripped_at.isoformat() if trip else None,
                "tripped_pnl": str(trip.pnl) if trip else None,
            })
        return out

    def _funding_db(self) -> FundingDB | None:
        if self._funding is None and self._funding_path is not None and self._funding_path.exists():
            self._funding = FundingDB(self._funding_path, readonly=True)
        return self._funding

    def close(self) -> None:
        if self._funding is not None:
            self._funding.close()
