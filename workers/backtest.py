"""Backtest a strategy on a simulated Bybit, through the real pipeline.

::

    python -m workers.backtest --strategy FundingArbBybitStrategy \\
        --start 2026-08-01 --end 2026-10-01

Loads the history (bars at the strategy's interval + funding settlements,
cached in ``~/.qrf/backtest_cache``), runs the *unchanged* strategy class
through the same runner, consolidator, ledger, guard and funding split as
live — on a simulated exchange with a simulated clock (see
``engine/backtest/``) — and prints a summary.  Nothing touches the real
system: no NATS, no Docker, no API key; the ledger and funding files go to
a temporary folder (printed; ``--workdir`` to choose it).

Fills: the next bar's open after the signal, Bybit taker fees, optional
``--slippage-bps``.  Simplifications: spot is priced like the perp (no
basis), no margin / liquidation.  Exit code 0 = ran, 2 = failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from engine.backtest.data import DEFAULT_CACHE, INTERVAL_SECONDS, load_history
from engine.backtest.engine import run_backtest

log = logging.getLogger(__name__)


def _when(text: str) -> datetime:
    """``2026-08-01`` or ``2026-08-01T08:00`` (UTC)."""
    when = datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def strategy_markets(topics: list[str], interval: str | None) -> tuple[list[str], str]:
    """The symbols a strategy trades and the bar interval to load."""
    symbols = sorted({t.split(".")[1] for t in topics if len(t.split(".")) >= 3})
    intervals = sorted({t.split(".")[3] for t in topics if ".bars." in t and len(t.split(".")) == 4})
    if interval is None:
        if len(intervals) > 1:
            raise ValueError(
                f"the strategy uses several bar intervals {intervals} - pick one with --interval"
            )
        interval = intervals[0] if intervals else "1h"
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"unsupported interval {interval!r}")
    if not symbols:
        raise ValueError("the strategy subscribes to no symbols")
    return symbols, interval


def strategy_params(items: list[str]) -> dict[str, object]:
    """``["entry_threshold_annualized=0.08", "symbols=ETHUSDT,SOLUSDT"]`` ->
    constructor kwargs.  Values are read as JSON when they parse (numbers,
    true/false), a comma list becomes a tuple, anything else stays text."""
    params: dict[str, object] = {}
    for item in items:
        key, sep, text = item.partition("=")
        if not sep or not key:
            raise ValueError(f"--param needs key=value, got {item!r}")
        value: object
        try:
            value = json.loads(text)
        except ValueError:
            value = tuple(text.split(",")) if "," in text else text
        params[key.strip()] = value
    return params


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m workers.backtest", description=__doc__.split("\n")[0])
    p.add_argument("--strategy", required=True, help="strategy class name")
    p.add_argument("--start", required=True, type=_when, help="UTC, e.g. 2026-08-01")
    p.add_argument("--end", required=True, type=_when, help="UTC, e.g. 2026-10-01")
    p.add_argument("--capital", type=Decimal, default=Decimal(60_000), help="for the return %%")
    p.add_argument("--max-loss", type=Decimal, help="guard limit (default: the strategy's max_loss)")
    p.add_argument("--guard-unrealized", action="store_true",
                   help="the guard also counts open losses (like GUARD_INCLUDE_UNREALIZED)")
    p.add_argument("--slippage-bps", type=Decimal, default=Decimal(0))
    p.add_argument("--interval", help="bar interval to load (default: the strategy's)")
    p.add_argument("--warmup", type=int, default=60, help="bars before --start for on_history_bar")
    p.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--workdir", type=Path, help="where to keep the ledger / funding files")
    p.add_argument("--out", type=Path, help="also write the full result (trades, PnL curve) as JSON")
    p.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                   help="strategy constructor argument, repeatable")
    p.add_argument("--verbose", action="store_true", help="show the strategy's own INFO logs")
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    from workers.strategy_worker import _load_strategy

    if args.end <= args.start:
        print("--end must be after --start")
        return 2
    strategy_cls = _load_strategy(args.strategy)
    strategy = strategy_cls(**strategy_params(args.param))
    symbols, interval = strategy_markets(list(strategy.topics), args.interval)
    print(f"Loading {interval} history for {', '.join(symbols)} ...")
    data = await load_history(
        symbols, interval, args.start, args.end, warmup_bars=args.warmup, cache_dir=args.cache
    )
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="backtest-"))
    result = await run_backtest(
        strategy, data, workdir, capital=args.capital, max_loss=args.max_loss,
        include_unrealized=args.guard_unrealized, slippage_bps=args.slippage_bps,
    )
    print(result.summary())
    print(f"  files: {workdir}")
    if args.out:
        args.out.write_text(json.dumps(result.to_dict(), indent=2))
        print(f"  full result: {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s  %(name)s  %(message)s",
    )
    try:
        return asyncio.run(run(args))
    except Exception:
        log.exception("backtest failed")
        return 2


if __name__ == "__main__":
    sys.exit(main())
