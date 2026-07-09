"""Funding Rate Arbitrage — Bybit delta-neutral (perp short + spot long hedge).

Runs on ETHUSDT and LINKUSDT simultaneously using Bybit's 8h settlement.

Strategy:
  - Shorts the perp on Bybit linear (exchange="bybit_demo") to collect funding.
  - After the perp fill is confirmed, buys the same quantity on Bybit spot
    (exchange="bybit_spot") to remain delta-neutral.
  - On exit, closes the perp first; after that fill is confirmed, closes spot.

Parameters (tuned 2026-05-02 after fee-aware re-analysis):
  entry_threshold       10.5 % annualised  (between Bybit's 10.95 % cap on
                                            LINK and the 5 % exit threshold;
                                            high enough to outpace fees over
                                            ~30 settlements, low enough to
                                            actually trigger in current regime)
  exit_threshold         5 % annualised  (hysteresis vs entry, no churn)
  min_consecutive        3 positive settled periods (24 h confirmed regime)
  min_hold               1 period   (8 h)
  max_hold             100 periods  (~33 d safety cap; never the thesis exit)
  max_volatility       0.80 annualised vol (skips liquidation-cascade regimes)
  position_size       50 % of equity per symbol

Decisions are taken at most once per 8 h settlement window per symbol; all
predicted-rate stream ticks between settlements are ignored (see the
``_settlement_window`` gate in :meth:`on_funding_rate`).

Run with::

    STRATEGY_NAME=FundingArbBybitStrategy python -m workers.strategy_worker
"""

from __future__ import annotations

import logging
import math
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

from interfaces.strategy import BaseStrategy

if TYPE_CHECKING:
    from data.types import AnyBar, FundingRate
    from execution.types import FillConfirmation
    from interfaces.signals import TargetPosition

log = logging.getLogger(__name__)


_SYMBOLS = ("ETHUSDT", "LINKUSDT")

# Bybit spot taker fee (0.10%).  Spot fees are charged in the base coin on a
# Buy and in the quote coin on a Sell.  To receive exactly perp_qty base coin
# in the wallet, we must order perp_qty / (1 - fee_rate).  Used in on_fill
# when sizing the spot hedge after a perp fill.
_SPOT_TAKER_FEE = 0.001


