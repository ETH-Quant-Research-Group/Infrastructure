"""Rebuild the consolidator's ledger from the recovery snapshots + Bybit.

Stop the consolidator first — it must be the ledger's only writer::

    docker compose stop consolidator
    docker compose run --rm consolidator python -m workers.recover_ledger
    docker compose run --rm consolidator python -m workers.recover_ledger --write
    docker compose start consolidator

**Check mode** (default): rebuilds into a temporary file, prints how it
differs from the current ledger, deletes the temporary file.  Nothing is
changed.  Exit code 0 = no differences, 1 = differences found, 2 = failed.

**--write**: after the same report, moves the current ledger to
``ledger.db.bak-<UTC time>`` and puts the rebuilt one in its place.  If the
current ledger exists and nothing differs, it is left as is.

How the rebuild works: ``engine/order/recovery.py``.  Uses the same brokers
(and Bybit credentials) as the consolidator.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engine.order import recovery
from engine.order.ledger import LedgerDB
from engine.order.snapshots import SnapshotDB

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

_QRF_DIR = Path.home() / ".qrf"


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m workers.recover_ledger",
        description="Rebuild the ledger from the latest snapshots + Bybit's order history.",
    )
    p.add_argument(
        "--write",
        action="store_true",
        help="replace the current ledger with the rebuilt one (keeps a backup)",
    )
    p.add_argument("--ledger", type=Path, default=_QRF_DIR / "ledger.db")
    p.add_argument("--snapshots", type=Path, default=_QRF_DIR / "snapshots.db")
    p.add_argument(
        "--lookback-days",
        type=float,
        default=7.0,
        help="how far back to read Bybit for a strategy without a snapshot (default 7)",
    )
    p.add_argument(
        "--margin",
        type=int,
        default=recovery.COUNTER_MARGIN,
        help=f"added to the highest order counter seen (default {recovery.COUNTER_MARGIN})",
    )
    return p.parse_args(argv)


def _open_readonly(path: Path, kind: str, opener: Callable[[Path], Any]) -> Any:
    if not path.exists():
        print(f"{kind} {path} not found - rebuilding without it")
        return None
    try:
        return opener(path)
    except sqlite3.DatabaseError as exc:
        print(f"{kind} {path} is unreadable ({exc}) - rebuilding without it")
        return None


async def run(args: argparse.Namespace, brokers: dict[str, Any]) -> int:
    now = datetime.now(UTC)
    current: LedgerDB | None = _open_readonly(
        args.ledger, "Ledger", lambda p: LedgerDB(p, readonly=True)
    )
    snapshot_db: SnapshotDB | None = _open_readonly(
        args.snapshots, "Snapshots", lambda p: SnapshotDB(p, readonly=True)
    )
    snapshots = snapshot_db.latest_all() if snapshot_db else []
    if snapshot_db:
        snapshot_db.close()

    strategies = set(current.strategy_ids()) if current else set()
    since = recovery.fetch_since(
        snapshots, strategies, now, timedelta(days=args.lookback_days)
    )
    try:
        orders = await recovery.fetch_orders(brokers, since, now)
    except Exception:
        if current:
            current.close()
        raise

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    rebuilt_path = args.ledger.with_name(f"{args.ledger.name}.rebuilt-{stamp}")
    target = LedgerDB(rebuilt_path)
    try:
        report = recovery.rebuild(target, current, snapshots, orders, since, args.margin)
        target.checkpoint()
    except Exception:
        target.close()
        rebuilt_path.unlink(missing_ok=True)
        raise
    finally:
        if current:
            current.close()
    target.close()

    print(report.text())

    if not args.write:
        rebuilt_path.unlink()
        print("\nCheck mode: nothing was changed. Run with --write to replace the ledger.")
        return 1 if report.has_differences else 0

    if current is not None and not report.has_differences:
        rebuilt_path.unlink()
        print("\nNo differences - the current ledger was left as is.")
        return 0

    backup = recovery.swap_in(args.ledger, rebuilt_path, now)
    kept = f" (the old one is kept as {backup})" if backup else ""
    print(f"\nLedger replaced: {args.ledger}{kept}")
    return 0


async def _run_and_close(args: argparse.Namespace) -> int:
    # Imported here so --help works without Bybit credentials.
    from workers.consolidator_worker import build_brokers

    brokers = build_brokers()
    try:
        return await run(args, brokers)
    finally:
        for broker in brokers.values():
            await broker.aclose()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    args = _parse(argv)
    try:
        return asyncio.run(_run_and_close(args))
    except Exception:
        log.exception("recovery failed - the current ledger was not changed")
        return 2


if __name__ == "__main__":
    sys.exit(main())
