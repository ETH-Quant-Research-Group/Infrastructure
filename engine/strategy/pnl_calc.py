from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal


@dataclass(frozen=True, kw_only=True)
class PnLSnapshot:
    """Immutable point-in-time PnL reading for one symbol."""

    timestamp: datetime
    symbol: str
    realized_pnl: Decimal
    unrealized_pnl: Decimal

    @property
    def total(self) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl


@dataclass
class _SymbolPosition:
    open_qty: Decimal = field(default_factory=lambda: Decimal(0))
    avg_entry_price: Decimal = field(default_factory=lambda: Decimal(0))
    realized_pnl: Decimal = field(default_factory=lambda: Decimal(0))
    last_unrealized: Decimal = field(default_factory=lambda: Decimal(0))


class PnLCalc:
    """Per-strategy PnL tracker that maintains its own position state.

    Tracks each symbol's open quantity, average entry price, and realized PnL
    independently of the broker.  This allows accurate per-strategy attribution
    even when multiple strategies trade the same symbol through the shared
    :class:`~engine.order.consolidator.OrderConsolidator`.

    Feed it from two sources:
    - :meth:`on_fill` — called by the consolidator whenever an order is placed
      for this strategy, with the signed quantity delta and fill price.
    - :meth:`update_market_price` — called by the runner on every bar to keep
      unrealized PnL current.
    """

    def __init__(self) -> None:
        self._positions: dict[str, _SymbolPosition] = {}
        self._latest: dict[str, PnLSnapshot] = {}
        self._history: list[PnLSnapshot] = []

    # ------------------------------------------------------------------ writes

    def on_fill(self, symbol: str, qty_delta: Decimal, fill_price: Decimal) -> None:
        """Record a fill for this strategy.

        ``qty_delta`` is signed: positive = bought, negative = sold.
        Updates avg_entry_price and realizes PnL for any position reduction.
        """
        if qty_delta == Decimal(0):
            return

        pos = self._positions.setdefault(symbol, _SymbolPosition())
        old_qty = pos.open_qty
        new_qty = old_qty + qty_delta

        if old_qty == Decimal(0):
            # Opening a fresh position
            pos.open_qty = qty_delta
            pos.avg_entry_price = fill_price

        elif (old_qty > 0) == (qty_delta > 0):
            # Adding to existing position — update weighted average entry
            total_cost = old_qty * pos.avg_entry_price + qty_delta * fill_price
            pos.open_qty = new_qty
            pos.avg_entry_price = total_cost / new_qty

        else:
            # Reducing or flipping position
            closed_qty = min(abs(qty_delta), abs(old_qty))
            if old_qty > 0:
                pos.realized_pnl += (fill_price - pos.avg_entry_price) * closed_qty
            else:
                pos.realized_pnl += (pos.avg_entry_price - fill_price) * closed_qty

            pos.open_qty = new_qty
            if new_qty == Decimal(0):
                pos.avg_entry_price = Decimal(0)
            elif (old_qty > 0) != (new_qty > 0):
                # Position flipped direction — new entry is at fill price
                pos.avg_entry_price = fill_price
            # Partially closed: avg_entry_price unchanged

        self._record_snapshot(symbol, pos, pos.last_unrealized)

    def update_market_price(self, symbol: str, price: Decimal) -> None:
        """Recompute unrealized PnL using the latest market price.

        Call this on every bar event for each symbol the strategy trades.
        """
        pos = self._positions.get(symbol)
        if pos is None or pos.open_qty == Decimal(0):
            return
        unrealized = self._unrealized(pos, price)
        pos.last_unrealized = unrealized
        self._record_snapshot(symbol, pos, unrealized)

    # ------------------------------------------------------------------ reads

    @property
    def total_realized(self) -> Decimal:
        return sum((p.realized_pnl for p in self._positions.values()), Decimal(0))

    @property
    def total_unrealized(self) -> Decimal:
        return sum((p.last_unrealized for p in self._positions.values()), Decimal(0))

    @property
    def total(self) -> Decimal:
        return self.total_realized + self.total_unrealized

    @property
    def history(self) -> list[PnLSnapshot]:
        """Full append-only list of snapshots, oldest first."""
        return list(self._history)

    def latest(self, symbol: str) -> PnLSnapshot | None:
        """Most recent snapshot for *symbol*, or ``None`` if not yet recorded."""
        return self._latest.get(symbol)

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _unrealized(pos: _SymbolPosition, price: Decimal) -> Decimal:
        if pos.open_qty > 0:
            return (price - pos.avg_entry_price) * pos.open_qty
        return (pos.avg_entry_price - price) * abs(pos.open_qty)

    def _record_snapshot(
        self, symbol: str, pos: _SymbolPosition, unrealized: Decimal
    ) -> None:
        snap = PnLSnapshot(
            timestamp=datetime.now(tz=UTC),
            symbol=symbol,
            realized_pnl=pos.realized_pnl,
            unrealized_pnl=unrealized,
        )
        self._latest[symbol] = snap
        self._history.append(snap)
