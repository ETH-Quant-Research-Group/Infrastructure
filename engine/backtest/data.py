"""Historical data for a backtest: bars, funding rates, order-size rules.

Read from the history database (``history.db`` in ``cache_dir``, default
``~/.qrf/backtest_cache``, see :mod:`engine.backtest.history_db`); what's
missing is fetched from Bybit's public API (no key) into it, so a rerun is
fast and works offline.

- **Bars** for each symbol at the strategy's interval, from ``warmup_bars``
  before the start (for ``on_history_bar``) to the end.  Bybit bars are
  used as downloaded at that interval, or built from shorter stored ones
  (the 1h starter set gives 2h … 1d).
- **Funding rates** at every settlement in the period.  Bybit's history has
  no mark price, so each gets the close of the last bar before it.  Fetched
  in 60-day windows: the connector's own paging stops at 200 records.
- **Order-size rules** for perps and spot, mapped to the backtest's
  exchanges (``bybit_demo`` / ``bybit_spot``); an uploaded dataset's own
  rules win for its symbols.

**Datasets:** ``dataset`` names a user upload to use instead of Bybit.  A
symbol the upload has bars for comes from it; any other symbol from Bybit.
Funding likewise: the upload's if it brought some for that symbol, else
Bybit's.  An upload is never downloaded into: a month it doesn't cover is
missing.

What's downloaded is tracked per calendar month.  A month fetched before it
ended is only good up to then, and is fetched again when a later period
needs it.  ``offline=True`` never downloads (or writes): a missing month
raises :class:`MissingHistory` — what the Deploy page's backtests use, as
they run without network.
"""

from __future__ import annotations

import bisect
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from data.connectors.types import KlineInterval
from engine.backtest.history_db import (
    BYBIT,
    HistoryDB,
    aggregate,
    sources_for,
)
from engine.backtest.history_db import (
    INTERVAL_SECONDS as INTERVAL_SECONDS,  # re-exported: imported from here elsewhere
)
from engine.data import codec
from engine.order.instruments import rules_from_dict, rules_to_dict

if TYPE_CHECKING:
    from data.types import FundingRate, TimeBar
    from execution.types import InstrumentRules

log = logging.getLogger(__name__)

DEFAULT_CACHE = Path.home() / ".qrf" / "backtest_cache"
PERP_EXCHANGE = "bybit_demo"
SPOT_EXCHANGE = "bybit_spot"
_FUNDING_WINDOW = timedelta(days=60)
_LISTINGS_MAX_AGE = timedelta(days=1)
_MS = timedelta(milliseconds=1)


@dataclass
class HistoryData:
    """Everything a backtest replays.  ``bars`` and ``funding`` are per
    symbol, oldest first; ``bars`` include the warm-up before ``start``."""

    interval: str
    start: datetime
    end: datetime
    bars: dict[str, list[TimeBar]] = field(default_factory=dict)
    funding: dict[str, list[FundingRate]] = field(default_factory=dict)
    rules: dict[tuple[str, str], InstrumentRules] = field(default_factory=dict)
    sources: dict[str, dict[str, str]] = field(default_factory=dict)  # symbol → bars / funding


class MissingHistory(Exception):  # noqa: N818 - reads better at the call site
    """``offline`` load, but some months aren't in the database."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__("not downloaded yet: " + ", ".join(missing))


def with_marks(rates: list[FundingRate], bars: list[TimeBar]) -> list[FundingRate]:
    """Fill each rate's mark price with the last bar close before it, and its
    ``next_funding_time`` with the following settlement — what the live
    ticker shows, from which strategies learn the symbol's funding interval."""
    closes = [b.close_time for b in bars]
    out = []
    for n, rate in enumerate(rates):
        i = bisect.bisect_right(closes, rate.timestamp) - 1
        if i >= 0:
            following = rates[n + 1].timestamp if n + 1 < len(rates) else None
            out.append(replace(rate, mark_price=bars[i].close, next_funding_time=following))
    return out


def bars_from(start: datetime, interval: str, warmup_bars: int) -> datetime:
    """Where the bars for a period starting at *start* begin (warm-up included)."""
    return start - timedelta(seconds=INTERVAL_SECONDS[interval] * warmup_bars)


def _month(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, 1, tzinfo=UTC)


