from __future__ import annotations

import logging
import math
import os
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

from interfaces.strategy import BaseStrategy

if TYPE_CHECKING:
    from data.types import AnyBar, FundingRate
    from interfaces.signals import TargetPosition

log = logging.getLogger(__name__)

_ANN_FACTOR = Decimal("1095")  # 3 payments/day × 365 days (8 h bars)

# Symbol is read once at import time so the class-level ``topics`` ClassVar
# is populated before the strategy worker reads it.  Override via env var:
#   STRATEGY_SYMBOL=LINKUSDT python -m workers.strategy_worker
_SYMBOL: str = os.getenv("STRATEGY_SYMBOL", "ETHUSDT")


class FundingArbStrategy(BaseStrategy):
    """Delta-neutral funding rate arbitrage on a single perpetual futures symbol.

    Shorts the perp to collect positive funding payments. The spot long hedge
    (for delta-neutrality) must be placed by the caller — this strategy only
    emits TargetPosition signals for the perp leg.

    Entry: annualised funding > entry_threshold
           AND >= min_consecutive_positive consecutive positive 8 h bars
           AND 20-bar rolling annualised volatility < max_volatility

    Hold:  minimum min_hold_periods 8 h-periods regardless of rate movements.

    Exit:  annualised rate < exit_threshold OR raw rate negative,
           evaluated only after min_hold_periods.
           Hard cap at max_hold_periods (always exits).

    Parameters
    ----------
    symbol:
        Futures symbol, e.g. ``"ETHUSDT"``.
    initial_equity:
        Starting equity in quote currency. Used for position sizing and
        compounding as funding payments accrue.
    position_size_pct:
        Fraction of equity to deploy per trade (``Decimal("0.50")`` = 50 %).
    entry_threshold_annualized:
        Minimum annualised funding rate to trigger entry (default 5 %).
    exit_threshold_annualized:
        Annualised rate below which exit is triggered after min hold (default 0 %).
    min_consecutive_positive:
        Minimum consecutive positive 8 h bars required before entry (default 2).
    min_hold_periods:
        Minimum number of 8 h settlement periods to hold (default 24 = 8 days).
    max_hold_periods:
        Hard cap on holding periods — exits unconditionally (default 42 = 14 days).
    max_volatility:
        20-bar annualised volatility ceiling for entry sanity check (default 100 %).
    """

    # --- PRODUCTION (restore these, comment out FAST MODE block below) ---
    # topics: ClassVar[list[str]] = [
    #     f"futures.{_SYMBOL}.bars.8h",
    #     f"futures.{_SYMBOL}.funding_rate",
    # ]

    # --- FAST MODE for testing (comment out when going live) ---
    topics: ClassVar[list[str]] = [
        f"futures.{_SYMBOL}.bars.1m",
        f"futures.{_SYMBOL}.funding_rate",
    ]
    max_loss: ClassVar[Decimal] = Decimal("1000")

    def __init__(
        self,
        symbol: str = _SYMBOL,
        initial_equity: Decimal = Decimal("10000"),
        position_size_pct: Decimal = Decimal("0.50"),
        entry_threshold_annualized: Decimal = Decimal("0.005"),
        exit_threshold_annualized: Decimal = Decimal("0.00"),
        min_consecutive_positive: int = 2,
        min_hold_periods: int = 10,  # FAST MODE: 10 bars/minutes.(= 8 days)
        max_hold_periods: int = 30,  # FAST MODE: 30 bars/minutes.(= 14 days)
        max_volatility: Decimal = Decimal("1.00"),
    ) -> None:
        self.symbol = symbol
        self._equity = initial_equity
        self._position_size_pct = position_size_pct
        self._entry_threshold_ann = entry_threshold_annualized
        self._exit_threshold_ann = exit_threshold_annualized
        self._min_consecutive = min_consecutive_positive
        self._min_hold = min_hold_periods
        self._max_hold = max_hold_periods
        self._max_vol = max_volatility

        # Position state
        self._position_coins: Decimal = Decimal("0")  # negative = short
        self._holding_periods: int = 0
        self._last_boundary: datetime | None = None  # last credited settlement boundary

        # Rolling history buffers
        self._bar_closes: deque[Decimal] = deque(
            maxlen=10
        )  # 21 closes → 20 log-returns
        self._funding_rates: deque[Decimal] = deque(
            maxlen=10
        )  # for consecutive_positive

        # FAST MODE: cache the last funding rate so on_bar can drive signal checks.
        # In production (8h bars) on_funding_rate fires at the same cadence as bars,
        # so this cache is not needed.  Remove in production if desired.
        self._last_rate: FundingRate | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        log.info("[FundingArb:%s] started", self.symbol)

    def on_stop(self) -> None:
        log.info(
            "[FundingArb:%s] stopped  position=%s coins  equity=%s",
            self.symbol,
            self._position_coins,
            self._equity,
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_bar(self, bar: AnyBar) -> TargetPosition | None:
        if bar.symbol != self.symbol:
            return None
        self._bar_closes.append(bar.close)
        log.info(
            "[FundingArb:%s] BAR  close=%s  bars_buffered=%d",
            self.symbol,
            bar.close,
            len(self._bar_closes),
        )

        # FAST MODE: drive signal checks from every bar using the cached funding rate.
        # In production (8h bars) remove this block — on_funding_rate handles it.
        if self._last_rate is not None:
            rate = self._last_rate
            ann_rate = rate.funding_rate * _ANN_FACTOR
            # Inject the current bar timestamp the period boundary advances with bars.
            from dataclasses import replace as _replace

            rate = _replace(rate, timestamp=bar.timestamp)
            if self._position_coins == Decimal("0"):
                return self._check_entry(rate, ann_rate)
            else:
                return self._check_hold_exit(rate, ann_rate)
        else:
            log.info(
                "[FundingArb:%s] BAR received but no funding rate cached yet — waiting",
                self.symbol,
            )

        return None

    def on_funding_rate(self, rate: FundingRate) -> TargetPosition | None:
        if rate.symbol != self.symbol:
            return None

        log.info(
            "[FundingArb:%s] FUNDING  rate=%s  ann=%.2f%%  mark=%s",
            self.symbol,
            rate.funding_rate,
            float(rate.funding_rate * _ANN_FACTOR) * 100,
            rate.mark_price,
        )
        self._funding_rates.append(rate.funding_rate)
        self._last_rate = (
            rate  # FAST MODE: cache for on_bar to use. Remove in production if desired.
        )
        ann_rate = rate.funding_rate * _ANN_FACTOR

        if self._position_coins == Decimal("0"):
            return self._check_entry(rate, ann_rate)
        else:
            return self._check_hold_exit(rate, ann_rate)

    # ------------------------------------------------------------------
    # Signal logic
    # ------------------------------------------------------------------

    def _check_entry(
        self, rate: FundingRate, ann_rate: Decimal
    ) -> TargetPosition | None:
        from interfaces.signals import TargetPosition

        consecutive_positive = self._count_consecutive_positive()
        volatility = self._rolling_volatility()

        entry_ok = (
            ann_rate > self._entry_threshold_ann
            and consecutive_positive >= self._min_consecutive
            and volatility < self._max_vol
        )

        log.info(
            "[FundingArb:%s] CHECK ENTRY  ann=%.2f%% (need >%.0f%%) "
            "consec=%d (need >=%d)  vol=%.2f%%  → %s",
            self.symbol,
            float(ann_rate) * 100,
            float(self._entry_threshold_ann) * 100,
            consecutive_positive,
            self._min_consecutive,
            float(volatility) * 100,
            "OK" if entry_ok else "NO",
        )

        if not entry_ok:
            return None

        # Position size: negative quantity = short perp
        notional = self._equity * self._position_size_pct
        coins = (notional / rate.mark_price).quantize(Decimal("0.000001"))
        if coins <= Decimal("0"):
            return None

        self._position_coins = -coins
        self._holding_periods = 0
        self._last_boundary = _floor_8h(rate.timestamp)

        log.info(
            "[FundingArb:%s] ENTRY  ann_rate=%.2f%%  consec=%d  vol=%.2f%%"
            "  coins=%s  notional=%s  equity=%s",
            self.symbol,
            float(ann_rate) * 100,
            consecutive_positive,
            float(volatility) * 100,
            coins,
            notional,
            self._equity,
        )
        return TargetPosition(
            symbol=self.symbol, quantity=-coins, exchange="bybit_demo"
        )

    def _check_hold_exit(
        self, rate: FundingRate, ann_rate: Decimal
    ) -> TargetPosition | None:
        # Advance holding period counter at each new 8 h settlement boundary.
        boundary = _floor_8h(rate.timestamp)
        if self._last_boundary is None or boundary > self._last_boundary:
            self._holding_periods += 1
            self._last_boundary = boundary
            self._credit_funding(rate)

        # Hard cap — always exit regardless of min hold
        if self._holding_periods >= self._max_hold:
            log.info(
                "[FundingArb:%s] EXIT (max_hold)  periods=%d",
                self.symbol,
                self._holding_periods,
            )
            return self._flatten()

        # Suppress rate-based exits during the minimum hold window
        if self._holding_periods < self._min_hold:
            return None

        # Rate-based exit conditions
        if ann_rate < self._exit_threshold_ann or rate.funding_rate < Decimal("0"):
            log.info(
                "[FundingArb:%s] EXIT (rate)  ann_rate=%.2f%%  periods=%d",
                self.symbol,
                float(ann_rate) * 100,
                self._holding_periods,
            )
            return self._flatten()

        return None

    def _flatten(self) -> TargetPosition:
        from interfaces.signals import TargetPosition

        # Positive delta to buy back the short
        close_qty = -self._position_coins
        self._position_coins = Decimal("0")
        self._holding_periods = 0
        self._last_boundary = None
        return TargetPosition(
            symbol=self.symbol, quantity=close_qty, exchange="bybit_demo"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _credit_funding(self, rate: FundingRate) -> None:
        """Compound funding income into equity for position sizing."""
        notional = abs(self._position_coins) * rate.mark_price
        # Short position receives positive funding
        payment = rate.funding_rate * notional
        self._equity += payment
        log.debug(
            "[FundingArb:%s] funding credit  rate=%s notional=%s payment=%s equity=%s",
            self.symbol,
            rate.funding_rate,
            notional,
            payment,
            self._equity,
        )

    def _count_consecutive_positive(self) -> int:
        count = 0
        for r in reversed(self._funding_rates):
            if r > Decimal("0"):
                count += 1
            else:
                break
        return count

    def _rolling_volatility(self) -> Decimal:
        """20-bar annualised volatility of log returns.
        Returns 0 if insufficient data."""
        closes = list(self._bar_closes)
        if len(closes) < 2:
            return Decimal("0")

        log_returns = [
            math.log(float(closes[i]) / float(closes[i - 1]))
            for i in range(1, len(closes))
            if float(closes[i - 1]) > 0
        ]
        if not log_returns:
            return Decimal("0")

        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / n
        # Annualise: 3 × 8 h periods per day × 365 days
        ann_vol = math.sqrt(variance) * math.sqrt(3 * 365)
        return Decimal(str(round(ann_vol, 6)))


# --- PRODUCTION: floor to real 8 h settlement boundaries ---
# def _floor_8h(dt: datetime) -> datetime:
#     """Floor a UTC datetime to the nearest 8 h settlement boundary
#       (00:00 / 08:00 / 16:00 UTC)."""
#     return dt.replace(
#         hour=(dt.hour // 8) * 8,
#         minute=0,
#         second=0,
#         microsecond=0,
#         tzinfo=timezone.utc,
#     )


# --- FAST MODE: floor to 1-minute boundaries so holding_periods ticks every minute ---
def _floor_8h(dt: datetime) -> datetime:
    """FAST MODE: floor to the nearest minute so holding_periods advances each bar.
    Restore the commented-out 8 h version above for production.
    """
    return dt.replace(second=0, microsecond=0, tzinfo=UTC)
