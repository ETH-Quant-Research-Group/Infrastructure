"""Daily run: recovery snapshots, funding split and PnL history for every strategy.

Run once a day, e.g. from the host's crontab (``-T``: no terminal under cron)::

    0 0 * * *  cd /path/to/Infrastructure && docker compose exec -T consolidator python -m workers.snapshot_all

Three independent jobs, all run by default, in this order; ``--only
snapshots|funding|pnl`` runs just one, so each can get its own cron schedule
(e.g. PnL every hour, the rest once a day):

- :func:`take_snapshots` — a recovery snapshot of every strategy that has
  placed orders since its last one (``~/.qrf/snapshots.db``).  Keeps the
  orders since each strategy's last snapshot within Bybit's order history,
  which ``workers/recover_ledger.py`` replays them from.
- :func:`collect_funding_job` — fetch Bybit's funding payments since the
  last run and split each across the strategies by their positions at
  settlement time; what no strategy accounts for goes to ``_unattributed``
  (``~/.qrf/funding.db``, see ``engine/order/funding.py``).  Needs the
  Bybit API key, like the consolidator.
- :func:`record_pnl` — one PnL row for **every** strategy, traded or not
  (``~/.qrf/pnl_history.db``): realized, unrealized at live Bybit prices,
  funding, fees, net.  The history behind the per-strategy Sharpe ratio
  (see ``engine/order/performance.py``).

A failing job doesn't stop the others (a funding hiccup shouldn't leave a
gap in the PnL history); the exit code is then 2.

Safe to run while the consolidator is trading: the ledger is opened
read-only and each strategy is read in one transaction.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engine.order.funding import UNATTRIBUTED, FundingDB, collect_funding
from engine.order.ledger import LedgerDB
from engine.order.performance import BybitPrices, PnLHistoryDB, strategy_pnl
from engine.order.snapshots import SnapshotDB, take_all

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from decimal import Decimal

log = logging.getLogger(__name__)

_QRF_DIR = Path.home() / ".qrf"
JOBS = ("snapshots", "funding", "pnl")


def take_snapshots(ledger: LedgerDB, snapshots_path: Path) -> None:
    """Recovery snapshot of every strategy with orders since its last one."""
    snapshots = SnapshotDB(snapshots_path)
    try:
        results = take_all(ledger, snapshots)
    finally:
        snapshots.close()
    if not results:
        print("Snapshots: no strategies in the ledger.")
    for strategy, counter, saved in results:
        what = "snapshot saved" if saved else "unchanged since its last snapshot"
        print(f"Snapshot {strategy}: {what} (order {counter})")


async def collect_funding_job(
    ledger: LedgerDB, funding_path: Path, brokers: dict[str, Any]
) -> None:
    """Fetch and split the funding payments since the last run."""
    db = FundingDB(funding_path)
    try:
        sources = {n: b for n, b in brokers.items() if hasattr(b, "funding_settlements")}
        if not sources:
            print("Funding: no broker reports funding payments.")
        for exchange, broker in sources.items():
            recorded = await collect_funding(ledger, db, broker, datetime.now(UTC))
            if not recorded:
                print(f"Funding {exchange}: no new settlements.")
            for settlement, shares in recorded:
                parts = ", ".join(f"{name} {amount}" for name, amount in shares.items())
                print(
                    f"Funding {settlement.symbol} {settlement.settled_at:%Y-%m-%d %H:%M}:"
                    f" {settlement.amount} -> {parts or 'nobody (0)'}"
                )
        unattributed = db.total(UNATTRIBUTED)
        if unattributed:
            print(f"Funding {UNATTRIBUTED} total so far: {unattributed}")
    finally:
        db.close()


async def record_pnl(
    ledger: LedgerDB,
    history_path: Path,
    prices: Callable[[str, str], Awaitable[Decimal | None]],
    funding_path: Path | None = None,
) -> None:
    """Record every strategy's current PnL — realized, unrealized at live
    prices, funding, fees, net — whether it traded since the last recording
    or not (its unrealized PnL moves anyway).  A strategy whose price
    couldn't be fetched gets an incomplete row (no unrealized / net), which
    the Sharpe calculation skips."""
    history = PnLHistoryDB(history_path)
    funding = (
        FundingDB(funding_path, readonly=True)
        if funding_path is not None and funding_path.exists()
        else None
    )
    cache: dict[tuple[str, str], Decimal | None] = {}
    try:
        strategies = sorted(name for name in ledger.strategies() if ledger.counter(name))
        if not strategies:
            print("PnL: no strategies with orders in the ledger.")
        for name in strategies:
            point = await strategy_pnl(ledger, name, prices, cache, funding)
            history.record(point)
            if point.complete:
                print(
                    f"PnL {name}: net {point.net_pnl} (realized {point.realized_pnl},"
                    f" unrealized {point.unrealized_pnl}, funding {point.funding},"
                    f" fees {point.fees_paid})"
                )
            else:
                print(
                    f"PnL {name}: INCOMPLETE - a price was unavailable; realized"
                    f" {point.realized_pnl}, funding {point.funding},"
                    f" fees {point.fees_paid}"
                )
    finally:
        history.close()
        if funding is not None:
            funding.close()


async def run(
    ledger_path: Path,
    snapshots_path: Path,
    history_path: Path,
    only: str | None = None,
    prices: Callable[[str, str], Awaitable[Decimal | None]] | None = None,
    *,
    funding_path: Path | None = None,
    brokers: dict[str, Any] | None = None,
) -> int:
    if not ledger_path.exists():
        print(f"Ledger {ledger_path} not found - nothing to do")
        return 2
    funding_path = funding_path or history_path.with_name("funding.db")
    jobs = JOBS if only is None else (only,)
    ledger = LedgerDB(ledger_path, readonly=True)
    own_prices = own_brokers = None
    failed = []
    try:
        for job in jobs:
            try:
                if job == "snapshots":
                    take_snapshots(ledger, snapshots_path)
                elif job == "funding":
                    if brokers is None:
                        # Imported here: needs the Bybit API key only for this job.
                        from workers.consolidator_worker import build_brokers

                        brokers = own_brokers = build_brokers()
                    await collect_funding_job(ledger, funding_path, brokers)
                elif job == "pnl":
                    if prices is None:
                        prices = own_prices = BybitPrices()
                    await record_pnl(ledger, history_path, prices, funding_path)
            except Exception:
                log.exception("%s job failed", job)
                failed.append(job)
    finally:
        ledger.close()
        if own_prices is not None:
            await own_prices.aclose()
        for broker in (own_brokers or {}).values():
            await broker.aclose()
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(
        prog="python -m workers.snapshot_all",
        description="Recovery snapshots, funding split and PnL history for every strategy.",
    )
    p.add_argument("--only", choices=JOBS, help="run just one job (default: all, in order)")
    p.add_argument("--ledger", type=Path, default=_QRF_DIR / "ledger.db")
    p.add_argument("--snapshots", type=Path, default=_QRF_DIR / "snapshots.db")
    p.add_argument("--funding", type=Path, default=_QRF_DIR / "funding.db")
    p.add_argument("--pnl-history", type=Path, default=_QRF_DIR / "pnl_history.db")
    args = p.parse_args(argv)
    try:
        return asyncio.run(
            run(
                args.ledger, args.snapshots, args.pnl_history, args.only,
                funding_path=args.funding,
            )
        )
    except Exception:
        log.exception("daily run failed")
        return 2


if __name__ == "__main__":
    sys.exit(main())