def _next_month(dt: datetime) -> datetime:
    return datetime(dt.year + dt.month // 12, dt.month % 12 + 1, 1, tzinfo=UTC)


def months(start: datetime, end: datetime) -> list[datetime]:
    """First instant of every calendar month that [start, end] touches."""
    out, m = [], _month(start)
    while m <= end:
        out.append(m)
        m = _next_month(m)
    return out


def _runs(items: list[datetime]) -> list[list[datetime]]:
    """Consecutive months grouped: [Jan, Feb, Apr] → [[Jan, Feb], [Apr]]."""
    out: list[list[datetime]] = []
    for m in items:
        if out and _next_month(out[-1][-1]) == m:
            out[-1].append(m)
        else:
            out.append([m])
    return out


# ------------------------------------------------------------------ the plan


@dataclass
class _Month:
    month: datetime
    status: str  # cached | partial | missing
    interval: str | None = None  # the stored timeframe the bars come from
    count: int = 0


@dataclass
class _SymbolPlan:
    bars_source: str
    bars: list[_Month]
    funding_source: str
    funding: list[_Month]


def _bybit_month(db: HistoryDB | None, symbol: str, kinds: list[str], m: datetime,
                 end: datetime) -> _Month:
    """Status of one Bybit month: the first of *kinds* (stored timeframes,
    best first; ``"funding"`` for funding) that was downloaded far enough."""
    if db is None:
        return _Month(m, "missing")
    seen = False
    for kind in kinds:
        got = db.fetched(symbol, kind if kind == "funding" else f"bars_{kind}", m)
        if got is None:
            continue
        seen = True
        until, complete = got
        if complete or until >= min(_next_month(m), end):
            month_end = _next_month(m)
            count = (db.count_funding(BYBIT, symbol, m, month_end) if kind == "funding"
                     else db.count_bars(BYBIT, symbol, kind, m, month_end))
            return _Month(m, "cached", None if kind == "funding" else kind, count)
    return _Month(m, "partial" if seen else "missing")


def _plan(db: HistoryDB | None, symbols: list[str], interval: str, start: datetime,
          end: datetime, warmup_bars: int, dataset: str) -> dict[str, _SymbolPlan]:
    first_bar = bars_from(start, interval, warmup_bars)
    bybit_kinds = sources_for(interval, INTERVAL_SECONDS)
    plans: dict[str, _SymbolPlan] = {}
    for symbol in symbols:
        stored = (sources_for(interval, db.stored_intervals(dataset, symbol))
                  if db is not None and dataset != BYBIT else [])
        if stored:
            iv = stored[0]
            # The upload's own months: the warm-up takes what it has.
            bar_months = [
                _Month(m, "cached" if n else "missing", iv, n)
                for m in months(start, end)
                for n in [db.count_bars(dataset, symbol, iv, m, _next_month(m))]  # type: ignore[union-attr]
            ]
            bars_source = dataset
        else:
            bar_months = [_bybit_month(db, symbol, bybit_kinds, m, end)
                          for m in months(first_bar, end)]
            bars_source = BYBIT
        if db is not None and dataset != BYBIT and db.has_funding(dataset, symbol):
            fund_months = [
                _Month(m, "cached" if n else "missing", None, n)
                for m in months(start, end)
                for n in [db.count_funding(dataset, symbol, m, _next_month(m))]
            ]
            funding_source = dataset
        else:
            fund_months = [_bybit_month(db, symbol, ["funding"], m, end) for m in months(start, end)]
            funding_source = BYBIT
        plans[symbol] = _SymbolPlan(bars_source, bar_months, funding_source, fund_months)
    return plans


# ------------------------------------------------------------------ loading


async def load_history(
    symbols: list[str],
    interval: str,
    start: datetime,
    end: datetime,
    *,
    warmup_bars: int = 60,
    cache_dir: Path | None = None,
    client: Any = None,
    rules_loader: Any = None,
    offline: bool = False,
    progress: Any = None,
    dataset: str = BYBIT,
) -> HistoryData:
    """Load (from the database, else Bybit) everything for *symbols* between
    *start* and *end*.  *client* / *rules_loader* replace the Bybit access
    in tests.  *offline*: never download — raise :class:`MissingHistory`
    listing what's missing.  *progress*: called with a line of text per
    download, for the Deploy page.  *dataset*: an upload to use (see the
    module docstring)."""
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"unsupported interval {interval!r}")
    cache = cache_dir or DEFAULT_CACHE
    db = HistoryDB.open(cache, readonly=offline)
    try:
        if dataset != BYBIT and (db is None or db.find(dataset) is None):
            raise MissingHistory([f"dataset {dataset!r}"])
        plans = _plan(db, symbols, interval, start, end, warmup_bars, dataset)
        if offline:
            missing = [
                f"{symbol} {what} {mo.month:%Y-%m}"
                + ("" if source == BYBIT else f" (not in {source})")
                for symbol, p in plans.items()
                for what, source, rows in (
                    (f"{interval} bars", p.bars_source, p.bars),
                    ("funding", p.funding_source, p.funding),
                )
                for mo in rows if mo.status != "cached"
            ]
            if not (cache / "instrument_rules.json").exists():
                missing.append("order-size rules")
            if missing:
                raise MissingHistory(missing)
        else:
            assert db is not None
            await _download(db, plans, interval, end, client, progress)
            plans = _plan(db, symbols, interval, start, end, warmup_bars, dataset)

        data = HistoryData(interval=interval, start=start, end=end)
        first_bar = bars_from(start, interval, warmup_bars)
        for symbol, p in plans.items():
            bars = _read_bars(db, symbol, interval, p, first_bar, end)
            rates = [] if db is None else db.funding(p.funding_source, symbol, start, end)
            data.bars[symbol] = bars
            data.funding[symbol] = with_marks(rates, bars)
            data.sources[symbol] = {"bars": p.bars_source, "funding": p.funding_source}
            log.info("%s: %d bars (%s), %d funding settlements (%s)", symbol, len(bars),
                     p.bars_source, len(rates), p.funding_source)
        data.rules = await _load_rules(cache, rules_loader)
        if db is not None and dataset != BYBIT:
            data.rules.update(db.rules(dataset))
        return data
    finally:
        if db is not None:
            db.close()


