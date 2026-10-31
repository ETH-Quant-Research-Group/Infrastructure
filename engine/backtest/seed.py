"""The starter set: history gathered automatically for the most-traded
Bybit USDT perps, so most backtests have their data before anyone asks.

By default the top 50 by 24h turnover, 1h bars + funding, 3 years back
(longer timeframes up to 1d are built from the 1h bars).  The webapp runs
:meth:`Seeder.run_forever` in the background: a first pass at start-up,
then once a day, which only fetches what's new (the current month, and
newly listed symbols).  Symbols that drop out of the top keep their data.

Settings (webapp env): ``HISTORY_SEED`` (``off`` to disable),
``HISTORY_SEED_TOP`` (50), ``HISTORY_SEED_INTERVAL`` (``1h``),
``HISTORY_SEED_YEARS`` (3).  By hand::

    docker compose exec webapp python -m engine.backtest.seed [--top 50] [--years 3]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from engine.backtest.data import (
    DEFAULT_CACHE,
    download_history,
    ensure_rules,
    listing_dates,
)

log = logging.getLogger(__name__)

_EVERY = timedelta(days=1)


async def top_symbols(n: int, loader: Any = None) -> list[str]:
    """The *n* USDT perps with the highest 24h turnover on Bybit."""
    tickers = await (loader or _fetch_tickers)()
    usdt = [t for t in tickers if str(t.get("symbol", "")).endswith("USDT")]
    usdt.sort(key=lambda t: float(t.get("turnover24h") or 0), reverse=True)
    return [t["symbol"] for t in usdt[:n]]


async def _fetch_tickers() -> list[dict[str, Any]]:
    import httpx

    async with httpx.AsyncClient(
        base_url="https://api.bybit.com", timeout=15.0
    ) as http:
        resp = await http.get("/v5/market/tickers", params={"category": "linear"})
        resp.raise_for_status()
        return list((resp.json().get("result") or {}).get("list") or [])


class Seeder:
    """Gathers the starter set; :attr:`status` is shown on the Deploy page."""

    def __init__(
        self,
        *,
        top: int = 50,
        interval: str = "1h",
        years: int = 3,
        cache_dir: Path | None = None,
        lock: asyncio.Lock | None = None,
    ) -> None:
        self.top, self.interval, self.years = top, interval, years
        self.cache_dir = cache_dir or DEFAULT_CACHE
        self.lock = lock or asyncio.Lock()  # shared with the page's downloads
        self.status: dict[str, Any] = {
            "enabled": True,
            "running": False,
            "top": top,
            "interval": interval,
            "years": years,
            "symbols": [],
            "done": 0,
            "current": None,
            "last_started": None,
            "last_finished": None,
            "errors": [],
        }

    @classmethod
    def from_env(cls, lock: asyncio.Lock | None = None) -> Seeder:
        seeder = cls(
            top=int(os.environ.get("HISTORY_SEED_TOP", "50")),
            interval=os.environ.get("HISTORY_SEED_INTERVAL", "1h"),
            years=int(os.environ.get("HISTORY_SEED_YEARS", "3")),
            lock=lock,
        )
        seeder.status["enabled"] = os.environ.get(
            "HISTORY_SEED", "on"
        ).strip().lower() not in ("0", "off", "false", "no")
        return seeder

    async def run_once(self) -> None:
        """One pass: refresh the top list, fetch whatever is missing."""
        if self.status["running"]:
            return
        self.status.update(
            running=True,
            done=0,
            current=None,
            errors=[],
            last_started=datetime.now(UTC).isoformat(),
        )
        try:
            symbols = await top_symbols(self.top)
            self.status["symbols"] = symbols
            listings = await listing_dates(self.cache_dir)
            end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
            start = end - timedelta(days=365 * self.years)
            async with self.lock:
                await ensure_rules(self.cache_dir)
            for symbol in symbols:
                self.status["current"] = symbol
                try:
                    # One symbol at a time, so a download from the page
                    # waits for one symbol, not the whole pass.
                    async with self.lock:
                        await download_history(
                            [symbol],
                            self.interval,
                            start,
                            end,
                            cache_dir=self.cache_dir,
                            listings=listings,
                        )
                except Exception as exc:
                    log.warning("starter set: %s failed: %s", symbol, exc)
                    self.status["errors"].append(f"{symbol}: {exc}")
                self.status["done"] += 1
        except Exception as exc:
            log.warning("starter set: pass failed: %s", exc)
            self.status["errors"].append(str(exc))
        finally:
            self.status.update(
                running=False, current=None, last_finished=datetime.now(UTC).isoformat()
            )
            log.info(
                "starter set: %d symbols checked, %d errors",
                self.status["done"],
                len(self.status["errors"]),
            )

    async def run_forever(self) -> None:
        if not self.status["enabled"]:
            log.info("starter set: HISTORY_SEED=off")
            return
        while True:
            await self.run_once()
            await asyncio.sleep(_EVERY.total_seconds())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m engine.backtest.seed",
        description="Gather the backtest starter set now.",
    )
    p.add_argument("--top", type=int, default=50)
    p.add_argument("--interval", default="1h")
    p.add_argument("--years", type=int, default=3)
    p.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    seeder = Seeder(
        top=args.top, interval=args.interval, years=args.years, cache_dir=args.cache
    )
    asyncio.run(seeder.run_once())
    print(
        f"{seeder.status['done']} symbols checked;"
        f" errors: {seeder.status['errors'] or 'none'}"
    )
    return 0 if not seeder.status["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
