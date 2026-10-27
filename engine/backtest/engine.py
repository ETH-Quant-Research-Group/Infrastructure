"""The backtest loop: replay history through the real pipeline, in lockstep.

The same code as live, wired in one process with no NATS or Docker:

    strategy ─► StrategyRunner ─► OrderConsolidator ─► SimBroker (perp / spot)
                     ▲                 │  ledger · guard · order-size check
                     └── on_fill ◄─────┘
    funding settlements ─► FundingDB (the real split)

Events (bar closes, funding settlements) are processed strictly in time
order.  For each one the simulated clock is set to its time, the event is
handed to the strategy, and **everything it triggers** — signals, orders,
fills, ``on_fill`` follow-ups — is worked off before the clock moves on.
Nothing waits in real time, and the same inputs always give the same
result.

A bar is known at the boundary it closes on (an 8 h bar from 00:00 at
08:00).  At each moment, in this order: the funding settlement on the
positions *held* is paid and split; then the bars ending there reach the
strategy; then the funding tick.  An order placed then fills at the open of
the bar starting then, so it doesn't collect that moment's settlement —
like Bybit, where only positions held at the settlement time are paid.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engine.backtest.data import PERP_EXCHANGE, SPOT_EXCHANGE
from engine.backtest.sim_broker import PERP, SPOT, SimBroker, SimExchange
from engine.clock import SimClock
from engine.order.consolidator import OrderConsolidator
from engine.order.funding import UNATTRIBUTED, FundingDB, collect_funding
from engine.order.guard_registry import StrategyGuardRegistry
from engine.order.instruments import InstrumentCache, OrderSizing, rules_to_dict
from engine.order.ledger import LedgerDB
from engine.order.performance import PnLPoint, Sharpe, sharpe, unrealized_at
from engine.strategy.guard import StrategyGuard
from engine.strategy.pnl_calc import PnLCalc
from engine.strategy.runner import StrategyRunner
from execution.types import Order, OrderSide, OrderType

if TYPE_CHECKING:
    from datetime import datetime

    from engine.backtest.data import HistoryData
    from engine.order.ledger import Holding, Trade
    from execution.types import FillConfirmation
    from interfaces.broker import BaseBroker
    from interfaces.signals import TargetPosition
    from interfaces.strategy import BaseStrategy

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    strategy: str
    start: datetime
    end: datetime
    capital: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    funding: Decimal
    fees: Decimal
    net_pnl: Decimal | None
    max_drawdown: Decimal
    sharpe: Sharpe
    trades: list[Trade]
    holdings: list[Holding]
    outcomes: dict[str, int]  # fill status → count
    unattributed_funding: Decimal
    guard_tripped: bool
    pnl: list[PnLPoint] = field(default_factory=list)

    @property
    def return_pct(self) -> Decimal | None:
        if self.net_pnl is None or not self.capital:
            return None
        return self.net_pnl / self.capital * 100

    def summary(self) -> str:
        def money(v: Decimal | None) -> str:
            return "unknown" if v is None else f"{v:,.2f}"

        ret = self.return_pct
        lines = [
            f"Backtest {self.strategy}: {self.start:%Y-%m-%d %H:%M} -> {self.end:%Y-%m-%d %H:%M} UTC",
            f"  net PnL      {money(self.net_pnl)} USDT"
            + (f"  ({ret:.2f}% of {self.capital:,.0f})" if ret is not None else ""),
            f"  realized     {money(self.realized_pnl)}",
            f"  unrealized   {money(self.unrealized_pnl)}  (open positions at the last price)",
            f"  funding      {money(self.funding)}",
            f"  fees         {money(self.fees)}",
            f"  max drawdown {money(self.max_drawdown)}",
            f"  Sharpe       {'n/a' if self.sharpe.value is None else f'{self.sharpe.value:.2f}'}"
            f"  (daily, {self.sharpe.returns} returns)",
            f"  orders       {len(self.trades)}  outcomes: "
            + (", ".join(f"{k} {v}" for k, v in sorted(self.outcomes.items())) or "none"),
        ]
        if self.unattributed_funding:
            lines.append(f"  unattributed funding {money(self.unattributed_funding)}")
        if self.guard_tripped:
            lines.append("  GUARD TRIPPED during the run - later signals were blocked")
        open_holdings = [h for h in self.holdings if h.quantity != 0]
        if open_holdings:
            lines.append("  open at the end: " + ", ".join(
                f"{h.exchange} {h.symbol} {h.quantity}" for h in open_holdings
            ))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        def s(v: Decimal | None) -> str | None:
            return None if v is None else str(v)

        return {
            "strategy": self.strategy,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "capital": str(self.capital),
            "net_pnl": s(self.net_pnl),
            "return_pct": s(self.return_pct),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": s(self.unrealized_pnl),
            "funding": str(self.funding),
            "fees": str(self.fees),
            "max_drawdown": str(self.max_drawdown),
            "sharpe": self.sharpe.value,
            "sharpe_returns": self.sharpe.returns,
            "outcomes": self.outcomes,
            "unattributed_funding": str(self.unattributed_funding),
            "guard_tripped": self.guard_tripped,
            "trades": [
                {
                    "order_link_id": t.order_link_id, "exchange": t.exchange, "symbol": t.symbol,
                    "side": t.side.value, "status": t.status, "requested_qty": str(t.requested_qty),
                    "filled_qty": str(t.filled_qty), "avg_price": s(t.avg_price),
                    "fees": {c: str(a) for c, a in t.fees.items()}, "at": t.created_at.isoformat(),
                }
                for t in self.trades
            ],
            "pnl": [
                {"at": p.taken_at.isoformat(), "net": s(p.net_pnl), "funding": str(p.funding)}
                for p in self.pnl
            ],
        }


class _NoBus:
    """The runner's bus, unused: the backtest hands it events directly."""

    def subscribe(self, *topics: str) -> asyncio.Queue[Any]:
        return asyncio.Queue()

    def unsubscribe(self, queue: asyncio.Queue[Any]) -> None:
        pass


