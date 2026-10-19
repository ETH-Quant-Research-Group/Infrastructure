"""Historical data for a backtest: bars, funding rates, order-size rules.

Fetched from Bybit's public API (no key) and cached as JSON files in
``cache_dir`` (default ``~/.qrf/backtest_cache``), so a rerun over the same
period is fast and works offline.

- **Bars** for each symbol at the strategy's interval, from ``warmup_bars``
  before the start (for ``on_history_bar``) to the end.
- **Funding rates** at every settlement in the period.  Bybit's history has
  no mark price, so each gets the close of the last bar before it.  Fetched
  in 60-day windows: the connector's own paging stops at 200 records.
- **Order-size rules** for perps and spot, mapped to the backtest's
  exchanges (``bybit_demo`` / ``bybit_spot``).
"""

from __future__ import annotations

import bisect
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from data.connectors.types import KlineInterval
from data.types import FundingRate, TimeBar
from engine.data import codec
from engine.order.instruments import rules_from_dict, rules_to_dict

if TYPE_CHECKING:
    from execution.types import InstrumentRules

log = logging.getLogger(__name__)

DEFAULT_CACHE = Path.home() / ".qrf" / "backtest_cache"
PERP_EXCHANGE = "bybit_demo"
SPOT_EXCHANGE = "bybit_spot"
_FUNDING_WINDOW = timedelta(days=60)

INTERVAL_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200,
    "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400,
}


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


def with_marks(rates: list[FundingRate], bars: list[TimeBar]) -> list[FundingRate]:
    """Fill each rate's mark price with the last bar close before it."""
    closes = [b.close_time for b in bars]
    out = []
    for rate in rates:
        i = bisect.bisect_right(closes, rate.timestamp) - 1
        if i >= 0:
            out.append(replace(rate, mark_price=bars[i].close))
    return out


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
) -> HistoryData:
    """Load (from the cache, else Bybit) everything for *symbols* between
    *start* and *end*.  *client* / *rules_loader* replace the Bybit access
    in tests."""
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"unsupported interval {interval!r}")
    cache = cache_dir or DEFAULT_CACHE
    cache.mkdir(parents=True, exist_ok=True)
    data = HistoryData(interval=interval, start=start, end=end)
    bars_from = start - timedelta(seconds=INTERVAL_SECONDS[interval] * warmup_bars)

    own_client = None
    if client is None:
        from clients.bybit_linear import BybitLinearClient

        client = own_client = BybitLinearClient()
    try:
        for symbol in symbols:
            path = cache / f"{symbol}_{interval}_{bars_from:%Y%m%dT%H}_{end:%Y%m%dT%H}.json"
            if path.exists():
                bars, rates = _read_cache(path)
            else:
                bars = await client.time_bars(
                    symbol, KlineInterval(interval), start=bars_from, end=end
                )
                rates = []
                window = start
                while window < end:
                    window_end = min(window + _FUNDING_WINDOW, end)
                    rates += await client.funding_rates(symbol, start=window, end=window_end)
                    window = window_end
                _write_cache(path, bars, rates)
            bars = sorted({b.timestamp: b for b in bars}.values(), key=lambda b: b.timestamp)
            rates = sorted(
                {r.timestamp: r for r in rates if start <= r.timestamp <= end}.values(),
                key=lambda r: r.timestamp,
            )
            data.bars[symbol] = bars
            data.funding[symbol] = with_marks(rates, bars)
            log.info("%s: %d bars, %d funding settlements", symbol, len(bars), len(rates))
    finally:
        if own_client is not None:
            await own_client.aclose()

    data.rules = await _load_rules(cache, rules_loader)
    return data


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
