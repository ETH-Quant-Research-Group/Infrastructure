"""Funding Rate Arbitrage — Bybit delta-neutral (perp short + spot long hedge).

Runs on ETHUSDT and LINKUSDT simultaneously using Bybit's 8h settlement.

Strategy:
  - Shorts the perp on Bybit linear (exchange="bybit_demo") to collect funding.
  - After the perp fill is confirmed, buys the same quantity on Bybit spot
    (exchange="bybit_spot") to remain delta-neutral.
  - On exit, closes the perp first; after that fill is confirmed, closes spot.

Parameters (optimised on 8h LINK+ETH backtest 2020–2026):
  entry_threshold  7 % annualised
  exit_threshold   5 % annualised
  min_consecutive  1 positive period
  min_hold         1 period  (8 h)
  max_hold         6 periods (48 h)
  position_size    50 % of equity per symbol

Run with::

    STRATEGY_NAME=FundingArbBybitStrategy python -m workers.strategy_worker
"""

from __future__ import annotations

import logging
import math
from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

from interfaces.strategy import BaseStrategy

if TYPE_CHECKING:
    from data.types import AnyBar, FundingRate
    from execution.types import FillConfirmation
    from interfaces.signals import TargetPosition

log = logging.getLogger(__name__)


_SYMBOLS = ["ETHUSDT", "LINKUSDT"]