def _bar_topics(strategy: BaseStrategy) -> tuple[set[str], set[str]]:
    """(bar topics, funding topics) the strategy subscribes to."""
    bars = {t for t in strategy.topics if ".bars." in t}
    funding = {t for t in strategy.topics if t.endswith(".funding_rate")}
    return bars, funding


async def run_backtest(
    strategy: BaseStrategy,
    data: HistoryData,
    workdir: Path,
    *,
    capital: Decimal = Decimal(60_000),
    max_loss: Decimal | None = None,
    include_unrealized: bool = False,
    unrealized_every: int = 0,
    close_on_trip: bool = False,
    slippage_bps: Decimal = Decimal(0),
    perp_fee: Decimal = Decimal("0.00055"),
    spot_fee: Decimal = Decimal("0.001"),
) -> BacktestResult:
    """Run *strategy* through *data* and return the result.  The ledger and
    funding files go into *workdir* (kept, for inspection).  The guard is
    checked after every event; *unrealized_every* counts those checks
    (live counts its timer checks instead)."""
    name = type(strategy).__name__
    workdir.mkdir(parents=True, exist_ok=True)
    first = min(
        (b.timestamp for bars in data.bars.values() for b in bars), default=data.start
    )
    clock = SimClock(first)
    sim = SimExchange(
        clock, data.bars, perp_fee=perp_fee, spot_fee=spot_fee,
        slippage_bps=slippage_bps, rules=data.rules,
    )
    perp = SimBroker(sim, PERP_EXCHANGE, PERP)
    spot = SimBroker(sim, SPOT_EXCHANGE, SPOT)
    brokers: dict[str, BaseBroker] = {PERP_EXCHANGE: perp, SPOT_EXCHANGE: spot}

    ledger = LedgerDB(workdir / "ledger.db", now=clock.now)
    funding_path = workdir / "funding.db"
    funding_db = FundingDB(funding_path)
    instruments = InstrumentCache()
    await instruments.load(brokers)
    limit = max_loss if max_loss is not None else type(strategy).max_loss
    guard = StrategyGuardRegistry(
        limit, ledger=ledger, funding_path=funding_path,
        prices=sim.price, include_unrealized=include_unrealized,
        unrealized_every=unrealized_every, close_on_trip=close_on_trip,
    )

    def order_factory(
        symbol: str, side: OrderSide, qty: Decimal, _price: Decimal, exchange: str = ""
    ) -> Order:
        return Order(
            symbol=symbol, side=side, order_type=OrderType.MARKET,
            quantity=instruments.quantize(exchange, symbol, qty), price=Decimal(0),
        )

    targets: asyncio.Queue[TargetPosition] = asyncio.Queue()
    fills: asyncio.Queue[FillConfirmation] = asyncio.Queue()
    consolidator = OrderConsolidator(
        target_queue=targets, brokers=brokers, default_exchange=PERP_EXCHANGE,
        fills_queue=fills, order_factory=order_factory, guard_registry=guard,
        ledger=ledger, order_check=instruments.check,
    )
    runner = StrategyRunner(
        strategy_id=name, strategy=strategy, bus=_NoBus(), topics=list(strategy.topics),
        target_queue=targets, guard=StrategyGuard(max_loss=limit), pnlcalc=PnLCalc(),
    )

    # The strategy sees simulated time and the real order-size rules.
    strategy.clock = clock
    symbols = sorted(data.bars)
    strategy.order_sizing = OrderSizing.from_dicts(
        rules_to_dict(ex, r) for (ex, sym), r in data.rules.items() if sym in symbols
    )

    bar_topics, funding_topics = _bar_topics(strategy)
    interval = data.interval
    history = sorted(
        (b for sym, bars in data.bars.items()
         if f"futures.{sym}.bars.{interval}" in bar_topics
         for b in bars if b.timestamp + timedelta(seconds=b.interval_seconds) <= data.start),
        key=lambda b: b.close_time,
    )
    # At each moment: 0 = pay the funding settlement on what is held,
    # 1 = bars that end here (a bar is known at the boundary it closes on),
    # 2 = the funding tick to the strategy.  An order placed at 1 or 2 fills
    # at the bar opening now, so it doesn't collect this settlement.
    _SETTLE, _BAR, _TICK = 0, 1, 2
    timeline: list[tuple[datetime, int, str, Any]] = []
    for sym, bars in data.bars.items():
        if f"futures.{sym}.bars.{interval}" in bar_topics:
            for b in bars:
                known = b.timestamp + timedelta(seconds=b.interval_seconds)
                if data.start < known <= data.end:
                    timeline.append((known, _BAR, sym, b))
    for sym, rates in data.funding.items():
        timeline += [(r.timestamp, _SETTLE, sym, r) for r in rates]
        if f"futures.{sym}.funding_rate" in funding_topics:
            timeline += [(r.timestamp, _TICK, sym, r) for r in rates]
    timeline.sort(key=lambda e: (e[0], e[1], e[2]))

    outcomes: Counter[str] = Counter()
    points: list[PnLPoint] = []

    async def drain() -> None:
        """Work off everything the last event triggered."""
        while not targets.empty() or not fills.empty():
            while not targets.empty():
                await consolidator.handle(targets.get_nowait())
            while not fills.empty():
                fill = fills.get_nowait()
                outcomes[fill.status.value] += 1
                if fill.quantity != 0:  # like strategy_worker._listen_fills
                    runner.pnl_calc.on_fill(
                        fill.symbol, fill.quantity, fill.fill_price, exchange=fill.exchange
                    )
                    await runner.notify_fill(fill)

    def record_point() -> None:
        if name not in ledger.strategy_ids():
            return
        snap = ledger.snapshot(name)
        prices = {(h.exchange, h.symbol): sim.mark(h.symbol) for h in snap.holdings}
        unrealized = unrealized_at(snap.holdings, prices)
        received = funding_db.total(name)
        points.append(PnLPoint(
            strategy=name, strategy_num=snap.strategy_num, taken_at=clock.now(),
            orders=snap.counter, realized_pnl=snap.realized_pnl, unrealized_pnl=unrealized,
            fees_paid=snap.fees_paid, funding=received,
            net_pnl=None if unrealized is None
            else snap.realized_pnl + unrealized + received - snap.fees_paid,
        ))

    runner.begin(history)
    for when, kind, sym, event in timeline:
        clock.set(max(when, clock.now()))
        if kind == _SETTLE:
            # Pay and split the settlement on what is held right now.
            if perp.settle_funding(sym, event.funding_rate, when) is not None:
                await collect_funding(ledger, funding_db, perp, when,
                                      lookback=timedelta(days=36_500))
            record_point()
            continue
        await runner.process(event)
        await drain()
        await guard.check_all()
        consolidator.close_tripped()
        await consolidator.run_waiting_closes()
        await drain()
        record_point()
    strategy.on_stop()

    holdings = ledger.holdings(name)
    last = points[-1] if points else None
    result = BacktestResult(
        strategy=name, start=data.start, end=data.end, capital=capital,
        realized_pnl=last.realized_pnl if last else Decimal(0),
        unrealized_pnl=last.unrealized_pnl if last else Decimal(0),
        funding=last.funding if last else Decimal(0),
        fees=last.fees_paid if last else Decimal(0),
        net_pnl=last.net_pnl if last else Decimal(0),
        max_drawdown=_max_drawdown(points),
        sharpe=sharpe(points),
        trades=list(reversed(ledger.trades(name, limit=None))),
        holdings=holdings,
        outcomes=dict(outcomes),
        unattributed_funding=funding_db.total(UNATTRIBUTED),
        guard_tripped=not guard.is_active(name),
        pnl=points,
    )
    guard.close()
    funding_db.close()
    ledger.close()
    return result


def _max_drawdown(points: list[PnLPoint]) -> Decimal:
    """Largest fall of net PnL from a previous peak (≥ 0), in USDT."""
    peak: Decimal | None = None
    worst = Decimal(0)
    for p in points:
        if p.net_pnl is None:
            continue
        peak = p.net_pnl if peak is None else max(peak, p.net_pnl)
        worst = max(worst, peak - p.net_pnl)
    return worst
