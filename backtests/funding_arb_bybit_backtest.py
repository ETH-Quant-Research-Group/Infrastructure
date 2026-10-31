"""Whole-system backtest for FundingArbBybitStrategy.

Nothing about the trading logic is re-implemented here.  The backtest wires up
the production objects exactly as the workers do and only replaces Bybit:

    real  strategies.funding_arb_bybit.FundingArbBybitStrategy
    real  engine.strategy.runner.StrategyRunner      (+ in-process StrategyGuard)
    real  engine.order.consolidator.OrderConsolidator (+ InstrumentCache rounding / order_check, as deployed)
    real  engine.order.guard_registry.StrategyGuardRegistry
    real  workers.consolidator_worker._order_factory  (lot-size quantisation)
    real  data.connectors.bybit_linear 1h fetch + _aggregate_to_8h (same 8h bars as live)
    SIM   Bybit perp + spot: fees, slippage, lot/precision/min-notional rejections,
          spot fee taken in base coin on buys, funding paid at every settlement
          (including 4h/1h intervals) on the mark price, unified USDT wallet.

Event flow per hour (simulated clock given to the strategy as ``strategy.clock``):
    prices update -> funding settles on open perp positions ->
    at 00/08/16 UTC: 8h bar per symbol, then the settled funding rate as the
    first funding tick of the window -> strategy -> runner -> consolidator ->
    sim broker -> FillConfirmation -> strategy.on_fill -> spot hedge -> ...
    other hours: a funding tick (only the recovery paths react to these)

Outputs (``--out``): report.md, equity.csv, trades.csv, incidents.csv,
grid.csv / walk_forward.csv in grid mode, PNG charts if matplotlib is installed.

Run from the repo root::

    uv run python -m backtests.funding_arb_bybit_backtest --start 2022-01-01
    uv run python -m backtests.funding_arb_bybit_backtest --grid small --workers 8
    uv run python -m backtests.funding_arb_bybit_backtest --set entry_threshold_annualized=0.2
    uv run python -m backtests.funding_arb_bybit_backtest --synthetic   # offline self-test

Only repo dependencies are required (httpx, nats-py, websockets); matplotlib is
optional for charts.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import math
import os
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

# Make the repo importable when run as a plain script too.
_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import httpx  # noqa: E402

import strategies.funding_arb_bybit as strat_mod  # noqa: E402
import workers.consolidator_worker as cw  # noqa: E402
from data.connectors.bybit_linear import BybitLinearConnector, _aggregate_to_8h  # noqa: E402
from data.connectors.types import KlineInterval  # noqa: E402
from data.normalizers.bybit_linear import to_time_bar  # noqa: E402
from data.types import FundingRate, TimeBar  # noqa: E402
from engine.order.consolidator import OrderConsolidator  # noqa: E402
from engine.order.guard_registry import StrategyGuardRegistry  # noqa: E402
from engine.strategy.guard import StrategyGuard  # noqa: E402
from engine.strategy.pnl_calc import PnLCalc  # noqa: E402
from engine.strategy.runner import StrategyRunner  # noqa: E402
from execution.types import (  # noqa: E402
    FundingSettlement,
    InstrumentRules,
    Order,
    OrderResult,
    OrderSide,
    OrderState,
    OrderStatus,
    PerpOrder,
    PerpPosition,
    Position,
)
from interfaces.broker import BaseBroker  # noqa: E402

FundingArbBybitStrategy = strat_mod.FundingArbBybitStrategy

H1 = 3_600_000
H8 = 8 * H1
REST = "https://api.bybit.com"
STRATEGY_ID = "FundingArbBybitStrategy"
PERP_EX, SPOT_EX = "bybit_demo", "bybit_spot"
D0 = Decimal(0)

log = logging.getLogger("backtest")


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


# =============================================================================
# Market data
# =============================================================================

@dataclass
class MarketData:
    symbols: list[str]
    hours: list[int]                                  # hour END timestamps (ms), sorted
    perp: dict[str, dict[int, Decimal]]               # close of the 1h bar ending at ts
    spot: dict[str, dict[int, Decimal]]
    mark: dict[str, dict[int, Decimal]]
    bars8h: dict[str, dict[int, TimeBar]]             # keyed by the boundary the bar closes on
    funding: dict[str, dict[int, Decimal]]            # settlement ts -> rate
    instruments: dict[str, dict[str, Decimal]]
    notes: list[str] = field(default_factory=list)
    synthetic: bool = False


async def _get(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    for attempt in range(6):
        try:
            r = await client.get(path, params=params)
            r.raise_for_status()
            j = r.json()
            if j.get("retCode") != 0:
                raise RuntimeError(f"{path} {params}: {j.get('retMsg')}")
            return j["result"]
        except Exception as exc:  # noqa: BLE001
            if attempt == 5:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))
            log.warning("retry %s %s: %s", path, params.get("symbol"), exc)
    raise RuntimeError("unreachable")


async def _fetch_funding_history(client, sym: str, start_ms: int, end_ms: int) -> list[tuple[int, str]]:
    """Page BACKWARD by endTime (Bybit returns newest-first)."""
    out: dict[int, str] = {}
    end = end_ms
    while True:
        res = await _get(client, "/v5/market/funding/history",
                         {"category": "linear", "symbol": sym, "endTime": end, "limit": 200})
        rows = res.get("list", [])
        if not rows:
            break
        for r in rows:
            ts = round(int(r["fundingRateTimestamp"]) / 60_000) * 60_000
            if start_ms <= ts <= end_ms:
                out[ts] = r["fundingRate"]
        oldest = min(int(r["fundingRateTimestamp"]) for r in rows)
        if oldest <= start_ms or len(rows) < 200:
            break
        end = oldest - 1
        await asyncio.sleep(0.05)
    return sorted(out.items())


async def _fetch_kline_1h(client, path: str, category: str, sym: str,
                          start_ms: int, end_ms: int) -> list[tuple[int, str]]:
    """(hour_end_ms, close) for /v5/market/kline or /v5/market/mark-price-kline."""
    out: dict[int, str] = {}
    end = end_ms
    while True:
        res = await _get(client, path, {"category": category, "symbol": sym, "interval": "60",
                                        "start": start_ms, "end": end, "limit": 1000})
        rows = res.get("list", [])
        if not rows:
            break
        for r in rows:
            out[int(r[0]) + H1] = r[4]
        oldest = min(int(r[0]) for r in rows)
        if oldest <= start_ms or len(rows) < 1000:
            break
        end = oldest - 1
        await asyncio.sleep(0.05)
    return sorted(out.items())


async def _fetch_instruments(client, sym: str) -> dict[str, str]:
    lin = (await _get(client, "/v5/market/instruments-info",
                      {"category": "linear", "symbol": sym}))["list"][0]["lotSizeFilter"]
    out = {"perp_step": lin["qtyStep"], "perp_min_qty": lin["minOrderQty"],
           "perp_min_notional": lin.get("minNotionalValue") or "5"}
    sp_list = (await _get(client, "/v5/market/instruments-info",
                          {"category": "spot", "symbol": sym}))["list"]
    if sp_list:
        sp = sp_list[0]["lotSizeFilter"]
        out.update(spot_step=sp.get("basePrecision") or "0.00000001",
                   spot_min_qty=sp.get("minOrderQty") or "0",
                   spot_min_amt=sp.get("minOrderAmt") or "1")
    else:
        out.update(spot_step="0", spot_min_qty="0", spot_min_amt="0")  # no spot market
    return out


async def download(symbols: list[str], start: str, end: str | None, cache: Path) -> MarketData:
    cache.mkdir(parents=True, exist_ok=True)
    s_ms = int(datetime.fromisoformat(start).replace(tzinfo=UTC).timestamp() * 1000)
    e_ms = int((datetime.fromisoformat(end).replace(tzinfo=UTC) if end
                else datetime.now(UTC)).timestamp() * 1000) // H1 * H1
    warm = s_ms - 30 * H8  # 30 windows of warm-up for the vol / consecutive filters
    notes: list[str] = []
    raw: dict[str, dict[str, Any]] = {}
    conn = BybitLinearConnector(timeout=20)
    async with httpx.AsyncClient(base_url=REST, timeout=20) as client:
        for sym in symbols:
            fn = cache / f"{sym}_{start}_{end or 'now'}.json"
            if fn.exists():
                raw[sym] = json.loads(fn.read_text(encoding="utf-8"))
                print(f"[data] {sym}: cache")
                continue
            print(f"[data] {sym}: downloading (perp 1h via repo connector, spot, mark, funding)")
            perp_1h = [k async for k in conn.fetch_klines(sym, KlineInterval.H1,
                                                         start_ms=warm, end_ms=e_ms)]
            d = {
                "perp_1h": perp_1h,
                "spot": await _fetch_kline_1h(client, "/v5/market/kline", "spot", sym, warm, e_ms),
                "mark": await _fetch_kline_1h(client, "/v5/market/mark-price-kline", "linear",
                                              sym, warm, e_ms),
                "funding": await _fetch_funding_history(client, sym, warm, e_ms),
                "instrument": await _fetch_instruments(client, sym),
            }
            # Cross-check the repo's own historical funding fetch.
            try:
                repo_rates = [r async for r in conn.fetch_funding_rates(sym, start_ms=warm, end_ms=e_ms)]
                d["repo_funding_count"] = len({r["funding_time_ms"] for r in repo_rates})
            except Exception as exc:  # noqa: BLE001
                d["repo_funding_count"] = f"error: {exc}"
            fn.write_text(json.dumps(d), encoding="utf-8")
            raw[sym] = d
    await conn.aclose()
    for sym in symbols:
        rc, n = raw[sym].get("repo_funding_count"), len(raw[sym]["funding"])
        if isinstance(rc, str) or (isinstance(rc, int) and rc < 0.95 * n):
            notes.append(f"{sym}: repo `BybitLinearConnector.fetch_funding_rates` returned {rc} "
                         f"settlements for the period, the backward pager returned {n}.")
    return build_market(symbols, raw, notes)


def build_market(symbols: list[str], raw: dict, notes: list[str], synthetic=False) -> MarketData:
    perp, spot, mark, bars, fund, inst = {}, {}, {}, {}, {}, {}
    hours: set[int] = set()
    for sym in symbols:
        d = raw[sym]
        p1 = d["perp_1h"]
        perp[sym] = {k["open_time_ms"] + H1: Decimal(k["close"]) for k in p1}
        spot[sym] = {t: Decimal(c) for t, c in d["spot"]}
        mark[sym] = {t: Decimal(c) for t, c in d["mark"]} or dict(perp[sym])
        fund[sym] = {t: Decimal(r) for t, r in d["funding"]}
        inst[sym] = {k: Decimal(v) for k, v in d["instrument"].items()}
        # exactly the live 8h aggregation; keep only complete 8-hour buckets
        by_bucket = Counter((k["open_time_ms"] // H8) * H8 for k in p1)
        bars[sym] = {}
        for b in _aggregate_to_8h(p1):
            if by_bucket[b["open_time_ms"]] == 8:
                bars[sym][b["open_time_ms"] + H8] = to_time_bar(b, symbol=sym, interval=KlineInterval.H8)
        hours |= set(perp[sym])
    return MarketData(symbols, sorted(hours), perp, spot, mark, bars, fund, inst, notes, synthetic)


def load_cached(symbols, start, end, cache: Path) -> MarketData:
    raw = {}
    for sym in symbols:
        raw[sym] = json.loads((cache / f"{sym}_{start}_{end or 'now'}.json").read_text(encoding="utf-8"))
    return build_market(symbols, raw, [])


def synthetic_market(symbols: list[str], days: int = 500, seed: int = 11) -> MarketData:
    """Offline self-test data.  Says NOTHING about real profitability."""
    rng = random.Random(seed)
    t0 = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
    n = days * 24
    raw = {}
    start_px = {"ETHUSDT": 3000, "LINKUSDT": 15, "SOLUSDT": 150, "XRPUSDT": 0.6,
                "DOGEUSDT": 0.12, "AVAXUSDT": 30, "DOTUSDT": 6, "ATOMUSDT": 8}
    for k, sym in enumerate(symbols):
        px, regime, x = start_px.get(sym, 10), 0, 0.0001
        perp_1h, spot_l, mark_l, fund = [], [], [], []
        sigma_h = 0.8 / math.sqrt(24 * 365)
        for i in range(n):
            t_open = t0 + i * H1
            px *= math.exp(rng.gauss(0, sigma_h))
            if (t_open + H1) % H8 == 0:
                u = rng.random()
                regime = {0: 1 if u < .03 else (2 if u < .035 else 0),
                          1: 0 if u < .07 else 1, 2: 0 if u < .1 else 2}[regime]
                mu = {0: 0.0001, 1: 0.0004, 2: -0.0002}[regime]
                x = 0.75 * x + 0.25 * mu + rng.gauss(0, 0.00006)
                r = 0.0001 if regime == 0 and rng.random() < .6 else x
                fund.append((t_open + H1, f"{max(min(r, .0075), -.0075):.8f}"))
            basis = 3 * x + rng.gauss(0, 0.0003)
            p = px * (1 + basis)
            perp_1h.append({"open_time_ms": t_open, "open": f"{p:.6g}", "high": f"{p:.6g}",
                            "low": f"{p:.6g}", "close": f"{p:.6g}", "volume": "1000",
                            "close_time_ms": t_open, "quote_volume": "1", "trade_count": 0,
                            "taker_buy_volume": "0", "taker_buy_quote_volume": "0"})
            spot_l.append((t_open + H1, f"{px:.6g}"))
            mark_l.append((t_open + H1, f"{p:.6g}"))
        hi = start_px.get(sym, 10) > 1
        raw[sym] = {"perp_1h": perp_1h, "spot": spot_l, "mark": mark_l, "funding": fund,
                    "instrument": {"perp_step": "0.01" if hi else "1", "perp_min_qty": "0.01" if hi else "1",
                                   "perp_min_notional": "5",
                                   # coarse spot precision on two coins to exercise rejections
                                   "spot_step": "0.1" if sym == "DOGEUSDT" else
                                                ("0.01" if sym == "XRPUSDT" else "0.00001"),
                                   "spot_min_qty": "0", "spot_min_amt": "1"}}
    return build_market(symbols, raw, ["SYNTHETIC DATA — pipeline self-test only."], synthetic=True)


# =============================================================================
# Simulated Bybit (unified account: one USDT wallet, perp + spot)
# =============================================================================

@dataclass
class Costs:
    perp_fee: Decimal = Decimal("0.00055")
    spot_fee: Decimal = Decimal("0.001")
    slippage: Decimal = Decimal("0.0002")   # per order, fraction of price


class SimAccount:
    def __init__(self, usdt: Decimal, symbols: list[str]) -> None:
        self.usdt = usdt
        self.perp_px = {s: D0 for s in symbols}
        self.spot_px = {s: D0 for s in symbols}
        self.mark_px = {s: D0 for s in symbols}
        self.perp_qty = {s: D0 for s in symbols}
        self.perp_avg = {s: D0 for s in symbols}
        self.coin = {s: D0 for s in symbols}
        # attribution
        self.sym_cash = defaultdict(lambda: D0)     # all USDT flows attributed to a symbol
        self.funding = defaultdict(lambda: D0)
        self.fees = defaultdict(lambda: D0)
        self.orders: list[dict] = []
        self.rejects: list[dict] = []

    def perp_unreal(self, s: str) -> Decimal:
        return (self.mark_px[s] - self.perp_avg[s]) * self.perp_qty[s]

    def sym_equity(self, s: str) -> Decimal:
        return self.sym_cash[s] + self.coin[s] * self.spot_px[s] + self.perp_unreal(s)

    def equity(self) -> Decimal:
        return self.usdt + sum(self.coin[s] * self.spot_px[s] + self.perp_unreal(s) for s in self.coin)

    def cash(self, s: str, amount: Decimal) -> None:
        self.usdt += amount
        self.sym_cash[s] += amount


def _on_step(q: Decimal, step: Decimal) -> bool:
    return step > 0 and (q / step) == (q / step).to_integral_value()


class _SimBrokerBase(BaseBroker):
    def __init__(self, acct: SimAccount, md: MarketData, costs: Costs, clock) -> None:
        self.a, self.md, self.c, self.clock = acct, md, costs, clock
        self._statuses: dict[str, OrderStatus] = {}   # order id / client tag -> final status

    def _reject(self, ex: str, order: Order, why: str) -> OrderResult:
        self.a.rejects.append({"ts": self.clock(), "exchange": ex, "symbol": order.symbol,
                               "side": str(order.side), "qty": str(order.quantity), "reason": why})
        return OrderResult(order_id=None, order=order, error=why)

    def _filled(self, order_id: str, order: Order, fill: Decimal,
                fees: dict[str, Decimal]) -> OrderResult:
        """Fills are immediate and complete: keep the final status for
        ``order_status``, which the consolidator polls for the real fill."""
        status = OrderStatus(
            order_id=order_id, client_order_id=order.client_order_id, symbol=order.symbol,
            side=order.side, state=OrderState.FILLED, quantity=order.quantity,
            filled_qty=order.quantity, leaves_qty=D0, avg_price=fill,
            filled_value=order.quantity * fill, fees=fees, raw_status="Filled",
        )
        self._statuses[order_id] = status
        if order.client_order_id:
            self._statuses[order.client_order_id] = status
        return OrderResult(order_id=order_id, order=order, error=None)

    async def order_status(self, order_id: str | None = None, client_order_id: str | None = None,
                           symbol: str | None = None) -> OrderStatus | None:
        return self._statuses.get(order_id or "") or self._statuses.get(client_order_id or "")

    async def order_history(self, start: datetime, end: datetime) -> list[OrderStatus]:
        return []

    async def list_positions(self) -> list[Position]:
        return [p for s in self.md.symbols if (p := await self.position(s)) is not None]

    _RULE_KEYS: tuple[str, str, str] = ("", "", "")   # (step, min qty, min value) in md.instruments

    async def instrument_rules(self) -> list[InstrumentRules]:
        """The simulated symbols' rules, loaded into the consolidator's
        InstrumentCache like Bybit's are live (rounding + order_check)."""
        step, min_qty, min_value = self._RULE_KEYS
        out = []
        for s in self.md.symbols:
            ins = self.md.instruments[s]
            if Decimal(ins[step]) > 0:
                out.append(InstrumentRules(symbol=s, qty_step=Decimal(ins[step]),
                                           min_qty=Decimal(ins[min_qty]) or None,
                                           min_notional=Decimal(ins[min_value]) or None))
        return out

    async def funding_settlements(self, start: datetime, end: datetime) -> list[FundingSettlement]:
        return []   # funding is paid straight into the account (settle_funding)

    async def cancel_order(self, order_id: str, symbol: str | None = None) -> OrderResult:
        return OrderResult(order_id=order_id, order=None, error=None)

    async def cancel_all_orders(self, symbol: str | None = None) -> OrderResult:
        return OrderResult(order_id=None, order=None, error=None)

    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        return []

    async def aclose(self) -> None:
        return None


