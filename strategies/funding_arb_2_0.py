"""
QuantETH — Funding Rate Arbitrage Strategy 2.0

Lighter-style 1h settlement. 100% based on FRA_QRF_1H_2.0.

Shorts the perp to collect positive funding payments. Delta-neutral when paired
with a spot hedge. Optimized for Lighter's 0% fee structure.

Settlement: 1h (Lighter-style, 8760 periods/year)

Parameter equivalence from 8h backtest → 1h:
  min_consecutive_positive  2  (2 × 8h = 16h)  →  16  (1h bars = 16h)
  min_hold_periods          24 (8h bars = 8d)   → 192  (1h bars = 8d)
  max_holding_periods       42 (8h bars = 14d)  → 336  (1h bars = 14d)

Backtest reference (8h, LINK+ETH 2020–2026):
  CAGR ~8.6%  |  Sharpe ~7.7  |  MaxDD < -0.32%  |  Win Rate ~94%
"""

from __future__ import annotations

import logging
import math
import os
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar, Optional

from interfaces.strategy import BaseStrategy

if TYPE_CHECKING:
    from data.types import AnyBar, FundingRate
    from interfaces.signals import TargetPosition

log = logging.getLogger(__name__)

_SYMBOL: str = os.getenv("STRATEGY_SYMBOL", "ETHUSDT")


