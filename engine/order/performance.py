"""Per-strategy PnL valuation, PnL history and Sharpe ratio.

- :func:`value_holdings` / :func:`strategy_pnl` — a strategy's PnL right
  now: realized and fees from the ledger, unrealized from live prices,
  funding from its share of the account's payments (``funding.py``).
- :class:`PnLHistoryDB` — those values recorded over time
  (``~/.qrf/pnl_history.db``), one row per strategy per recording, written
  by ``workers/snapshot_all.py``.  Unrealized PnL is the part that can't be
  rebuilt later (it needs the price at the time), which is why it's stored.
- :func:`sharpe` — Sharpe ratio from that history, for any period.

The rows carry their exact time and :func:`sharpe` buckets them by period
itself, so recording more often than once a day later changes nothing.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from engine.order.funding import FundingDB
    from engine.order.ledger import Holding, LedgerDB

log = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path.home() / ".qrf" / "pnl_history.db"
_YEAR = timedelta(days=365)  # crypto trades every day


class BybitPrices:
    """Current prices from Bybit's public tickers endpoint (no API key).

    Perps (``bybit_demo``) use the mark price, spot (``bybit_spot``) the
    last trade price — the exchange names from
    ``workers.consolidator_worker.build_brokers``.  Any failure gives
    ``None``, so a value that depends on it is shown as unknown, never 0.
    """

    _CATEGORY = {"bybit_demo": "linear", "bybit_spot": "spot"}

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url="https://api.bybit.com", timeout=10.0
        )

    async def __call__(self, exchange: str, symbol: str) -> Decimal | None:
        category = self._CATEGORY.get(exchange)
        if category is None:
            return None
        try:
            resp = await self._client.get(
                "/v5/market/tickers", params={"category": category, "symbol": symbol}
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("result", {}).get("list", [])
            if data.get("retCode") != 0 or not items:
                return None
            raw = items[0].get("markPrice") if category == "linear" else None
            raw = raw or items[0].get("lastPrice")
            return Decimal(str(raw)) if raw else None
        except Exception as exc:
            log.warning("price for %s %s unavailable: %s", exchange, symbol, exc)
            return None

    async def aclose(self) -> None:
        await self._client.aclose()


# ----------------------------------------------------------------- valuation


@dataclass(frozen=True, kw_only=True)
class HoldingValue:
    """A holding with its live price and unrealized PnL (``None`` = unknown)."""

    holding: Holding
    price: Decimal | None
    unrealized_pnl: Decimal | None


async def value_holdings(
    holdings: Iterable[Holding],
    prices: Callable[[str, str], Awaitable[Decimal | None]],
    cache: dict[tuple[str, str], Decimal | None] | None = None,
) -> list[HoldingValue]:
    """Price every open holding: unrealized = (price − avg entry) × quantity.

    A closed holding (quantity 0) needs no price and is worth 0.  *cache*
    lets callers fetch each (exchange, symbol) once across strategies.
    """
    cache = {} if cache is None else cache
    values = []
    for h in holdings:
        if h.quantity == 0:
            values.append(HoldingValue(holding=h, price=None, unrealized_pnl=Decimal(0)))
            continue
        key = (h.exchange, h.symbol)
        if key not in cache:
            cache[key] = await prices(*key)
        price = cache[key]
        unrealized = None if price is None else (price - h.avg_entry_price) * h.quantity
        values.append(HoldingValue(holding=h, price=price, unrealized_pnl=unrealized))
    return values


def unrealized_at(
    holdings: Iterable[Holding], prices: dict[tuple[str, str], Decimal | None]
) -> Decimal | None:
    """Total unrealized PnL at already-fetched *prices* — no fetching, so
    it can run synchronously.  ``None`` if an open holding has no price."""
    total = Decimal(0)
    for h in holdings:
        if h.quantity == 0:
            continue
        price = prices.get((h.exchange, h.symbol))
        if price is None:
            return None
        total += (price - h.avg_entry_price) * h.quantity
    return total


def total_unrealized(values: Iterable[HoldingValue]) -> Decimal | None:
    """Sum of unrealized PnL, or ``None`` if any holding's is unknown."""
    total = Decimal(0)
    for v in values:
        if v.unrealized_pnl is None:
            return None
        total += v.unrealized_pnl
    return total


@dataclass(frozen=True, kw_only=True)
class PnLPoint:
    """A strategy's PnL at one moment: net = realized + unrealized +
    funding − fees.  ``unrealized_pnl`` / ``net_pnl`` are ``None`` when a
    price was missing — the point is then incomplete.  ``funding`` is what
    has been collected so far (positive = received; 0 before the first
    funding run)."""

    strategy: str
    strategy_num: int
    taken_at: datetime
    orders: int
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    fees_paid: Decimal
    net_pnl: Decimal | None
    funding: Decimal = Decimal(0)

    @property
    def complete(self) -> bool:
        return self.net_pnl is not None


def net_pnl(
    realized: Decimal, unrealized: Decimal | None, funding: Decimal, fees: Decimal
) -> Decimal | None:
    """realized + unrealized + funding − fees, or ``None`` if unrealized is unknown."""
    return None if unrealized is None else realized + unrealized + funding - fees