class SimPerpBroker(_SimBrokerBase):
    _RULE_KEYS = ("perp_step", "perp_min_qty", "perp_min_notional")

    async def place_order(self, order: Order) -> OrderResult:
        a, s, q = self.a, order.symbol, order.quantity
        ins = self.md.instruments[s]
        px = a.perp_px[s]
        reduce = isinstance(order, PerpOrder) and order.reduce_only
        if q <= 0:
            return self._reject(PERP_EX, order, "qty <= 0")
        if not _on_step(q, ins["perp_step"]):
            return self._reject(PERP_EX, order, f"Qty invalid: not a multiple of qtyStep {ins['perp_step']}")
        if q < ins["perp_min_qty"]:
            return self._reject(PERP_EX, order, f"Qty below minOrderQty {ins['perp_min_qty']}")
        if not reduce and q * px < ins["perp_min_notional"]:
            return self._reject(PERP_EX, order, f"Notional below minNotionalValue {ins['perp_min_notional']}")
        sign = Decimal(1) if order.side is OrderSide.BUY else Decimal(-1)
        old = a.perp_qty[s]
        if reduce and (old == 0 or (old > 0) == (sign > 0) or q > abs(old)):
            return self._reject(PERP_EX, order, "reduce-only order would increase position")
        fill = px * (1 + sign * self.c.slippage)
        new = old + sign * q
        if old == 0 or (old > 0) == (sign > 0):          # open / add
            a.perp_avg[s] = (abs(old) * a.perp_avg[s] + q * fill) / abs(new)
        else:                                            # reduce / close / flip
            closed = min(q, abs(old))
            a.cash(s, (fill - a.perp_avg[s]) * closed * (1 if old > 0 else -1))
            if new == 0:
                a.perp_avg[s] = D0
            elif (new > 0) != (old > 0):
                a.perp_avg[s] = fill
        a.perp_qty[s] = new
        fee = q * fill * self.c.perp_fee
        a.cash(s, -fee)
        a.fees[s] += fee
        a.orders.append({"ts": self.clock(), "exchange": PERP_EX, "symbol": s,
                         "side": str(order.side), "qty": str(q), "price": str(fill)})
        # Like the live BybitBroker: the consolidator reads the real fill via order_status.
        return self._filled(f"p{len(a.orders)}", order, fill, {"USDT": fee})

    async def position(self, symbol: str) -> PerpPosition | None:
        q = self.a.perp_qty[symbol]
        if q == 0:
            return None
        return PerpPosition(symbol=symbol, quantity=q, avg_entry_price=self.a.perp_avg[symbol],
                            unrealized_pnl=self.a.perp_unreal(symbol), realized_pnl=D0,
                            mark_price=self.a.mark_px[symbol])

    def settle_funding(self, ts: int) -> None:
        for s in self.md.symbols:
            r = self.md.funding[s].get(ts)
            q = self.a.perp_qty[s]
            if r is None or q == 0:
                continue
            pay = -q * self.a.mark_px[s] * r          # short (q<0) receives when r>0
            self.a.cash(s, pay)
            self.a.funding[s] += pay


