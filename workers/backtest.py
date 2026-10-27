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

How the Deploy page runs it (inside the strategy's own image, no network)::

    python -m workers.backtest --markets            # what the strategy trades (JSON)
    python -m workers.backtest --live-settings --offline --json-result \\
        --start ... --end ... --interval ...

- ``--strategy`` defaults to ``STRATEGY_NAME`` (baked into strategy images).
- ``--live-settings``: the guard is set up from the same environment as
  live — ``MAX_DRAWDOWN`` else ``DEFAULT_MAX_LOSS`` (1000) for the limit,
  ``CLOSE_ON_TRIP`` else ``GUARD_CLOSE_ON_TRIP``, ``GUARD_INCLUDE_UNREALIZED``,
  ``GUARD_UNREALIZED_EVERY``.
- ``--offline``: never download; fails listing the missing months.
- ``--json-result``: the full result as one ``__RESULT__{...}`` line at
  the end of the output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from engine.backtest.data import (
    DEFAULT_CACHE,
    INTERVAL_SECONDS,
    MissingHistory,
    load_history,
)
from engine.backtest.engine import run_backtest

log = logging.getLogger(__name__)

RESULT_PREFIX = "__RESULT__"


def _env_flag(name: str) -> bool | None:
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None


def live_settings() -> dict[str, object]:
    """The guard settings a live deploy of this container would get — same
    environment variables, same defaults (see workers/consolidator_worker.py)."""
    max_loss = Decimal(1000)
    for name in ("MAX_DRAWDOWN", "DEFAULT_MAX_LOSS"):
        raw = os.environ.get(name, "").strip()
        if raw:
            try:
                max_loss = Decimal(raw)
            except InvalidOperation:
                log.warning("%s=%r is not a number - ignored", name, raw)
                continue
            break
    close = _env_flag("CLOSE_ON_TRIP")
    if close is None:
        close = bool(_env_flag("GUARD_CLOSE_ON_TRIP"))
    every = os.environ.get("GUARD_UNREALIZED_EVERY", "0").strip() or "0"
    return {
        "max_loss": max_loss,
        "close_on_trip": close,
        "include_unrealized": bool(_env_flag("GUARD_INCLUDE_UNREALIZED")),
        "unrealized_every": int(every),
    }


def strategy_intervals(topics: list[str]) -> list[str]:
    """Every bar interval the strategy subscribes to."""
    return sorted({t.split(".")[3] for t in topics if ".bars." in t and len(t.split(".")) == 4})


def _when(text: str) -> datetime:
    """``2026-08-01`` or ``2026-08-01T08:00`` (UTC)."""
    when = datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def strategy_markets(topics: list[str], interval: str | None) -> tuple[list[str], str]:
    """The symbols a strategy trades and the bar interval to load."""
    symbols = sorted({t.split(".")[1] for t in topics if len(t.split(".")) >= 3})
    intervals = strategy_intervals(topics)
    if interval is not None and intervals and interval not in intervals:
        raise ValueError(
            f"the strategy gets {', '.join(intervals)} bars, not {interval}"
            " - it would see no bars at all"
        )
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
    p.add_argument("--strategy", default=os.environ.get("STRATEGY_NAME") or None,
                   help="strategy class name (default: STRATEGY_NAME)")
    p.add_argument("--markets", action="store_true",
                   help="only print the strategy's symbols and bar intervals as JSON")
    p.add_argument("--start", type=_when, help="UTC, e.g. 2026-08-01")
    p.add_argument("--end", type=_when, help="UTC, e.g. 2026-10-01")
    p.add_argument("--capital", type=Decimal, default=Decimal(60_000), help="for the return %%")
    p.add_argument("--max-loss", type=Decimal, help="guard limit (default: the strategy's max_loss)")
    p.add_argument("--guard-unrealized", action="store_true",
                   help="the guard also counts open losses (like GUARD_INCLUDE_UNREALIZED)")
    p.add_argument("--unrealized-every", type=int, default=0, metavar="X",
                   help="every X-th guard check counts open losses (like GUARD_UNREALIZED_EVERY)")
    p.add_argument("--close-on-trip", action="store_true",
                   help="a guard trip closes the strategy's positions (like CLOSE_ON_TRIP)")
    p.add_argument("--live-settings", action="store_true",
                   help="guard settings from the environment, exactly as live")
    p.add_argument("--offline", action="store_true", help="never download history")
    p.add_argument("--dataset", default="bybit",
                   help="an uploaded dataset to use instead of Bybit (default: bybit)")
    p.add_argument("--json-result", action="store_true",
                   help=f"end with the full result as one {RESULT_PREFIX}{{...}} line")
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

    if not args.strategy:
        print("--strategy is required (or set STRATEGY_NAME)")
        return 2
    strategy_cls = _load_strategy(args.strategy)
    strategy = strategy_cls(**strategy_params(args.param))
    topics = list(strategy.topics)
    if args.markets:
        symbols = sorted({t.split(".")[1] for t in topics if len(t.split(".")) >= 3})
        print(json.dumps({
            "strategy": strategy_cls.__name__, "symbols": symbols,
            "intervals": strategy_intervals(topics) or ["1h"], "warmup": args.warmup,
        }))
        return 0
    if args.start is None or args.end is None:
        print("--start and --end are required")
        return 2
    if args.end <= args.start:
        print("--end must be after --start")
        return 2
    symbols, interval = strategy_markets(topics, args.interval)

    guard: dict[str, object] = {
        "max_loss": args.max_loss, "include_unrealized": args.guard_unrealized,
        "unrealized_every": args.unrealized_every, "close_on_trip": args.close_on_trip,
    }
    if args.live_settings:
        guard = live_settings()
    if guard["include_unrealized"]:
        unrealized = "every check"
    elif guard["unrealized_every"]:
        unrealized = f"1 in every {guard['unrealized_every']} checks"
    else:
        unrealized = "never"
    print(f"Guard: limit {guard['max_loss'] or type(strategy).max_loss},"
          f" close on trip {'yes' if guard['close_on_trip'] else 'no'},"
          f" unrealized counted {unrealized}")

    print(f"Loading {interval} history for {', '.join(symbols)} ...")
    try:
        data = await load_history(
            symbols, interval, args.start, args.end, warmup_bars=args.warmup,
            cache_dir=args.cache, offline=args.offline, dataset=args.dataset,
        )
    except MissingHistory as exc:
        print(f"History missing ({exc}) - download it first")
        return 2
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="backtest-"))
    result = await run_backtest(
        strategy, data, workdir, capital=args.capital, slippage_bps=args.slippage_bps,
        **guard,  # type: ignore[arg-type]
    )
    print(result.summary())
    print(f"  files: {workdir}")
    if args.out:
        args.out.write_text(json.dumps(result.to_dict(), indent=2))
        print(f"  full result: {args.out}")
    if args.json_result:
        print(RESULT_PREFIX + json.dumps({**result.to_dict(), "interval": interval,
                                          "dataset": args.dataset, "sources": data.sources,
                                          "guard": {k: str(v) for k, v in guard.items()}}))
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