async def strategy_pnl(
    ledger: LedgerDB,
    strategy: str,
    prices: Callable[[str, str], Awaitable[Decimal | None]],
    cache: dict[tuple[str, str], Decimal | None] | None = None,
    funding: FundingDB | None = None,
) -> PnLPoint:
    """*strategy*'s PnL now: net = realized + unrealized + funding − fees."""
    snap = ledger.snapshot(strategy)  # one consistent read
    unrealized = total_unrealized(await value_holdings(snap.holdings, prices, cache))
    received = funding.total(strategy) if funding is not None else Decimal(0)
    return PnLPoint(
        strategy=strategy,
        strategy_num=snap.strategy_num,
        taken_at=snap.taken_at,
        orders=snap.counter,
        realized_pnl=snap.realized_pnl,
        unrealized_pnl=unrealized,
        fees_paid=snap.fees_paid,
        funding=received,
        net_pnl=net_pnl(snap.realized_pnl, unrealized, received, snap.fees_paid),
    )


# ------------------------------------------------------------------- history


class PnLHistoryDB:
    """Append-only PnL history, one row per strategy per recording."""

    def __init__(self, db_path: str | Path | None = None, *, readonly: bool = False) -> None:
        path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        if readonly:
            self._conn = sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False
            )
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS pnl_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy        TEXT NOT NULL,
                strategy_num    INTEGER NOT NULL,
                taken_at        TEXT NOT NULL,
                orders          INTEGER NOT NULL,
                realized_pnl    TEXT NOT NULL,
                unrealized_pnl  TEXT,
                fees_paid       TEXT NOT NULL,
                funding         TEXT NOT NULL DEFAULT '0',
                net_pnl         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pnl_history_strategy
                ON pnl_history(strategy, id);
        """)
        self._conn.commit()

    def record(self, point: PnLPoint) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO pnl_history (strategy, strategy_num, taken_at, orders,"
                " realized_pnl, unrealized_pnl, fees_paid, funding, net_pnl)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    point.strategy,
                    point.strategy_num,
                    point.taken_at.isoformat(),
                    point.orders,
                    str(point.realized_pnl),
                    None if point.unrealized_pnl is None else str(point.unrealized_pnl),
                    str(point.fees_paid),
                    str(point.funding),
                    None if point.net_pnl is None else str(point.net_pnl),
                ),
            )

    def history(self, strategy: str) -> list[PnLPoint]:
        """Every recorded point of *strategy*, oldest first."""
        rows = self._conn.execute(
            "SELECT strategy_num, taken_at, orders, realized_pnl, unrealized_pnl,"
            " fees_paid, funding, net_pnl FROM pnl_history WHERE strategy = ?"
            " ORDER BY id",
            (strategy,),
        ).fetchall()
        return [
            PnLPoint(
                strategy=strategy,
                strategy_num=r[0],
                taken_at=datetime.fromisoformat(r[1]),
                orders=r[2],
                realized_pnl=Decimal(r[3]),
                unrealized_pnl=None if r[4] is None else Decimal(r[4]),
                fees_paid=Decimal(r[5]),
                funding=Decimal(r[6]),
                net_pnl=None if r[7] is None else Decimal(r[7]),
            )
            for r in rows
        ]

    def last_id(self, strategy: str) -> int | None:
        """Id of *strategy*'s newest row — cheap check for "anything new?"."""
        (last,) = self._conn.execute(
            "SELECT MAX(id) FROM pnl_history WHERE strategy = ?", (strategy,)
        ).fetchone()
        return None if last is None else int(last)

    def close(self) -> None:
        self._conn.close()


# -------------------------------------------------------------------- sharpe


@dataclass(frozen=True, kw_only=True)
class Sharpe:
    """``value`` is ``None`` until there are at least 2 returns (and some
    variation).  ``returns`` is how many period-to-period changes it used."""

    value: float | None
    returns: int
    period: timedelta


def sharpe(points: Iterable[PnLPoint], period: timedelta = timedelta(days=1)) -> Sharpe:
    """Annualized Sharpe ratio of a strategy's PnL history.

    1. Bucket the points by *period* (UTC, from the epoch) and keep the last
       point in each bucket.
    2. Return = change in net PnL between **consecutive** buckets, in
       dollars — a gap (no recording, or an incomplete point) is skipped,
       never counted as one period.
    3. Sharpe = mean / population std of the returns × √(365 days / period).
       No risk-free rate — same definition as the dashboard's account-wide
       Sharpe (``webapp/api/performance.py``), which uses 8 h buckets.
    """
    step = period.total_seconds()
    buckets: dict[int, PnLPoint] = {}
    for p in points:
        buckets[int(p.taken_at.timestamp() // step)] = p  # last one wins

    returns: list[Decimal] = []
    for key in sorted(buckets):
        prev, cur = buckets.get(key - 1), buckets[key]
        if prev is not None and prev.net_pnl is not None and cur.net_pnl is not None:
            returns.append(cur.net_pnl - prev.net_pnl)

    value = None
    if len(returns) >= 2:
        n = Decimal(len(returns))
        mean = sum(returns, Decimal(0)) / n
        std = (sum(((r - mean) ** 2 for r in returns), Decimal(0)) / n).sqrt()
        if std > 0:
            annualize = (Decimal(_YEAR.total_seconds()) / Decimal(step)).sqrt()
            value = float(round(mean / std * annualize, 4))
    return Sharpe(value=value, returns=len(returns), period=period)