class SimSpotBroker(_SimBrokerBase):
    _RULE_KEYS = ("spot_step", "spot_min_qty", "spot_min_amt")

    async def place_order(self, order: Order) -> OrderResult:
        a, s, q = self.a, order.symbol, order.quantity
        ins = self.md.instruments[s]
        px = a.spot_px[s]
        if ins["spot_step"] == 0 or px == 0:
            return self._reject(SPOT_EX, order, "no spot market / no price")
        if q <= 0:
            return self._reject(SPOT_EX, order, "qty <= 0")
        if not _on_step(q, ins["spot_step"]):
            return self._reject(SPOT_EX, order, f"Order quantity has too many decimals (basePrecision {ins['spot_step']})")
        if q < ins["spot_min_qty"]:
            return self._reject(SPOT_EX, order, f"Qty below minOrderQty {ins['spot_min_qty']}")
        if q * px < ins["spot_min_amt"]:
            return self._reject(SPOT_EX, order, f"Order value below minOrderAmt {ins['spot_min_amt']}")
        if order.side is OrderSide.BUY:
            fill = px * (1 + self.c.slippage)
            cost = q * fill
            if a.usdt < cost:
                return self._reject(SPOT_EX, order, "Insufficient balance (USDT)")
            a.cash(s, -cost)
            got = (q * (1 - self.c.spot_fee)).quantize(Decimal("1e-8"), rounding=ROUND_DOWN)
            a.coin[s] += got
            a.fees[s] += (q - got) * fill
            fees = {s.removesuffix("USDT"): q - got}   # Bybit spot buys pay in the base coin
        else:
            if a.coin[s] < q:
                return self._reject(SPOT_EX, order, "Insufficient balance (coin)")
            fill = px * (1 - self.c.slippage)
            a.coin[s] -= q
            proceeds = q * fill
            fee = proceeds * self.c.spot_fee
            a.cash(s, proceeds - fee)
            a.fees[s] += fee
            fees = {"USDT": fee}
        a.orders.append({"ts": self.clock(), "exchange": SPOT_EX, "symbol": s,
                         "side": str(order.side), "qty": str(q), "price": str(fill)})
        return self._filled(f"s{len(a.orders)}", order, fill, fees)

    async def position(self, symbol: str) -> Position | None:
        q = self.a.coin[symbol]
        if q <= 0:
            return None
        return Position(symbol=symbol, quantity=q, avg_entry_price=D0,
                        unrealized_pnl=D0, realized_pnl=D0)