class FundingArbBybitStrategy(BaseStrategy):
    """Delta-neutral funding arb on Bybit: perp short + spot long hedge.

    Manages each symbol independently with its own state machine; fills from
    the perp leg trigger the spot hedge via :meth:`on_fill`.

    Invariants enforced by this implementation:

      I1.  Decisions are taken on settled funding rates only — not on the
           predicted-rate stream Bybit publishes between settlements.

      I2.  Entry and exit are evaluated at the same cadence (one per 8 h
           settlement window per symbol).

      I3.  Within a single window, no symbol can traverse
           ``hedged → exiting → flat → entering`` — the gate is set BEFORE
           ``_flatten`` runs, so any subsequent stream tick is rejected.

      I4.  Net delta exposure tracked by the strategy is always 0 while
           ``state == hedged`` — perp_qty and spot_qty are signed mirrors.

      I5.  The price stop is intentionally absent.  A correctly-hedged
           position cannot have a directional loss; if one is observed,
           the hedge has broken and the orphan-recovery paths handle it.
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
        entry_threshold_annualized: float = 0.105,
        exit_threshold_annualized: float = 0.05,
        min_consecutive_positive: int = 3,
        min_hold_periods: int = 1,
        max_hold_periods: int = 100,
        max_volatility: float = 0.80,
        symbols: tuple[str, ...] = _SYMBOLS,
    ) -> None:
        self._equity = initial_equity
        self._symbols = tuple(symbols)
        self._params = {
            "position_size_pct": position_size_pct,
            "entry_threshold_ann": entry_threshold_annualized,
            "exit_threshold_ann": exit_threshold_annualized,
            "min_consecutive": min_consecutive_positive,
            "min_hold": min_hold_periods,
            "max_hold": max_hold_periods,
            "max_volatility": max_volatility,
            "period_hours": 8,
        }

        # Per-symbol state -------------------------------------------------
        # fill_state ∈ {"flat", "entering", "hedged", "exiting"}
        self._fill_state: dict[str, str] = {s: "flat" for s in self._symbols}
        self._perp_qty: dict[str, float] = {s: 0.0 for s in self._symbols}   # < 0 = short
        self._spot_qty: dict[str, float] = {s: 0.0 for s in self._symbols}   # > 0 = long
        self._entry_price: dict[str, float] = {s: 0.0 for s in self._symbols}
        self._holding_periods: dict[str, int] = {s: 0 for s in self._symbols}
        self._last_period_time: dict[str, datetime | None] = {s: None for s in self._symbols}
        self._cum_funding: dict[str, float] = {s: 0.0 for s in self._symbols}

        # Settlement-window gate: canonical UTC datetime of the most recent
        # settlement window this symbol has already processed.  Prevents both
        # acting on between-settlement predicted-rate noise (I1) and
        # re-entering the same window we just exited (I3).
        self._last_settled_window: dict[str, datetime | None] = {s: None for s in self._symbols}

        # Watchdog: timestamp of the last unfilled signal we emitted.  If a
        # fill confirmation does not arrive within _PENDING_TIMEOUT_S, the
        # next event triggers a state reconciliation rather than blindly
        # waiting forever.
        self._pending_signal_at: dict[str, datetime | None] = {s: None for s in self._symbols}

        # Rolling buffer of *settled* funding rates for the consecutive-
        # positive entry filter.  Predicted rates are never appended here.
        self._settled_rates: dict[str, deque[float]] = {
            s: deque(maxlen=24) for s in self._symbols
        }
        # Bar closes for rolling volatility.
        self._bar_closes: dict[str, deque[float]] = {
            s: deque(maxlen=21) for s in self._symbols
        }

    # Watchdog: a perp signal that has not yet been confirmed by an on_fill
    # call within this many seconds is treated as stuck.  10 s is generous
    # for a market order on Bybit demo (typical fill latency is < 2 s).
    _PENDING_TIMEOUT_S: ClassVar[float] = 10.0

    # ------------------------------------------------------------------
    # State recovery — call after construction, before on_start
    # ------------------------------------------------------------------

    def restore_from_positions(
        self,
        perp_positions: list,
        spot_positions: list,
    ) -> None:
        """Restore fill_state from live broker positions after a restart.

        Prevents the strategy from re-entering positions that are already
        open and prevents missed on_fill from leaving a one-legged position
        orphaned.
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

        for sym in self._symbols:
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
                # the next on_funding_rate detects the missing hedge and
                # auto-emits the spot buy.
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
                # Spot orphan — perp was closed but spot was never sold.
                # Keep state=flat but remember spot qty so on_funding_rate
                # can auto-sell it on the next tick.
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
        log.info(
            "[FundingArbBybit] started — symbols=%s  entry=%.0f%%  exit=%.0f%%"
            "  min_consec=%d  max_hold=%d  max_vol=%.0f%%",
            list(self._symbols),
            self._params["entry_threshold_ann"] * 100,
            self._params["exit_threshold_ann"] * 100,
            self._params["min_consecutive"],
            self._params["max_hold"],
            self._params["max_volatility"] * 100,
        )

    def on_stop(self) -> None:
        for sym in self._symbols:
            log.info(
                "[FundingArbBybit:%s] stopped  perp=%s  spot=%s  equity=%.2f",
                sym,
                self._perp_qty[sym],
                self._spot_qty[sym],
                self._equity,
            )

    def heartbeat_state(self) -> dict:
        """Snapshot of strategy state for periodic heartbeat publication."""
        return {
            "name": self.__class__.__name__,
            "display_name": self.display_name,
            "equity": self._equity,
            "symbols": {
                sym: {
                    "fill_state": self._fill_state[sym],
                    "perp_qty": self._perp_qty[sym],
                    "spot_qty": self._spot_qty[sym],
                    "entry_price": self._entry_price[sym],
                    "holding_periods": self._holding_periods[sym],
                    "cum_funding": self._cum_funding[sym],
                    "last_settled_window": (
                        self._last_settled_window[sym].isoformat()
                        if self._last_settled_window[sym] else None
                    ),
                }
                for sym in self._symbols
            },
        }

    # ------------------------------------------------------------------
    # Market data handlers
    # ------------------------------------------------------------------

    def on_bar(self, bar: AnyBar) -> TargetPosition | None:
        if bar.symbol not in self._symbols:
            return None
        self._bar_closes[bar.symbol].append(float(bar.close))
        return None

    def on_funding_rate(self, rate: FundingRate) -> TargetPosition | None:
        sym = rate.symbol
        if sym not in self._symbols:
            return None

        current_rate = float(rate.funding_rate)
        if current_rate != current_rate:   # NaN check
            return None

        periods_per_yr = (24.0 / self._params["period_hours"]) * 365.0
        ann_rate = current_rate * periods_per_yr
        now = datetime.now(UTC)

        # ----- Watchdog: detect stuck pending signals -----
        # A pending state ("entering"/"exiting") with no fill confirmation
        # within _PENDING_TIMEOUT_S is treated as stuck.  We log a warning
        # and clear the pending timestamp; the next event (or restart-time
        # restore_from_positions) will reconcile state from broker truth.
        pending_at = self._pending_signal_at[sym]
        if (
            pending_at is not None
            and self._fill_state[sym] in ("entering", "exiting")
            and (now - pending_at).total_seconds() > self._PENDING_TIMEOUT_S
        ):
            log.warning(
                "[FundingArbBybit:%s] WATCHDOG: state=%s pending for %.1fs without fill"
                " — clearing pending flag, will reconcile on next event/restart",
                sym, self._fill_state[sym],
                (now - pending_at).total_seconds(),
            )
            self._pending_signal_at[sym] = None

        # ----- Recovery paths: must run on every event, not gated -----
        # These exist to repair broken state (orphaned spot, missing hedge
        # after a failed/dropped fill).  They do NOT take entry/exit
        # decisions, so firing them on predicted-rate ticks is safe.
        if self._fill_state[sym] == "flat" and self._spot_qty[sym] > 0.0:
            spot_qty = self._spot_qty[sym]
            self._spot_qty[sym] = 0.0
            log.warning(
                "[FundingArbBybit:%s] AUTO-SELLING orphaned spot qty=%s",
                sym, spot_qty,
            )
            from interfaces.signals import TargetPosition
            return TargetPosition(
                symbol=sym,
                quantity=Decimal(str(-spot_qty)),
                exchange="bybit_spot",
            )

        if (
            self._fill_state[sym] == "hedged"
            and self._spot_qty[sym] == 0.0
            and self._perp_qty[sym] != 0.0
        ):
            perp_qty = abs(self._perp_qty[sym])
            spot_qty = perp_qty / (1.0 - _SPOT_TAKER_FEE)
            log.warning(
                "[FundingArbBybit:%s] MISSING SPOT HEDGE — re-emitting spot buy qty=%s"
                " (perp=%s, fee-grossed=%s)",
                sym, spot_qty, perp_qty, spot_qty,
            )
            from interfaces.signals import TargetPosition
            return TargetPosition(
                symbol=sym,
                quantity=Decimal(str(spot_qty)),
                exchange="bybit_spot",
            )

        # ----- Settlement-window gate: single source of decision cadence -----
        # I1, I2, I3.  All entry/exit/holding-period accrual flows through
        # exactly one decision per settlement window per symbol.
        window = self._settlement_window(now)
        last = self._last_settled_window[sym]

        if last is not None and window <= last:
            log.debug(
                "[FundingArbBybit:%s] predicted rate=%.6f ann=%.2f%% (window %s already processed)",
                sym, current_rate, ann_rate * 100, window.isoformat(),
            )
            return None

        if last is None:
            # Cold start: only trust the event as a settlement if it arrived
            # within 5 minutes of the boundary.  Otherwise it's mid-window
            # predicted noise — defer to the next true boundary.
            seconds_into_window = (now - window).total_seconds()
            if seconds_into_window > 300:
                log.info(
                    "[FundingArbBybit:%s] startup mid-window (%ds past %s) —"
                    " deferring first decision to next settlement boundary",
                    sym, int(seconds_into_window), window.isoformat(),
                )
                self._last_settled_window[sym] = window
                return None

        log.info(
            "[FundingArbBybit:%s] SETTLED  window=%s  rate=%.6f  ann=%.2f%%  mark=%s  state=%s",
            sym,
            window.isoformat(),
            current_rate,
            ann_rate * 100,
            rate.mark_price,
            self._fill_state[sym],
        )

        # Append ONLY settled rates so consecutive-positive count and rolling
        # stats reflect realised history, not stream noise.
        self._settled_rates[sym].append(current_rate)
        # Mark this window processed BEFORE acting, so any synchronous error
        # in the decision branch still closes the gate for this window.
        self._last_settled_window[sym] = window

        if self._fill_state[sym] == "flat":
            return self._check_entry(sym, rate, ann_rate, now)

        if self._fill_state[sym] == "hedged":
            return self._check_hold_exit(sym, rate, current_rate, ann_rate, window)

        # state ∈ {"entering", "exiting"} — fill not yet confirmed.  The gate
        # is already advanced so the next stream tick won't re-decide either.
        # Decision picks back up at the next true settlement window.
        return None

    @staticmethod
    def _settlement_window(now: datetime) -> datetime:
        """Canonical UTC datetime of the most recent 00/08/16 settlement boundary."""
        hour_bucket = (now.hour // 8) * 8
        return now.replace(hour=hour_bucket, minute=0, second=0, microsecond=0)

    # ------------------------------------------------------------------
    # Fill hook — triggers the spot hedge leg
    # ------------------------------------------------------------------

    def on_fill(self, fill: FillConfirmation) -> TargetPosition | None:
        from interfaces.signals import TargetPosition

        sym = fill.symbol
        if sym not in self._symbols:
            return None

        # Any fill clears the pending watchdog: confirmation has arrived.
        self._pending_signal_at[sym] = None

        state = self._fill_state[sym]

        if state == "entering":
            # Perp short was filled.  Size the spot buy to receive exactly
            # perp_qty base coin in wallet after fees:  order = perp / (1 - fee).
            perp_filled = abs(float(fill.quantity))
            spot_qty = perp_filled / (1.0 - _SPOT_TAKER_FEE)
            fill_price = abs(float(fill.fill_price)) if fill.fill_price else 0.0
            if fill_price > 0:
                # Track entry-side fees in shadow equity (perp 0.055% + spot 0.10%).
                fee = perp_filled * fill_price * (0.00055 + 0.001)
                self._equity -= fee
            self._fill_state[sym] = "hedged"
            # Provisionally track the *intended* post-fee qty.  This will be
            # the strategy's view of its hedge until the spot-fill on_fill
            # arrives (at which point we'll trust the actual fill quantity).
            self._spot_qty[sym] = perp_filled
            self._pending_signal_at[sym] = datetime.now(UTC)
            log.info(
                "[FundingArbBybit:%s] PERP FILL confirmed qty=%s → buying spot %.6f",
                sym, fill.quantity, spot_qty,
            )
            return TargetPosition(
                symbol=sym,
                quantity=Decimal(str(spot_qty)),
                exchange="bybit_spot",
            )

        if state == "hedged" and fill.exchange == "bybit_spot":
            # Spot buy fill confirmation — capture the actual filled quantity
            # so _spot_qty reflects truth, not the size we ordered.  This
            # matters because Bybit deducts fees in base coin: ordering 1.001
            # ETH gives ~0.999996 ETH in wallet, not exactly 1 ETH.
            actual = abs(float(fill.quantity))
            if actual > 0:
                self._spot_qty[sym] = actual
                log.info(
                    "[FundingArbBybit:%s] SPOT FILL confirmed actual_qty=%.8f"
                    " (perp_qty=%.6f, residual_delta=%+.6f)",
                    sym, actual, abs(self._perp_qty[sym]),
                    actual - abs(self._perp_qty[sym]),
                )
            return None

        if state == "exiting":
            # Perp close was filled — sell the actual spot holdings.
            spot_qty = self._spot_qty[sym]
            fill_price = abs(float(fill.fill_price)) if fill.fill_price else 0.0
            if spot_qty > 0 and fill_price > 0:
                fee = spot_qty * fill_price * (0.00055 + 0.001)
                self._equity -= fee
            if spot_qty > 0:
                self._fill_state[sym] = "flat"
                self._spot_qty[sym] = 0.0
                self._pending_signal_at[sym] = datetime.now(UTC)
                log.info(
                    "[FundingArbBybit:%s] PERP CLOSE confirmed — selling spot %.8f",
                    sym, spot_qty,
                )
                return TargetPosition(
                    symbol=sym,
                    quantity=Decimal(str(-spot_qty)),
                    exchange="bybit_spot",
                )
            # No spot to sell — go straight to flat.
            self._fill_state[sym] = "flat"

        if state == "flat" and fill.exchange == "bybit_spot":
            # Spot sell fill confirmation arrives in flat state.  Nothing to
            # do; the auto-orphan path already cleared _spot_qty.
            return None

        return None

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    def _check_entry(
        self,
        sym: str,
        rate: FundingRate,
        ann_rate: float,
        now: datetime,
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
            "  consec=%d (>=%d)  vol=%.2f%% (<%.0f%%)  → %s",
            sym,
            ann_rate * 100,
            self._params["entry_threshold_ann"] * 100,
            consecutive,
            self._params["min_consecutive"],
            volatility * 100,
            self._params["max_volatility"] * 100,
            "OK" if entry_ok else "NO",
        )

        if not entry_ok:
            return None

        mark = float(rate.mark_price)
        if mark <= 0:
            return None

        notional = self._equity * self._params["position_size_pct"]
        size = -math.floor(abs(notional / mark))   # negative integer = short perp
        if size == 0:
            log.warning(
                "[FundingArbBybit:%s] ENTRY skipped: notional %.2f / mark %.2f rounds to 0 contracts",
                sym, notional, mark,
            )
            return None

        self._perp_qty[sym] = size
        self._entry_price[sym] = mark
        self._holding_periods[sym] = 0
        self._cum_funding[sym] = 0.0
        self._last_period_time[sym] = now
        self._fill_state[sym] = "entering"
        self._pending_signal_at[sym] = now

        log.info(
            "[FundingArbBybit:%s] ENTRY  ann=%.2f%%  size=%d  notional=%.2f  equity=%.2f",
            sym,
            ann_rate * 100,
            size,
            notional,
            self._equity,
        )
        return TargetPosition(
            symbol=sym,
            quantity=Decimal(str(size)),
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
        window: datetime,
    ) -> TargetPosition | None:
        # Caller has already verified this is the first event in a new
        # settlement window.  Treat as a true settlement: advance the holding
        # counter, accrue funding into shadow equity, then evaluate exits.
        last = self._last_period_time[sym]
        if last is not None:
            period_hours = self._params["period_hours"]
            periods_elapsed = max(
                1,
                round((window - last).total_seconds() / (period_hours * 3600)),
            )
        else:
            periods_elapsed = 1

        self._holding_periods[sym] += periods_elapsed
        self._cum_funding[sym] += current_rate
        self._last_period_time[sym] = window

        # Compound funding into shadow equity.  Shadow equity drives the next
        # entry's position size; tracking it here keeps sizing fee-aware.
        mark = float(rate.mark_price)
        if mark > 0:
            payment = abs(self._perp_qty[sym]) * mark * current_rate
            self._equity += payment

        log.info(
            "[FundingArbBybit:%s] SETTLEMENT period #%d (+%d)  rate=%.6f%%  cum=%.4f%%  equity=%.2f",
            sym,
            self._holding_periods[sym],
            periods_elapsed,
            current_rate * 100,
            self._cum_funding[sym] * 100,
            self._equity,
        )

        # Suppress all exits during the minimum hold window.
        if self._holding_periods[sym] < self._params["min_hold"]:
            return None

        exit_reasons: list[str] = []

        # Hard cap — safety only, default 100 periods (~33 d).  Not a thesis exit.
        if self._holding_periods[sym] >= self._params["max_hold"]:
            exit_reasons.append(f"max_hold={self._holding_periods[sym]}")

        if ann_rate < self._params["exit_threshold_ann"]:
            exit_reasons.append(f"funding_low={ann_rate:.2%}")

        if current_rate < 0:
            exit_reasons.append(f"funding_negative={current_rate:.6f}")

        # Note: no price stop.  A correctly-hedged delta-neutral position
        # cannot lose to price moves; if it appears to, the hedge is broken
        # and the orphan-recovery paths above will handle it.

        if exit_reasons:
            log.info(
                "[FundingArbBybit:%s] EXIT (settlement)  reasons=%s  periods=%d  cum_funding=%.4f%%",
                sym,
                exit_reasons,
                self._holding_periods[sym],
                self._cum_funding[sym] * 100,
            )
            return self._flatten(sym)

        return None

    def _flatten(self, sym: str) -> TargetPosition:
        from interfaces.signals import TargetPosition

        # Net the strategy's contribution back to zero.  The consolidator
        # reconciles to actual broker position via broker_delta, so any
        # accumulated fee residual on the broker side gets correctly closed.
        close_qty = -self._perp_qty[sym]   # positive = buy back the short
        self._perp_qty[sym] = 0.0
        self._holding_periods[sym] = 0
        self._last_period_time[sym] = None
        self._fill_state[sym] = "exiting"
        self._pending_signal_at[sym] = datetime.now(UTC)

        return TargetPosition(
            symbol=sym,
            quantity=Decimal(str(close_qty)),
            exchange="bybit_demo",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _count_consecutive_positive(self, sym: str) -> int:
        count = 0
        for r in reversed(list(self._settled_rates[sym])):
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