def _read_bars(db: HistoryDB | None, symbol: str, interval: str, p: _SymbolPlan,
               first_bar: datetime, end: datetime) -> list[TimeBar]:
    if db is None:
        return []
    if p.bars_source != BYBIT:
        iv = next((mo.interval for mo in p.bars if mo.interval), interval)
        return [b for b in aggregate(db.raw_bars(p.bars_source, symbol, iv, first_bar - timedelta(
            seconds=INTERVAL_SECONDS[interval]), end), interval)
            if first_bar <= b.timestamp <= end]
    out: list[TimeBar] = []
    for mo in p.bars:  # month by month: every bucket fits inside a month
        if mo.interval is None:
            continue
        month_end = _next_month(mo.month) - _MS
        out += aggregate(db.raw_bars(BYBIT, symbol, mo.interval, mo.month, month_end), interval)
    return [b for b in out if first_bar <= b.timestamp <= end]


async def _download(db: HistoryDB, plans: dict[str, _SymbolPlan], interval: str,
                    end: datetime, client: Any, progress: Any) -> None:
    """Fetch every Bybit month the plans lack: bars at *interval* itself."""
    todo = {
        symbol: (
            [mo.month for mo in p.bars if p.bars_source == BYBIT and mo.status != "cached"],
            [mo.month for mo in p.funding if p.funding_source == BYBIT and mo.status != "cached"],
        )
        for symbol, p in plans.items()
    }
    if not any(b or f for b, f in todo.values()):
        return
    own_client = None
    if client is None:
        from clients.bybit_linear import BybitLinearClient

        client = own_client = BybitLinearClient()
    try:
        for symbol, (bar_months, fund_months) in todo.items():
            fetched_at = datetime.now(UTC)
            for span in _runs(bar_months):
                span_end = _next_month(span[-1])
                if progress:
                    progress(f"{symbol}: {interval} bars {span[0]:%Y-%m} to {span[-1]:%Y-%m}")
                bars = await client.time_bars(
                    symbol, KlineInterval(interval), start=span[0], end=span_end
                )
                db.write_bars(BYBIT, symbol, interval,
                              [b for b in bars if span[0] <= b.timestamp < span_end])
                for m in span:
                    db.mark_fetched(symbol, f"bars_{interval}", m,
                                    min(_next_month(m), fetched_at), _next_month(m) <= fetched_at)
            for span in _runs(fund_months):
                if progress:
                    progress(f"{symbol}: funding {span[0]:%Y-%m} to {span[-1]:%Y-%m}")
                rates: list[FundingRate] = []
                window, span_end = span[0], _next_month(span[-1])
                while window < span_end:
                    window_end = min(window + _FUNDING_WINDOW, span_end)
                    rates += await client.funding_rates(symbol, start=window, end=window_end)
                    window = window_end
                db.write_funding(BYBIT, symbol, rates)
                for m in span:
                    db.mark_fetched(symbol, "funding", m,
                                    min(_next_month(m), fetched_at), _next_month(m) <= fetched_at)
    finally:
        if own_client is not None:
            await own_client.aclose()