# =============================================================================
# Simulated clock for the strategy module
# =============================================================================

_CLOCK = [datetime(2020, 1, 1, tzinfo=UTC)]


class _SimClock:
    """Handed to the strategy as ``strategy.clock``: it reads the time only
    through ``self.now()`` (interfaces/strategy.py)."""

    def now(self) -> datetime:
        return _CLOCK[0]


class _CountingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.counts: Counter[str] = Counter()
        self.samples: dict[str, str] = {}

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        body = msg.split("]", 1)[-1].strip()
        key = " ".join(w for w in body.split()[:3] if not any(ch.isdigit() for ch in w))
        self.counts[key] += 1
        self.samples.setdefault(key, f"{iso(int(_CLOCK[0].timestamp() * 1000))}  {msg[:160]}")


# =============================================================================
# The backtest
# =============================================================================

@dataclass
class Config:
    wallet: Decimal = Decimal("60000")
    strategy_max_loss: Decimal | None = Decimal("500")    # in-process guard (MAX_DRAWDOWN)
    registry_max_loss: Decimal | None = Decimal("1000")   # consolidator guard (DEFAULT_MAX_LOSS)
    # Live no longer sets one (consolidator_worker.main): the real per-symbol
    # rules (InstrumentCache) round and check every order instead.
    min_order_size: Decimal = Decimal("0")
    ticks_per_window: int = 8                             # 8 = one funding tick per hour
    naked_usd: Decimal = Decimal("100")                   # net-delta notional counted as unhedged
    start_ms: int = 0                                     # trading starts here (earlier = warm-up)
    costs: Costs = field(default_factory=Costs)