class FundingArbBybitStrategy(BaseStrategy):
    """Delta-neutral funding arb on Bybit: perp short + spot long hedge.

    Manages ETHUSDT and LINKUSDT independently.  Each symbol runs its own
    state machine; fills from the perp leg trigger the spot hedge via
    :meth:`on_fill`.
    """

    display_name: ClassVar[str] = "FARB2.0-Bybit"

    topics: ClassVar[list[str]] = [
        "futures.ETHUSDT.bars.8h",
        "futures.ETHUSDT.funding_rate",
        "futures.LINKUSDT.bars.8h",
        "futures.LINKUSDT.funding_rate",
    ]
    max_loss: ClassVar[Decimal] = Decimal("500")

    def __init__(
        self,
        initial_equity: float = 30_000.0,
        position_size_pct: float = 0.50,
        entry_threshold_annualized: float = 0.07,
        exit_threshold_annualized: float = 0.05,
        min_consecutive_positive: int = 1,
        min_hold_periods: int = 1,       # 1 × 8 h = 8 h minimum hold
        max_hold_periods: int = 6,       # 6 × 8 h = 48 h hard cap
        max_volatility: float = 1.00,
        stop_loss_pct: float = 0.05,
        use_price_stop: bool = True,
    ) -> None:
        self._equity = initial_equity
        self._params = {
            "position_size_pct": position_size_pct,
            "entry_threshold_ann": entry_threshold_annualized,
            "exit_threshold_ann": exit_threshold_annualized,
            "min_consecutive": min_consecutive_positive,
            "min_hold": min_hold_periods,
            "max_hold": max_hold_periods,
            "max_volatility": max_volatility,
            "stop_loss_pct": stop_loss_pct,
            "use_price_stop": use_price_stop,
            "period_hours": 8,
        }

        # Per-symbol state -------------------------------------------------
        # fill_state: "flat" | "entering" | "hedged" | "exiting"
        self._fill_state: dict[str, str] = {s: "flat" for s in _SYMBOLS}
        self._perp_qty: dict[str, float] = {s: 0.0 for s in _SYMBOLS}   # < 0 = short
        self._spot_qty: dict[str, float] = {s: 0.0 for s in _SYMBOLS}   # > 0 = long
        self._entry_price: dict[str, float] = {s: 0.0 for s in _SYMBOLS}
        self._holding_periods: dict[str, int] = {s: 0 for s in _SYMBOLS}
        self._last_period_time: dict[str, datetime | None] = {s: None for s in _SYMBOLS}
        self._cum_funding: dict[str, float] = {s: 0.0 for s in _SYMBOLS}

        # Rolling buffers for signal filters
        self._bar_closes: dict[str, deque[float]] = {
            s: deque(maxlen=21) for s in _SYMBOLS
        }
        self._funding_rates: dict[str, deque[float]] = {
            s: deque(maxlen=24) for s in _SYMBOLS
        }

    # ------------------------------------------------------------------
    # State recovery — call after construction, before on_start
    # ------------------------------------------------------------------

    def restore_from_positions(
        self,
        perp_positions: list,
        spot_positions: list,
    ) -> None:
        """Restore fill_state from live broker positions after a restart.

        Prevents the strategy from re-entering positions that are already open
        and prevents missed on_fill from leaving a one-legged position orphaned.

        Rules:
          - perp qty < 0 AND spot qty > 0 → "hedged"
          - perp qty < 0, no spot found   → still "entering" (spot order will be
            retried below via a synthetic on_fill-style TargetPosition — caller
            must handle the return value)
          - no perp, spot qty > 0         → "exiting" orphan — log warning only,
            caller should flatten spot manually or via explicit TargetPosition
        """
        from decimal import Decimal as _D

        perp_map: dict[str, _D] = {}
        spot_map: dict[str, _D] = {}

        for p in perp_positions:
            if p.quantity != _D(0):
                perp_map[p.symbol] = p.quantity

        for p in spot_positions:
            if p.quantity > _D(0):
                spot_map[p.symbol] = p.quantity

        for sym in _SYMBOLS:
            perp_qty = perp_map.get(sym, _D(0))
            spot_qty = spot_map.get(sym, _D(0))

            if perp_qty < _D(0) and spot_qty > _D(0):
                self._fill_state[sym] = "hedged"
                self._perp_qty[sym] = float(perp_qty)
                self._spot_qty[sym] = float(spot_qty)
                self._last_period_time[sym] = datetime.now(UTC)
                log.info(
                    "[FundingArbBybit:%s] restored state=hedged  perp=%s  spot=%s",
                    sym, perp_qty, spot_qty,
                )
            elif perp_qty < _D(0) and spot_qty == _D(0):
                # Perp open but no spot — set state=hedged with spot_qty=0 so
                # the next on_funding_rate call detects the missing hedge and
                # automatically re-emits the spot buy.
                self._fill_state[sym] = "hedged"
                self._perp_qty[sym] = float(perp_qty)
                self._spot_qty[sym] = 0.0
                self._last_period_time[sym] = datetime.now(UTC)
                log.warning(
                    "[FundingArbBybit:%s] restored state=hedged (NO SPOT) perp=%s"
                    " — will auto-rehedge on next funding tick",
                    sym, perp_qty,
                )
            elif perp_qty == _D(0) and spot_qty > _D(0):
                # Spot orphan — perp was already closed but spot was never sold.
                # Keep state=flat but remember spot qty so on_funding_rate can
                # auto-sell it on the next tick.
                self._fill_state[sym] = "flat"
                self._spot_qty[sym] = float(spot_qty)
                log.warning(
                    "[FundingArbBybit:%s] ORPHANED SPOT qty=%s — will auto-sell on next funding tick",
                    sym, spot_qty,
                )
            # else: both zero — truly flat, nothing to restore

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        log.info("[FundingArbBybit] started — symbols: %s", _SYMBOLS)

    def on_stop(self) -> None:
        for sym in _SYMBOLS:
            log.info(
                "[FundingArbBybit:%s] stopped  perp=%s  spot=%s  equity=%.2f",
                sym,
                self._perp_qty[sym],
                self._spot_qty[sym],
                self._equity,
            )

    # ------------------------------------------------------------------
    # Market data handlers
    # ------------------------------------------------------------------

    def on_bar(self, bar: AnyBar) -> TargetPosition | None:
        if bar.symbol not in _SYMBOLS:
            return None
        self._bar_closes[bar.symbol].append(float(bar.close))
        return None

    def on_funding_rate(self, rate: FundingRate) -> TargetPosition | None:
        sym = rate.symbol
        if sym not in _SYMBOLS:
            return None

        current_rate = float(rate.funding_rate)
        if current_rate != current_rate:   # NaN check
            return None

        self._funding_rates[sym].append(current_rate)

        periods_per_yr = (24.0 / self._params["period_hours"]) * 365.0
        ann_rate = current_rate * periods_per_yr

        log.info(
            "[FundingArbBybit:%s] FUNDING  rate=%.6f  ann=%.2f%%  mark=%s  state=%s",
            sym,
            current_rate,
            ann_rate * 100,
            rate.mark_price,
            self._fill_state[sym],
        )

        if self._fill_state[sym] == "flat":
            # Auto-sell orphaned spot (perp already closed, spot was never sold)
            if self._spot_qty[sym] > 0.0:
                spot_qty = self._spot_qty[sym]
                self._spot_qty[sym] = 0.0
                log.warning(
                    "[FundingArbBybit:%s] AUTO-SELLING orphaned spot qty=%s",
                    sym, spot_qty,
                )
                from interfaces.signals import TargetPosition
                return TargetPosition(
                    symbol=sym,
                    quantity=Decimal(str(-round(spot_qty, 3))),
                    exchange="bybit_spot",
                )
            return self._check_entry(sym, rate, current_rate, ann_rate)

        if self._fill_state[sym] == "hedged":
            # If perp is open but spot hedge is missing (failed order or orphaned
            # state after restart), re-emit the spot buy immediately.
            if self._spot_qty[sym] == 0.0 and self._perp_qty[sym] != 0.0:
                spot_qty = abs(self._perp_qty[sym])
                log.warning(
                    "[FundingArbBybit:%s] MISSING SPOT HEDGE — re-emitting spot buy qty=%s",
                    sym, spot_qty,
                )
                from interfaces.signals import TargetPosition
                return TargetPosition(
                    symbol=sym,
                    quantity=Decimal(str(round(spot_qty, 3))),
                    exchange="bybit_spot",
                )
            return self._check_hold_exit(sym, rate, current_rate, ann_rate)

        # "entering" or "exiting" — wait for fill confirmation before acting
        return None

    # ------------------------------------------------------------------
    # Fill hook — triggers the spot hedge leg
    # ------------------------------------------------------------------

    def on_fill(self, fill: FillConfirmation) -> TargetPosition | None:
        from interfaces.signals import TargetPosition

        sym = fill.symbol
        if sym not in _SYMBOLS:
            return None

        state = self._fill_state[sym]

        if state == "entering":
            # Perp short was filled — buy spot to fully hedge.
            # Bybit spot taker fee (0.1%) is deducted in base coin, so receiving
            # perp_qty LINK in wallet requires ordering perp_qty / 0.999.
            # This ensures spot wallet qty == perp qty after fees → perfect delta neutral.
            perp_qty = abs(float(fill.quantity))
            spot_qty = round(perp_qty / 0.999, 3)
            fill_price = abs(float(fill.fill_price)) if fill.fill_price else 0.0
            if fill_price > 0:
                # Deduct entry fees from equity tracking: perp 0.01% + spot 0.10%
                fee = perp_qty * fill_price * 0.0011
                self._equity -= fee
            self._fill_state[sym] = "hedged"
            self._spot_qty[sym] = perp_qty  # track actual perp qty for exit sizing
            log.info(
                "[FundingArbBybit:%s] PERP FILL confirmed qty=%s → buying spot %s",
                sym,
                fill.quantity,
                spot_qty,
            )
            return TargetPosition(
                symbol=sym,
                quantity=Decimal(str(round(spot_qty, 3))),
                exchange="bybit_spot",
            )

        if state == "exiting":
            # Perp close was filled — sell the spot hedge.
            # Deduct exit fees: perp 0.01% + spot 0.10%.
            spot_qty = self._spot_qty[sym]
            fill_price = abs(float(fill.fill_price)) if fill.fill_price else 0.0
            if spot_qty > 0 and fill_price > 0:
                fee = spot_qty * fill_price * 0.0011
                self._equity -= fee
            if spot_qty > 0:
                self._fill_state[sym] = "flat"
                self._spot_qty[sym] = 0.0
                log.info(
                    "[FundingArbBybit:%s] PERP CLOSE confirmed — selling spot %s",
                    sym,
                    spot_qty,
                )
                return TargetPosition(
                    symbol=sym,
                    quantity=Decimal(str(-round(spot_qty, 3))),
                    exchange="bybit_spot",
                )
            self._fill_state[sym] = "flat"

        return None

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    def _check_entry(
        self,
        sym: str,
        rate: FundingRate,
        current_rate: float,
        ann_rate: float,
    ) -> TargetPosition | None:
        from interfaces.signals import TargetPosition

        consecutive = self._count_consecutive_positive(sym)
        volatility = self._rolling_volatility(sym)

        entry_ok = (
            ann_rate > self._params["entry_threshold_ann"]
            and consecutive >= self._params["min_consecutive"]
            and volatility < self._params["max_volatility"]
        )

        log.info(
            "[FundingArbBybit:%s] CHECK ENTRY  ann=%.2f%% (>%.0f%%)"
            "  consec=%d (>=%d)  vol=%.2f%%  → %s",
            sym,
            ann_rate * 100,
            self._params["entry_threshold_ann"] * 100,
            consecutive,
            self._params["min_consecutive"],
            volatility * 100,
            "OK" if entry_ok else "NO",
        )

        if not entry_ok:
            return None

        mark = float(rate.mark_price)
        if mark <= 0:
            return None

        notional = self._equity * self._params["position_size_pct"]
        size = -math.floor(abs(notional / mark))   # negative integer = short perp

        self._perp_qty[sym] = size
        self._entry_price[sym] = mark
        self._holding_periods[sym] = 0
        self._cum_funding[sym] = 0.0
        self._last_period_time[sym] = datetime.now(UTC)
        self._fill_state[sym] = "entering"

        log.info(
            "[FundingArbBybit:%s] ENTRY  ann=%.2f%%  size=%s  notional=%.2f  equity=%.2f",
            sym,
            ann_rate * 100,
            round(size, 6),
            notional,
            self._equity,
        )
        return TargetPosition(
            symbol=sym,
            quantity=Decimal(str(round(size, 6))),
            exchange="bybit_demo",
        )

    # ------------------------------------------------------------------
    # Hold / exit logic
    # ------------------------------------------------------------------

    def _check_hold_exit(
        self,
        sym: str,
        rate: FundingRate,
        current_rate: float,
        ann_rate: float,
    ) -> TargetPosition | None:
        period_hours = self._params["period_hours"]

        now = datetime.now(UTC)
        last = self._last_period_time[sym]

        # Determine whether we are AT a settlement window.
        # Settlement windows are at 00:00, 08:00, 16:00 UTC.
        # We consider ourselves "at settlement" if this is the first funding
        # event of a new 8h period (i.e. >= period_hours since last settlement).
        at_settlement = (last is None or now - last >= timedelta(hours=period_hours))

        if at_settlement:
            self._holding_periods[sym] += 1
            self._cum_funding[sym] += current_rate
            self._last_period_time[sym] = now

            # Compound funding into equity
            mark = float(rate.mark_price)
            if mark > 0:
                payment = abs(self._perp_qty[sym]) * mark * current_rate
                self._equity += payment

            log.info(
                "[FundingArbBybit:%s] SETTLEMENT period #%d  rate=%.4f%%  cum=%.4f%%  equity=%.2f",
                sym,
                self._holding_periods[sym],
                current_rate * 100,
                self._cum_funding[sym] * 100,
                self._equity,
            )

        # Hard cap — exit regardless of min_hold (check every event so it fires
        # even if we miss a settlement window).
        if self._holding_periods[sym] >= self._params["max_hold"]:
            log.info(
                "[FundingArbBybit:%s] EXIT (max_hold=%d)  cum_funding=%.4f%%",
                sym,
                self._holding_periods[sym],
                self._cum_funding[sym] * 100,
            )
            return self._flatten(sym)

        # Rate-based and stop-loss exits are ONLY evaluated AT settlement windows.
        # The predicted funding rate fluctuates noisily between settlements —
        # acting on every tick causes rapid entry/exit churn.
        if not at_settlement:
            return None

        # Suppress rate-based exits during minimum hold window
        if self._holding_periods[sym] < self._params["min_hold"]:
            return None

        # Rate-based exits — evaluated once per settlement only
        exit_reasons = []

        if ann_rate < self._params["exit_threshold_ann"]:
            exit_reasons.append(f"funding_low={ann_rate:.2%}")

        if current_rate < 0:
            exit_reasons.append(f"funding_negative={current_rate:.6f}")

        if (
            self._params["use_price_stop"]
            and self._bar_closes[sym]
            and self._entry_price[sym] > 0
        ):
            current_price = self._bar_closes[sym][-1]
            pnl_pct = (self._entry_price[sym] - current_price) / self._entry_price[sym]
            if pnl_pct < -self._params["stop_loss_pct"]:
                exit_reasons.append(f"stop_loss={pnl_pct:.2%}")

        if exit_reasons:
            log.info(
                "[FundingArbBybit:%s] EXIT (settlement)  reasons=%s  periods=%d",
                sym,
                exit_reasons,
                self._holding_periods[sym],
            )
            return self._flatten(sym)

        return None

    def _flatten(self, sym: str) -> TargetPosition:
        from interfaces.signals import TargetPosition

        close_qty = -self._perp_qty[sym]   # positive = buy back the short
        self._perp_qty[sym] = 0.0
        self._holding_periods[sym] = 0
        self._last_period_time[sym] = None
        self._fill_state[sym] = "exiting"   # on_fill will close spot leg

        return TargetPosition(
            symbol=sym,
            quantity=Decimal(str(round(close_qty, 6))),
            exchange="bybit_demo",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _count_consecutive_positive(self, sym: str) -> int:
        count = 0
        for r in reversed(list(self._funding_rates[sym])):
            if r > 0:
                count += 1
            else:
                break
        return count

    def _rolling_volatility(self, sym: str) -> float:
        """20-bar annualised log-return volatility.  Returns 0.5 if insufficient data."""
        closes = list(self._bar_closes[sym])
        if len(closes) < 2:
            return 0.5

        log_returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(max(1, len(closes) - 20), len(closes))
            if closes[i - 1] > 0
        ]
        if not log_returns:
            return 0.5

        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / n
        periods_per_yr = (24.0 / self._params["period_hours"]) * 365.0
        return math.sqrt(variance) * math.sqrt(periods_per_yr)