async def download_history(
    symbols: list[str],
    interval: str,
    start: datetime,
    end: datetime,
    *,
    cache_dir: Path | None = None,
    client: Any = None,
    progress: Any = None,
    listings: dict[str, datetime] | None = None,
) -> None:
    """Fetch what the database lacks from Bybit for *symbols* over
    [start, end], without reading it back (the starter set).  Months
    entirely before a symbol's listing (*listings*) are skipped."""
    db = HistoryDB.open(cache_dir or DEFAULT_CACHE)
    assert db is not None
    try:
        plans = _plan(db, symbols, interval, start, end, 0, BYBIT)
        for symbol, p in plans.items():
            listed = (listings or {}).get(symbol)
            if listed is not None:
                p.bars = [mo for mo in p.bars if _next_month(mo.month) > listed]
                p.funding = [mo for mo in p.funding if _next_month(mo.month) > listed]
        await _download(db, plans, interval, end, client, progress)
    finally:
        db.close()


async def ensure_rules(cache_dir: Path | None = None, rules_loader: Any = None) -> bool:
    """Download Bybit's order-size rules if they aren't cached; whether they are now."""
    cache = cache_dir or DEFAULT_CACHE
    cache.mkdir(parents=True, exist_ok=True)
    return bool(await _load_rules(cache, rules_loader))


# ------------------------------------------------------------------ coverage


def coverage(
    symbols: list[str],
    interval: str,
    start: datetime,
    end: datetime,
    *,
    warmup_bars: int = 60,
    cache_dir: Path | None = None,
    listings: dict[str, datetime] | None = None,
    dataset: str = BYBIT,
) -> dict[str, Any]:
    """What the database holds for a backtest of *symbols* over [start, end]
    — no download.  Per symbol: where its bars and funding come from, and
    per month ``cached`` (with how many bars / settlements), ``partial``
    (downloaded before the month ended, not far enough), ``missing``, or
    ``before listing`` (Bybit has nothing: the symbol wasn't trading yet,
    from *listings*).  ``ready`` = a backtest can run offline."""
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"unsupported interval {interval!r}")
    cache = cache_dir or DEFAULT_CACHE
    listings = listings or {}
    db = HistoryDB.open(cache, readonly=True)
    try:
        known = db is not None and (dataset == BYBIT or db.find(dataset) is not None)
        plans = _plan(db, symbols, interval, start, end, warmup_bars, dataset)
        dataset_rules = db.rules(dataset) if db is not None and dataset != BYBIT else {}
    finally:
        if db is not None:
            db.close()

    def rows(symbol: str, source: str, items: list[_Month]) -> list[dict[str, Any]]:
        listed = listings.get(symbol) if source == BYBIT else None
        out = []
        for mo in items:
            status = mo.status
            if status != "cached" and listed is not None and _next_month(mo.month) <= listed:
                status = "before listing"
            out.append({"month": f"{mo.month:%Y-%m}", "status": status, "count": mo.count,
                        **({"from": mo.interval} if mo.interval and mo.interval != interval else {})})
        return out

    has_rules = (cache / "instrument_rules.json").exists()
    out: dict[str, Any] = {
        "interval": interval, "start": start.isoformat(), "end": end.isoformat(),
        "bars_from": bars_from(start, interval, warmup_bars).isoformat(),
        "dataset": dataset, "dataset_found": known, "rules": has_rules, "symbols": {},
    }
    for symbol, p in plans.items():
        listed = listings.get(symbol)
        bar_rows = rows(symbol, p.bars_source, p.bars)
        fund_rows = rows(symbol, p.funding_source, p.funding)
        out["symbols"][symbol] = {
            "listed": listed.isoformat() if listed else None,
            "bars_source": p.bars_source,
            "funding_source": p.funding_source,
            "own_rules": any(sym == symbol for _, sym in dataset_rules),
            "bars": bar_rows,
            "funding": fund_rows,
            "ready": all(r["status"] in ("cached", "before listing") for r in bar_rows + fund_rows),
        }
    out["ready"] = known and has_rules and all(s["ready"] for s in out["symbols"].values())
    return out