async def run_backtest(md: MarketData, params: dict, cfg: Config, quiet: bool = True) -> dict:
    syms = md.symbols
    acct = SimAccount(cfg.wallet, syms)
    clock_ms = lambda: int(_CLOCK[0].timestamp() * 1000)  # noqa: E731
    perp = SimPerpBroker(acct, md, cfg.costs, clock_ms)
    spot = SimSpotBroker(acct, md, cfg.costs, clock_ms)

    strategy = FundingArbBybitStrategy(symbols=tuple(syms), **params)
    strategy.clock = _SimClock()
    target_q: asyncio.Queue = asyncio.Queue()
    fills_q: asyncio.Queue = asyncio.Queue()
    guard = StrategyGuard(max_loss=cfg.strategy_max_loss if cfg.strategy_max_loss is not None
                          else Decimal("1e18"))
    runner = StrategyRunner(STRATEGY_ID, strategy, bus=None, topics=list(strategy.topics),  # type: ignore[arg-type]
                            target_queue=target_q, guard=guard, pnlcalc=PnLCalc())
    registry = None
    if cfg.registry_max_loss is not None:
        registry = StrategyGuardRegistry(default_max_loss=cfg.registry_max_loss)
        registry.configure(STRATEGY_ID, cfg.registry_max_loss)
    brokers = {PERP_EX: perp, SPOT_EX: spot}
    await cw.INSTRUMENTS.load(brokers)   # as the live consolidator does at start-up
    cons = OrderConsolidator(target_queue=target_q, brokers=brokers,
                             default_exchange=PERP_EX, min_order_size=cfg.min_order_size,
                             fills_queue=fills_q, order_factory=cw._order_factory,
                             guard_registry=registry, order_check=cw.INSTRUMENTS.check)

    warn = _CountingHandler()
    slog = logging.getLogger(strat_mod.__name__)
    clog = logging.getLogger("engine.order.consolidator")
    rlog = logging.getLogger("engine.order.guard_registry")
    for lg in (slog, clog, rlog):
        lg.addHandler(warn)
        lg.setLevel(logging.WARNING)
        lg.propagate = not quiet

    phantom: Counter[str] = Counter()

    async def emit(sig) -> None:  # noqa: ANN001
        await runner._emit(sig)
        while not target_q.empty() or not fills_q.empty():
            while not target_q.empty():
                t = target_q.get_nowait()
                n_orders = len(acct.orders) + len(acct.rejects)
                await cons._reconcile(t)
                if len(acct.orders) + len(acct.rejects) == n_orders and not fills_q.empty():
                    phantom[f"{t.exchange or PERP_EX}:{t.symbol}"] += 1
            while not fills_q.empty():
                f = fills_q.get_nowait()
                runner.pnl_calc.on_fill(f.symbol, f.quantity, f.fill_price, exchange=f.exchange)
                await runner.notify_fill(f)

    strategy.on_start()
    last_px: dict[str, dict[str, Decimal]] = {k: {} for k in ("perp", "spot", "mark")}
    equity_rows: list[tuple[int, float, float, float]] = []
    trades: list[dict] = []
    open_ep: dict[str, dict] = {}
    incidents: list[dict] = []
    naked_open: dict[str, dict] = {}
    naked_hours = 0
    last_eq: Decimal | None = None
    guard_trip_ts = registry_trip_ts = None
    guard_halted_hours = 0
    guard_trips = 0
    guard_was_active = True
    stopped = False   # consolidator guard tripped -> manager stops the container for good
    hours_since_tick = 0
    tick_every = max(1, 8 // max(1, cfg.ticks_per_window))

    for h in md.hours:
        _CLOCK[0] = datetime.fromtimestamp(h / 1000 + 1, UTC)
        for s in syms:
            for kind, src, dst in (("perp", md.perp, acct.perp_px), ("spot", md.spot, acct.spot_px),
                                   ("mark", md.mark, acct.mark_px)):
                v = src[s].get(h)
                if v is not None:
                    last_px[kind][s] = v
                    dst[s] = v
        perp.settle_funding(h)                       # settlement happens AT the boundary

        boundary = h % H8 == 0
        trading = h >= cfg.start_ms
        hours_since_tick += 1
        if boundary:
            pre = {s: acct.sym_equity(s) for s in syms}
        if boundary and not stopped:
            for s in syms:
                bar = md.bars8h[s].get(h)
                if bar is not None:
                    await emit(runner._dispatch(bar))
            for s in syms:
                r = md.funding[s].get(h)
                if r is None or acct.mark_px[s] == 0:
                    continue
                ev = FundingRate(symbol=s, funding_rate=r, mark_price=acct.mark_px[s],
                                 timestamp=_CLOCK[0], next_funding_time=_next_settlement(md, s, h))
                sig = runner._dispatch(ev)
                if not trading and sig is not None and sig.exchange == PERP_EX and sig.quantity < 0:
                    # warm-up: let filters fill, but don't open positions before --start
                    strategy._fill_state[s] = "flat"
                    strategy._perp_qty[s] = 0.0
                    strategy._pending_signal_at[s] = None
                    sig = None
                await emit(sig)
            hours_since_tick = 0
        if boundary:
            # trade episodes (perp position open -> closed)
            for s in syms:
                is_open = acct.perp_qty[s] != 0
                if is_open and s not in open_ep:
                    open_ep[s] = {"symbol": s, "entry_ts": h, "eq0": pre[s],
                                  "f0": acct.funding[s], "fee0": acct.fees[s],
                                  "notional": float(abs(acct.perp_qty[s]) * acct.perp_px[s]),
                                  "entry_px": acct.perp_px[s], "mae": 0.0}
                elif not is_open and s in open_ep:
                    ep = open_ep.pop(s)
                    pnl = acct.sym_equity(s) - ep["eq0"]
                    fund = acct.funding[s] - ep["f0"]
                    fees = acct.fees[s] - ep["fee0"]
                    trades.append({"symbol": s, "entry": iso(ep["entry_ts"]), "exit": iso(h),
                                   "hold_periods": (h - ep["entry_ts"]) // H8,
                                   "notional": round(ep["notional"], 2),
                                   "funding": float(fund), "fees": float(fees),
                                   "price_basis": float(pnl - fund + fees), "pnl": float(pnl),
                                   "pnl_bps": float(pnl) / ep["notional"] * 1e4 if ep["notional"] else 0,
                                   "max_adverse_%": round(ep["mae"] * 100, 2)})
            eq = acct.equity()
            dep = sum(acct.coin[s] * acct.spot_px[s] for s in syms)
            equity_rows.append((h, float(eq), float(dep), float(acct.usdt)))
        elif not boundary and not stopped and hours_since_tick % tick_every == 0:
            for s in syms:   # mid-window ticks: gate ignores them, recovery paths may act
                if acct.mark_px[s] == 0:
                    continue
                ev = FundingRate(symbol=s, funding_rate=_last_rate(md, s, h), mark_price=acct.mark_px[s],
                                 timestamp=_CLOCK[0], next_funding_time=_next_settlement(md, s, h))
                await emit(runner._dispatch(ev))

        for s, ep in open_ep.items():
            ep["mae"] = max(ep["mae"], float(acct.perp_px[s] / ep["entry_px"] - 1))

        # unhedged exposure
        for s in syms:
            net = acct.perp_qty[s] + acct.coin[s]
            notional = abs(net) * (acct.spot_px[s] or acct.perp_px[s])
            if notional > cfg.naked_usd:
                naked_hours += 1
                inc = naked_open.get(s)
                if inc is None:
                    naked_open[s] = {"symbol": s, "start": iso(h), "end": iso(h), "hours": 1,
                                     "max_notional": float(notional),
                                     "perp_qty": str(acct.perp_qty[s]), "coin": str(acct.coin[s])}
                else:
                    inc["end"], inc["hours"] = iso(h), inc["hours"] + 1
                    inc["max_notional"] = max(inc["max_notional"], float(notional))
            elif s in naked_open:
                incidents.append(naked_open.pop(s))

        # in-process guard is fed account PnL deltas (workers/strategy_worker.py bridge)
        eq = acct.equity()
        if last_eq is not None and cfg.strategy_max_loss is not None:
            delta = eq - last_eq
            if abs(delta) <= guard.max_loss * 2:
                guard.record_pnl(delta)
        last_eq = eq
        if not guard.is_active:
            guard_halted_hours += 1
            if guard_was_active:
                guard_trips += 1
                guard_trip_ts = guard_trip_ts or h
        guard_was_active = guard.is_active
        if registry is not None and not stopped and not registry.is_active(STRATEGY_ID):
            registry_trip_ts = h
            stopped = True

    incidents.extend(naked_open.values())
    strategy.on_stop()
    for lg in (slog, clog, rlog):
        lg.removeHandler(warn)

    still_open = [{"symbol": s, "perp_qty": str(acct.perp_qty[s]), "coin": str(acct.coin[s]),
                   "strategy_state": strategy._fill_state[s]} for s in syms
                  if acct.perp_qty[s] != 0 or acct.coin[s] * acct.spot_px[s] > cfg.naked_usd]
    return {
        "params": params,
        "equity": equity_rows,
        "trades": trades,
        "incidents": incidents,
        "naked_hours": naked_hours,
        "rejects": acct.rejects,
        "n_orders": len(acct.orders),
        "phantom_fills": dict(phantom),
        "warnings": dict(warn.counts),
        "warning_samples": warn.samples,
        "guard_trip": iso(guard_trip_ts) if guard_trip_ts else None,
        "guard_trips": guard_trips,
        "guard_halted_hours": guard_halted_hours,
        "registry_trip": iso(registry_trip_ts) if registry_trip_ts else None,
        "funding_by_symbol": {s: float(acct.funding[s]) for s in syms},
        "fees_by_symbol": {s: float(acct.fees[s]) for s in syms},
        "pnl_by_symbol": {s: float(acct.sym_equity(s)) for s in syms},
        "open_at_end": still_open,
        "strategy_shadow_equity": strategy._equity,
    }

def _next_settlement(md: MarketData, s: str, h: int) -> datetime | None:
    """First settlement strictly after h, i.e. what the live ticker shows as nextFundingTime."""
    import bisect
    ts = _LAST_RATE_CACHE.get(s)
    if ts is None:
        ts = _LAST_RATE_CACHE[s] = sorted(md.funding[s])
    i = bisect.bisect_right(ts, h)
    return datetime.fromtimestamp(ts[i] / 1000, UTC) if i < len(ts) else None

_LAST_RATE_CACHE: dict[str, list[int]] = {}


def _last_rate(md: MarketData, s: str, h: int) -> Decimal:
    import bisect
    ts = _LAST_RATE_CACHE.get(s)
    if ts is None:
        ts = _LAST_RATE_CACHE[s] = sorted(md.funding[s])
    i = bisect.bisect_right(ts, h) - 1
    return md.funding[s][ts[i]] if i >= 0 else D0


# =============================================================================
# Metrics
# =============================================================================

def metrics(res: dict, cap: float, start_ms: int = 0, end_ms: int | None = None) -> dict:
    rows = [r for r in res["equity"] if r[0] >= start_ms and (end_ms is None or r[0] < end_ms)]
    if len(rows) < 6:
        return {}
    eq = [r[1] for r in rows]
    daily = [eq[i + 3] - eq[i] for i in range(0, len(eq) - 3, 3)]
    years = (rows[-1][0] - rows[0][0]) / (365 * 24 * H1)
    pnl = eq[-1] - eq[0]
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    dep = [r[2] for r in rows]
    avg_dep = sum(dep) / len(dep)
    sd = statistics.pstdev(daily) if len(daily) > 1 else 0
    tr = [t for t in res["trades"] if iso(start_ms) <= t["exit"] and (end_ms is None or t["exit"] < iso(end_ms))]
    return {
        "net_pnl": pnl,
        "ann_ret_on_cap_%": pnl / cap / years * 100 if years > 0 else float("nan"),
        "ann_ret_on_deployed_%": pnl / avg_dep / years * 100 if avg_dep > 0 and years > 0 else float("nan"),
        "sharpe": statistics.mean(daily) / sd * math.sqrt(365) if sd > 0 else float("nan"),
        "max_dd": mdd,
        "trades": len(tr),
        "win_rate_%": sum(t["pnl"] > 0 for t in tr) / len(tr) * 100 if tr else float("nan"),
        "avg_trade_bps": statistics.mean(t["pnl_bps"] for t in tr) if tr else float("nan"),
        "avg_hold_periods": statistics.mean(t["hold_periods"] for t in tr) if tr else float("nan"),
        "funding": sum(t["funding"] for t in tr),
        "fees": sum(t["fees"] for t in tr),
        "price_basis": sum(t["price_basis"] for t in tr),
        "avg_deployed": avg_dep,
        "max_deployed": max(dep),
        "naked_hours": res["naked_hours"],
        "rejected_orders": len(res["rejects"]),
    }


def daily_pnl(res: dict) -> list[tuple[int, float]]:
    rows = res["equity"]
    return [(rows[i][0], rows[i + 3][1] - rows[i][1]) for i in range(0, len(rows) - 3, 3)]


# =============================================================================
# Grid (multiprocessing) + walk-forward
# =============================================================================

GRIDS = {
    "tiny": {   # smoke test
        "entry_threshold_annualized": [0.15, 0.25],
        "exit_threshold_annualized": [0.0, 0.02],
    },
    "small": {
        "entry_threshold_annualized": [0.11, 0.15, 0.20, 0.30],
        "exit_threshold_annualized": [-1.0, 0.0, 0.02, 0.05],
        "min_consecutive_positive": [1, 3, 6],
        "max_volatility": [0.8, 99.0],
        "min_hold_periods": [1, 9],
    },
    "medium": {
        "entry_threshold_annualized": [0.11, 0.13, 0.15, 0.20, 0.25, 0.30, 0.40],
        "exit_threshold_annualized": [-1.0, 0.0, 0.02, 0.05, 0.08],
        "min_consecutive_positive": [1, 3, 6, 9],
        "max_volatility": [0.6, 0.8, 1.2, 99.0],
        "min_hold_periods": [1, 3, 9, 21],
        "max_hold_periods": [100, 100_000],
    },
}

_W: dict[str, Any] = {}


def _worker_init(spec: dict) -> None:
    logging.disable(logging.CRITICAL)
    if spec["synthetic"]:
        _W["md"] = synthetic_market(spec["symbols"])
    else:
        _W["md"] = load_cached(spec["symbols"], spec["start_fetch"], spec["end"], Path(spec["cache"]))
    _W["cfg"] = spec["cfg"]


def _worker_run(params: dict) -> dict:
    res = asyncio.run(run_backtest(_W["md"], params, _W["cfg"]))
    return {"params": params, "daily": daily_pnl(res), "equity": res["equity"],
            "trades": res["trades"], "naked_hours": res["naked_hours"], "rejects": res["rejects"]}


def walk_forward(grid_res: list[dict], cap: float, train_days: int, test_days: int,
                 objective: str, min_trades: int) -> tuple[list[tuple[int, float]], list[dict]]:
    days = [t for t, _ in grid_res[0]["daily"]]
    D = len(days)
    series = [[v for _, v in g["daily"]] for g in grid_res]
    entries = []
    for g in grid_res:
        per_day = Counter()
        for t in g["trades"]:
            d = datetime.strptime(t["entry"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
            per_day[int(d.timestamp() * 1000) // (24 * H1)] += 1
        entries.append([per_day.get(ts // (24 * H1), 0) for ts in days])

    def score(k: int, a: int, b: int) -> float:
        xs = series[k][a:b]
        if sum(entries[k][a:b]) < min_trades:
            return -math.inf
        if objective == "sharpe":
            sd = statistics.pstdev(xs)
            return statistics.mean(xs) / sd if sd > 0 else -math.inf
        return sum(xs)

    oos: list[tuple[int, float]] = []
    picks = []
    start = train_days
    while start < D:
        end = min(start + test_days, D)
        scores = [score(k, start - train_days, start) for k in range(len(grid_res))]
        k = max(range(len(scores)), key=scores.__getitem__)
        if scores[k] == -math.inf:
            start = end
            continue
        oos.extend((days[i], series[k][i]) for i in range(start, end))
        picks.append({"test_start": iso(days[start]), "test_end": iso(days[end - 1]),
                      "oos_pnl": round(sum(series[k][start:end]), 2), **grid_res[k]["params"]})
        start = end
    return oos, picks


# =============================================================================
# Report
# =============================================================================

def md_table(rows: list[dict], cols: list[str] | None = None) -> str:
    if not rows:
        return "_none_\n"
    cols = cols or list(rows[0])
    def f(v):  # noqa: ANN001
        if isinstance(v, float):
            return "" if math.isnan(v) else f"{v:,.2f}"
        return str(v)
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(f(r.get(c, "")) for c in cols) + " |" for r in rows]
    return "\n".join(out) + "\n"


def write_csv(path: Path, rows: list[dict]) -> None:
    import csv
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def try_plot(path: Path, curves: dict[str, list[tuple[int, float]]], title: str) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    fig, ax = plt.subplots(figsize=(10, 4.5))
    for name, pts in curves.items():
        if pts:
            ax.plot([datetime.fromtimestamp(t / 1000, UTC) for t, _ in pts], [v for _, v in pts], label=name)
    ax.set_title(title)
    ax.set_ylabel("cumulative P&L, USDT")
    ax.grid(alpha=.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return True


def scenario_section(name: str, res: dict, cap: float, start_ms: int) -> list[str]:
    m = metrics(res, cap, start_ms)
    L = [f"### {name}\n"]
    L.append(md_table([{k: (round(v, 2) if isinstance(v, float) else v) for k, v in m.items()}]))
    L.append(f"Orders filled: {res['n_orders']}, rejected: {len(res['rejects'])}, "
             f"phantom fills (consolidator confirmed a fill without sending an order): "
             f"{sum(res['phantom_fills'].values())}  ")
    g = (f"{res['guard_trips']}× (first {res['guard_trip']}), halted {res['guard_halted_hours']} h in total"
         if res["guard_trips"] else "no")
    L.append(f"In-process guard (re-activates when PnL recovers): **{g}** · consolidator guard "
             f"(container stopped for good): **{res['registry_trip'] or 'no'}** · symbol-hours with "
             f"unhedged exposure > $100: **{res['naked_hours']}**\n")
    if res["rejects"]:
        rc = Counter((r["exchange"], r["symbol"], r["reason"]) for r in res["rejects"])
        L.append("Rejected orders (exchange, symbol, reason → count):\n")
        L.append(md_table([{"exchange": e, "symbol": s, "reason": why, "count": n}
                           for (e, s, why), n in rc.most_common(15)]))
    if res["phantom_fills"]:
        L.append("Phantom fills by leg: " + ", ".join(f"{k}: {v}" for k, v in res["phantom_fills"].items()) + "\n")
    if res["warnings"]:
        L.append("Strategy / consolidator warnings:\n")
        L.append(md_table([{"warning": k, "count": v, "first": res["warning_samples"].get(k, "")}
                           for k, v in Counter(res["warnings"]).most_common(12)]))
    if res["incidents"]:
        L.append("Largest unhedged-exposure incidents:\n")
        L.append(md_table(sorted(res["incidents"], key=lambda r: -r["max_notional"])[:10]))
    if res["open_at_end"]:
        L.append("Positions still open at the end:\n")
        L.append(md_table(res["open_at_end"]))
    per = [{"symbol": s, "pnl": res["pnl_by_symbol"][s], "funding": res["funding_by_symbol"][s],
            "fees": res["fees_by_symbol"][s],
            "trades": sum(t["symbol"] == s for t in res["trades"])} for s in res["pnl_by_symbol"]]
    L.append("Per symbol:\n")
    L.append(md_table(per))
    return L


# =============================================================================
# main
# =============================================================================

def parse_set(items: list[str]) -> dict:
    out = {}
    for it in items or []:
        k, v = it.split("=", 1)
        out[k] = int(v) if v.lstrip("-").isdigit() else float(v)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2022-01-01", help="first trading day (UTC)")
    ap.add_argument("--end", default=None)
    ap.add_argument("--symbols", default=",".join(strat_mod._SYMBOLS))
    ap.add_argument("--cache", default=".backtest_cache")
    ap.add_argument("--out", default="backtest_report")
    ap.add_argument("--synthetic", action="store_true", help="offline self-test data")
    ap.add_argument("--set", action="append", default=[], metavar="KWARG=VALUE",
                    help="strategy constructor kwarg override, repeatable")
    ap.add_argument("--wallet", type=float, default=60_000)
    ap.add_argument("--perp-fee", type=float, default=0.00055)
    ap.add_argument("--spot-fee", type=float, default=0.001)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--strategy-max-loss", type=float, default=float(FundingArbBybitStrategy.max_loss),
                    help="in-process guard (MAX_DRAWDOWN env); default = strategy.max_loss")
    ap.add_argument("--registry-max-loss", type=float,
                    default=float(os.getenv("DEFAULT_MAX_LOSS", "1000")),
                    help="consolidator guard (DEFAULT_MAX_LOSS / Deploy page)")
    ap.add_argument("--ticks-per-window", type=int, default=8)
    ap.add_argument("--grid", choices=list(GRIDS), default=None)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--objective", choices=["pnl", "sharpe"], default="pnl")
    ap.add_argument("--train-days", type=int, default=365)
    ap.add_argument("--test-days", type=int, default=91)
    ap.add_argument("--min-trades", type=int, default=8)
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    log.setLevel(logging.INFO)

    symbols = a.symbols.split(",")
    start_dt = datetime.fromisoformat(a.start).replace(tzinfo=UTC)
    start_fetch = a.start
    if a.synthetic:
        md = synthetic_market(symbols)
        start_ms = md.hours[0] + 30 * H8
    else:
        md = asyncio.run(download(symbols, start_fetch, a.end, Path(a.cache)))
        start_ms = int(start_dt.timestamp() * 1000)

    cap = min(a.wallet, 60_000.0)
    costs = Costs(Decimal(str(a.perp_fee)), Decimal(str(a.spot_fee)), Decimal(str(a.slippage_bps / 1e4)))
    base_cfg = Config(wallet=Decimal(str(a.wallet)), strategy_max_loss=Decimal(str(a.strategy_max_loss)),
                      registry_max_loss=Decimal(str(a.registry_max_loss)),
                      ticks_per_window=a.ticks_per_window, start_ms=start_ms, costs=costs)
    noguard_cfg = Config(**{**base_cfg.__dict__, "strategy_max_loss": None, "registry_max_loss": None})
    params = parse_set(a.set)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    print("[run] as deployed (guards on) ...")
    r_dep = asyncio.run(run_backtest(md, params, base_cfg))
    print(f"[run] done in {time.time() - t0:.0f}s; guards off ...")
    r_ng = asyncio.run(run_backtest(md, params, noguard_cfg))

    L = [f"# FundingArbBybitStrategy — whole-system backtest"
         f"{'  (SYNTHETIC SELF-TEST — numbers are meaningless)' if md.synthetic else ''}\n",
         f"Trading window: {iso(start_ms)} → {iso(md.hours[-1])} UTC · symbols: {', '.join(symbols)}  ",
         f"Strategy params: defaults{'' if not params else ' + ' + json.dumps(params)} · wallet "
         f"${a.wallet:,.0f} · fees perp {a.perp_fee * 1e4:.1f} bps / spot {a.spot_fee * 1e4:.1f} bps · "
         f"slippage {a.slippage_bps} bps/order · guards: in-process ${a.strategy_max_loss:,.0f}, "
         f"consolidator ${a.registry_max_loss:,.0f}\n"]
    if md.notes:
        L.append("**Data notes**\n")
        L += [f"- {n}" for n in md.notes]
        L.append("")
    L.append("## Results\n")
    L += scenario_section("As deployed (both guards active)", r_dep, cap, start_ms)
    L += scenario_section("Same, guards disabled (pure strategy economics)", r_ng, cap, start_ms)

    write_csv(out / "trades.csv", r_ng["trades"])
    write_csv(out / "trades_as_deployed.csv", r_dep["trades"])
    write_csv(out / "incidents.csv", r_dep["incidents"])
    write_csv(out / "rejects.csv", r_dep["rejects"])
    write_csv(out / "equity.csv", [{"time": iso(t), "equity": e, "deployed_spot": d, "usdt": u}
                                   for t, e, d, u in r_ng["equity"]])

    def cum(res):  # noqa: ANN001
        rows = [r for r in res["equity"] if r[0] >= start_ms]
        return [(t, e - rows[0][1]) for t, e, _, _ in rows] if rows else []
    curves = {"as deployed": cum(r_dep), "guards off": cum(r_ng)}

    if a.grid:
        combos = [dict(zip(GRIDS[a.grid], v)) for v in itertools.product(*GRIDS[a.grid].values())]
        combos = [{**params, **c} for c in combos
                  if c.get("exit_threshold_annualized", -9) < c.get("entry_threshold_annualized", 9)]
        print(f"[grid] {len(combos)} combinations on {a.workers} workers (guards off) ...")
        spec = {"synthetic": a.synthetic, "symbols": symbols, "start_fetch": start_fetch,
                "end": a.end, "cache": a.cache, "cfg": noguard_cfg}
        t1 = time.time()
        with ProcessPoolExecutor(max_workers=a.workers, initializer=_worker_init, initargs=(spec,)) as ex:
            grid_res = []
            for i, g in enumerate(ex.map(_worker_run, combos)):
                grid_res.append(g)
                if i % 10 == 0:
                    print(f"  {i}/{len(combos)}  {time.time() - t1:.0f}s")
        rows = []
        for g in grid_res:
            m = metrics({"equity": g["equity"], "trades": g["trades"], "naked_hours": g["naked_hours"],
                         "rejects": g["rejects"]}, cap, start_ms)
            rows.append({**g["params"], **{k: m.get(k) for k in
                         ("net_pnl", "ann_ret_on_cap_%", "ann_ret_on_deployed_%", "sharpe", "max_dd",
                          "trades", "avg_trade_bps", "naked_hours", "rejected_orders")}})
        rows.sort(key=lambda r: -(r["net_pnl"] or -1e18))
        write_csv(out / "grid.csv", rows)
        L.append(f"## Parameter grid ({len(rows)} combinations, guards off, full period — in-sample)\n")
        keys = list(GRIDS[a.grid])
        L.append(md_table(rows[:15], keys + ["net_pnl", "ann_ret_on_cap_%", "sharpe", "max_dd",
                                             "trades", "avg_trade_bps", "naked_hours"]))
        pos = sum((r["net_pnl"] or 0) > 0 for r in rows)
        L.append(f"\n{pos} of {len(rows)} combinations are profitable after costs.\n")
        grid_res_t = [{"params": g["params"], "daily": [(t, v) for t, v in g["daily"] if t >= start_ms],
                       "trades": g["trades"]} for g in grid_res]
        oos, picks = walk_forward(grid_res_t, cap, a.train_days, a.test_days, a.objective, a.min_trades)
        write_csv(out / "walk_forward.csv", picks)
        L.append(f"## Walk-forward (optimise on trailing {a.train_days} d by `{a.objective}`, "
                 f"trade next {a.test_days} d blind)\n")
        if oos:
            best = max(grid_res_t, key=lambda g: sum(v for _, v in g["daily"]))
            oos_t0 = oos[0][0]
            best_oos = [(t, v) for t, v in best["daily"] if t >= oos_t0]
            live_oos = [(t, v) for t, v in daily_pnl(r_ng) if t >= oos_t0]
            def summ(pts):  # noqa: ANN001
                xs = [v for _, v in pts]
                yrs = len(xs) / 365
                sd = statistics.pstdev(xs) if len(xs) > 1 else 0
                return {"net_pnl": sum(xs), "ann_ret_on_cap_%": sum(xs) / cap / yrs * 100 if yrs else float("nan"),
                        "sharpe": statistics.mean(xs) / sd * math.sqrt(365) if sd else float("nan")}
            L.append(md_table([{"series": "walk-forward OOS", **summ(oos)},
                               {"series": "best full-sample combo (in-sample)", **summ(best_oos)},
                               {"series": "current params", **summ(live_oos)}]))
            L.append("\n" + md_table(picks))
            c = 0.0
            curves["walk-forward OOS"] = [(t, (c := c + v)) for t, v in oos]
        else:
            L.append("Not enough history for a walk-forward split.\n")

    if try_plot(out / "equity.png", curves, "FundingArbBybitStrategy backtest"):
        L.append("![equity](equity.png)\n")
    L.append("## How to read this\n")
    L.append("- *As deployed* runs the guards exactly as production does. If a guard trips, the "
             "simulation keeps running with whatever positions were left open, the way production "
             "would until someone intervenes.\n"
             "- *Phantom fills*: the consolidator confirmed a fill without sending an order (the old "
             "`min_order_size=1` did this). Orders the size rules refuse are now reported as "
             "DROPPED with quantity 0, so this should stay 0.\n"
             "- *Unhedged exposure* is any hour where perp quantity and spot balance don't offset "
             "(net notional > $100), e.g. after a rejected spot order.\n"
             "- Funding events use the rate that actually settled at each boundary. Historical "
             "predicted rates are not available from Bybit, so the live ticker behaviour cannot "
             "be replayed.\n"
             "- Fills happen at the 1h close at the boundary ± slippage, with no latency between legs.\n")
    (out / "report.md").write_text("\n".join(L), encoding="utf-8")
    print(f"\nDone in {time.time() - t0:.0f}s -> {out}/report.md")


if __name__ == "__main__":
    main()