class FundingArb20Strategy(BaseStrategy):
    """Funding rate arbitrage 2.0 — Lighter 1h settlement."""

    display_name: ClassVar[str] = "FARB2.0"

    # --- PRODUCTION ---
    topics: ClassVar[list[str]] = [
        f"futures.{_SYMBOL}.bars.1h",
        f"futures.{_SYMBOL}.funding_rate",
    ]

    # --- FAST MODE for testing (swap above with this block) ---
    # topics: ClassVar[list[str]] = [
    #     f"futures.{_SYMBOL}.bars.1m",
    #     f"futures.{_SYMBOL}.funding_rate",
    # ]

    max_loss: ClassVar[Decimal] = Decimal("1000")

    def __init__(
        self,
        symbol: str = _SYMBOL,
        initial_equity: float = 10000.0,
        entry_threshold_annualized: float = 0.05,
        exit_threshold_annualized: float = 0.00,
        min_consecutive_positive: int = 16,    # 1h equivalent of 2×8h = 16h confirmation
        min_hold_periods: int = 192,            # 8 days × 24h
        max_volatility: float = 1.00,
        position_size_pct: float = 0.50,        # matches backtest POS_PCT
        stop_loss_pct: float = 0.05,
        regime_slope_max_per_day: float = 0.001,
        use_regime_filter: bool = True,
        use_price_stop: bool = True,
        max_holding_periods: int = 336,         # 14 days × 24h
        period_hours: int = 1,                  # Lighter 1h settlement
    ) -> None:
        self.symbol = symbol
        self._equity = initial_equity

        self.params = {
            "entry_threshold_annualized": entry_threshold_annualized,
            "exit_threshold_annualized": exit_threshold_annualized,
            "min_consecutive_positive": min_consecutive_positive,
            "min_hold_periods": min_hold_periods,
            "max_volatility": max_volatility,
            "position_size_pct": position_size_pct,
            "stop_loss_pct": stop_loss_pct,
            "regime_slope_max_per_day": regime_slope_max_per_day,
            "use_regime_filter": use_regime_filter,
            "use_price_stop": use_price_stop,
            "max_holding_periods": max_holding_periods,
            "period_hours": period_hours,
        }

        # Position state
        self.current_position: float = 0.0  # negative = short
        self.entry_price: float = 0.0
        self.holding_periods: int = 0
        self.entry_funding_rate: float = 0.0
        self.cumulative_funding_collected: float = 0.0
        self._last_funding_wall_time: Optional[datetime] = None

        # Rolling buffers
        # 151 closes needed for the 150-bar SMA regime filter (150 1h bars = ~6 days)
        self._bar_closes: deque[float] = deque(maxlen=151)
        # 24 entries to safely support min_consecutive_positive=16
        self._funding_rates: deque[float] = deque(maxlen=24)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        log.info("[FundingArb20:%s] started", self.symbol)

    def on_stop(self) -> None:
        log.info(
            "[FundingArb20:%s] stopped  position=%s  equity=%s",
            self.symbol,
            self.current_position,
            self._equity,
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_bar(self, bar: AnyBar) -> TargetPosition | None:
        if bar.symbol != self.symbol:
            return None
        self._bar_closes.append(float(bar.close))
        return None

    def on_funding_rate(self, rate: FundingRate) -> TargetPosition | None:
        if rate.symbol != self.symbol:
            return None

        current_rate = float(rate.funding_rate)

        # Guard: treat NaN-equivalent as no data
        if current_rate != current_rate:  # NaN check without math import
            return None

        self._funding_rates.append(current_rate)

        log.info(
            "[FundingArb20:%s] FUNDING  rate=%s  mark=%s",
            self.symbol,
            rate.funding_rate,
            rate.mark_price,
        )

        period_hours = self.params["period_hours"]
        periods_per_yr = (24 / period_hours) * 365  # 8760 for 1h
        annualized_rate = current_rate * periods_per_yr

        volatility = self._rolling_volatility()

        consecutive_positive = 0
        for r in reversed(list(self._funding_rates)):
            if r > 0:
                consecutive_positive += 1
            else:
                break

        regime_slope_per_day = self._regime_slope()

        if self.current_position == 0:
            regime_ok = (
                not self.params["use_regime_filter"]
                or regime_slope_per_day < self.params["regime_slope_max_per_day"]
            )

            entry_conditions = [
                annualized_rate > self.params["entry_threshold_annualized"],
                consecutive_positive >= self.params["min_consecutive_positive"],
                volatility < self.params["max_volatility"],
                regime_ok,
            ]

            if all(entry_conditions):
                log.info(
                    "[FundingArb20:%s] ENTRY signal: funding=%.1f%% annualized, "
                    "consecutive_positive=%d, vol=%.1f%%, regime_slope=%.3f%%/day",
                    self.symbol,
                    annualized_rate * 100,
                    consecutive_positive,
                    volatility * 100,
                    regime_slope_per_day * 100,
                )
                self.entry_funding_rate = current_rate
                self.holding_periods = 0
                self.cumulative_funding_collected = 0.0
                self._last_funding_wall_time = datetime.now(timezone.utc)
                return self._build_short_signal(rate)

            return None  # FLAT — no signal

        else:
            # Advance period counter at most once per period_hours real hours.
            # Wall-clock based — correct regardless of how often on_funding_rate fires.
            now = datetime.now(timezone.utc)
            if (
                self._last_funding_wall_time is None
                or now - self._last_funding_wall_time >= timedelta(hours=period_hours)
            ):
                self.holding_periods += 1
                self.cumulative_funding_collected += current_rate
                self._last_funding_wall_time = now

                # Compound funding income into equity (matches backtest: eq += pos × px × fr)
                mark_price = float(rate.mark_price)
                if mark_price > 0:
                    payment = abs(self.current_position) * mark_price * current_rate
                    self._equity += payment

                log.debug(
                    "[FundingArb20:%s] Funding period #%d accrued @ %s  "
                    "rate=%s  cumulative=%s  equity=%s",
                    self.symbol,
                    self.holding_periods,
                    now.strftime("%Y-%m-%d %H:%M UTC"),
                    current_rate,
                    self.cumulative_funding_collected,
                    self._equity,
                )

            # Hard cap: always exit regardless of min_hold
            if self.holding_periods >= self.params["max_holding_periods"]:
                log.info(
                    "[FundingArb20:%s] EXIT (max_hold=%d)  cumulative_funding=%s  equity=%s",
                    self.symbol,
                    self.holding_periods,
                    self.cumulative_funding_collected,
                    self._equity,
                )
                return self._flatten()

            # Min-hold guard: suppress all rate-based exits for the first N periods
            if self.holding_periods < self.params["min_hold_periods"]:
                return None  # HOLD

            # Rate-based exits (only evaluated after min_hold_periods)
            exit_reasons = []

            if annualized_rate < self.params["exit_threshold_annualized"]:
                exit_reasons.append(f"funding_low={annualized_rate:.1%}")

            if current_rate < 0:
                exit_reasons.append(f"funding_negative={current_rate:.6f}")

            if self.params["use_price_stop"] and self._bar_closes and self.entry_price > 0:
                current_price = self._bar_closes[-1]
                pnl_pct = (self.entry_price - current_price) / self.entry_price
                if pnl_pct < -self.params["stop_loss_pct"]:
                    exit_reasons.append(f"stop_loss={pnl_pct:.2%}")

            if exit_reasons:
                log.info(
                    "[FundingArb20:%s] EXIT (rate)  reasons=%s  "
                    "holding_periods=%d  cumulative_funding=%s  equity=%s",
                    self.symbol,
                    exit_reasons,
                    self.holding_periods,
                    self.cumulative_funding_collected,
                    self._equity,
                )
                return self._flatten()

            return None  # HOLD

    # ------------------------------------------------------------------
    # Signal builders
    # ------------------------------------------------------------------

    def _build_short_signal(self, rate: FundingRate) -> TargetPosition:
        from interfaces.signals import TargetPosition

        price = float(rate.mark_price)
        notional = self._equity * self.params["position_size_pct"]
        size = -(abs(notional / price))  # negative = short

        self.current_position = size
        self.entry_price = price

        return TargetPosition(
            symbol=self.symbol,
            quantity=Decimal(str(round(size, 6))),
            exchange="bybit_demo",
        )

    def _flatten(self) -> TargetPosition:
        from interfaces.signals import TargetPosition

        close_qty = -self.current_position  # positive = buy back the short
        self.current_position = 0.0
        self.holding_periods = 0
        self._last_funding_wall_time = None

        return TargetPosition(
            symbol=self.symbol,
            quantity=Decimal(str(round(close_qty, 6))),
            exchange="bybit_demo",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _rolling_volatility(self) -> float:
        """20-bar annualised volatility of log returns.
        Returns 0.50 (source default) if insufficient data."""
        closes = list(self._bar_closes)
        if len(closes) < 2:
            return 0.50

        log_returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(max(1, len(closes) - 20), len(closes))
            if closes[i - 1] > 0
        ]
        if not log_returns:
            return 0.50

        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / n
        periods_per_yr = (24 / self.params["period_hours"]) * 365
        return math.sqrt(variance) * math.sqrt(periods_per_yr)

    def _regime_slope(self) -> float:
        """150-bar SMA slope per day. Returns 0.0 if insufficient data."""
        closes = list(self._bar_closes)
        if len(closes) < 151:
            return 0.0

        prev_sma = sum(closes[-151:-1]) / 150
        curr_sma = sum(closes[-150:]) / 150
        if prev_sma <= 0:
            return 0.0
        return (curr_sma - prev_sma) / prev_sma * 3