async def listing_dates(cache_dir: Path | None = None, loader: Any = None) -> dict[str, datetime]:
    """When each perp symbol started trading on Bybit (``launchTime``),
    cached for a day.  Empty if Bybit can't be reached and nothing is
    cached."""
    cache = cache_dir or DEFAULT_CACHE
    path = cache / "listings.json"
    cached = json.loads(path.read_text()) if path.exists() else None
    if cached is not None and datetime.now(UTC) - datetime.fromisoformat(
        cached["fetched_at"]
    ) < _LISTINGS_MAX_AGE:
        raw = cached["listings"]
    else:
        try:
            raw = await (loader or _fetch_listings)()
        except Exception as exc:
            log.warning("could not load listing dates (%s)", exc)
            raw = cached["listings"] if cached is not None else {}
        else:
            cache.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"fetched_at": datetime.now(UTC).isoformat(),
                                        "listings": raw}))
    return {symbol: datetime.fromtimestamp(int(ms) / 1000, tz=UTC) for symbol, ms in raw.items()}


async def _fetch_listings() -> dict[str, int]:
    """symbol → launchTime (ms) for every linear perp, from Bybit's public
    instruments-info endpoint."""
    import httpx

    out: dict[str, int] = {}
    cursor = ""
    async with httpx.AsyncClient(base_url="https://api.bybit.com", timeout=15.0) as http:
        for _ in range(20):
            params = {"category": "linear", "limit": "1000"}
            if cursor:
                params["cursor"] = cursor
            resp = await http.get("/v5/market/instruments-info", params=params)
            resp.raise_for_status()
            result = resp.json().get("result") or {}
            for item in result.get("list") or []:
                if item.get("launchTime"):
                    out[item["symbol"]] = int(item["launchTime"])
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
    return out


# --------------------------------------------- no longer used (kept as was)
#
# Before history.db the cache was JSON files: first one per (symbol,
# interval, exact period), then one per calendar month.  Neither is read or
# written any more.


def _piece_path(cache: Path, symbol: str, kind: str, month: datetime) -> Path:
    return cache / "months" / symbol / kind / f"{month:%Y-%m}.json"


def _read_piece(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    except ValueError:
        return None


def _write_pieces(
    cache: Path, symbol: str, kind: str, span: list[datetime], items: list[Any],
    fetched_at: datetime,
) -> None:
    for month in span:
        end = _next_month(month)
        mine = [x for x in items if month <= x.timestamp < end]
        path = _piece_path(cache, symbol, kind, month)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "until": min(end, fetched_at).isoformat(),
            "complete": end <= fetched_at,
            "items": [json.loads(codec.encode(x)) for x in mine],
        }))


def _read_cache(path: Path) -> tuple[list[TimeBar], list[FundingRate]]:
    raw = json.loads(path.read_text())
    bars = [codec.decode(json.dumps(b).encode()) for b in raw["bars"]]
    rates = [codec.decode(json.dumps(r).encode()) for r in raw["funding"]]
    return bars, rates  # type: ignore[return-value]


def _write_cache(path: Path, bars: list[TimeBar], rates: list[FundingRate]) -> None:
    path.write_text(json.dumps({
        "bars": [json.loads(codec.encode(b)) for b in bars],
        "funding": [json.loads(codec.encode(r)) for r in rates],
    }))


# -------------------------------------------------------------------- rules


async def _load_rules(
    cache: Path, rules_loader: Any
) -> dict[tuple[str, str], InstrumentRules]:
    path = cache / "instrument_rules.json"
    if path.exists():
        items = json.loads(path.read_text())
    else:
        try:
            loaded = await (rules_loader or _fetch_bybit_rules)()
        except Exception as exc:
            log.warning("could not load order-size rules (%s) — orders will be dropped", exc)
            return {}
        items = [rules_to_dict(ex, r) for ex, rules in loaded.items() for r in rules]
        path.write_text(json.dumps(items))
    return {(ex, r.symbol): r for ex, r in (rules_from_dict(d) for d in items)}


async def _fetch_bybit_rules() -> dict[str, list[InstrumentRules]]:
    """Every symbol's order-size rules from Bybit's public endpoint."""
    import httpx

    from execution.brokers._bybit_common import fetch_instrument_rules

    async with httpx.AsyncClient(base_url="https://api.bybit.com", timeout=15.0) as http:
        async def get(path: str, params: dict[str, Any]) -> dict[str, Any]:
            resp = await http.get(path, params=params)
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

        return {
            PERP_EXCHANGE: await fetch_instrument_rules(get, "linear"),
            SPOT_EXCHANGE: await fetch_instrument_rules(get, "spot"),
        }
