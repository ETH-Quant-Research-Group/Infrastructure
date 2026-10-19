"""Strategy info worker — live info about a chosen set of strategies.

Started with strategy names and/or ids (both ways can be mixed).  Every one
is resolved to its id in the ledger; if any is unknown, it fails with all
the unknown ones listed.  Then, every ``interval`` seconds, it writes info
about all of them to a JSON file (default ``~/.qrf/strategy_info.json``):
holdings with live unrealized PnL, realized PnL, fees, net, the Sharpe
ratio (from the PnL history ``workers/snapshot_all.py`` records), trade
counts, unfinished trades and the last few trades.

At the same time it publishes each watched strategy's PnL on NATS as
``pnl.<strategy>`` — what the dashboard's per-strategy charts show (see
:func:`dashboard_pnl`).  Only the watched strategies get a chart.

Two functions, on :class:`StrategyInfoWorker` and also over NATS
request/reply while it runs:

- **add** — put more strategies on the list (all or nothing)::

      control.strategy_info.add  {"names": [...], "ids": [...]}
      → {"added": [ids], "watched": [ids]}  or  {"error": "..."}

- **info** — info for just the given ids::

      control.strategy_info.get  {"ids": [...]}
      → {"strategies": [...]}  or  {"error": "..."}

Run with::

    python -m workers.strategy_info --name FundingArbBybitStrategy --id 3

Without ``--name`` / ``--id`` it reads ``STRATEGY_INFO_NAMES`` and
``STRATEGY_INFO_IDS`` (comma-separated); an empty list is fine — add
strategies later.  Strategies added at runtime are not remembered across a
restart.  Other env vars: ``NATS_URL``, ``STRATEGY_INFO_INTERVAL`` (seconds,
default 5).

Read-only: opens the ledger without writing (the consolidator stays its
only writer) and gets prices from Bybit's public market endpoint — no API
key, so it can't place orders.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import nats

from config import NATS_URL
from engine.order.funding import UNATTRIBUTED, FundingDB
from engine.order.ledger import LedgerDB
from engine.order.performance import (
    BybitPrices,
    PnLHistoryDB,
    Sharpe,
    net_pnl,
    sharpe,
    total_unrealized,
    value_holdings,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    import nats.aio.client
    import nats.aio.msg

log = logging.getLogger(__name__)

_QRF_DIR = Path.home() / ".qrf"
ADD_SUBJECT = "control.strategy_info.add"
GET_SUBJECT = "control.strategy_info.get"
_LAST_TRADES = 5


class UnknownStrategyError(ValueError):
    """One or more strategy names / ids are not in the ledger."""

    def __init__(self, names: Iterable[str] = (), ids: Iterable[int] = ()) -> None:
        self.names = list(names)
        self.ids = list(ids)
        parts = [f"unknown strategy id {i}" for i in self.ids]
        parts += [f"unknown strategy name {n!r}" for n in self.names]
        super().__init__(", ".join(parts))


def dashboard_pnl(info: dict[str, Any], timestamp: str) -> dict[str, str] | None:
    """A strategy's info as the dashboard's ``pnl.<strategy>`` message
    (``webapp/store.py`` ``record_pnl``):

    - ``total_realized`` = realized + funding − fees (the locked-in part)
    - ``total_unrealized`` = unrealized at live prices
    - ``total`` = net

    ``None`` when a price is missing — skipped rather than sent as 0.
    """
    totals = info["totals"]
    if totals["net_pnl"] is None:
        return None
    locked = (
        Decimal(totals["realized_pnl"]) + Decimal(totals["funding"]) - Decimal(totals["fees_paid"])
    )
    return {
        "strategy_id": info["name"],
        "total_realized": str(locked),
        "total_unrealized": totals["unrealized_pnl"],
        "total": totals["net_pnl"],
        "timestamp": timestamp,
    }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


class StrategyInfoWorker:
    """Keeps a list of watched strategies and reports on them."""

    def __init__(
        self,
        ledger: LedgerDB,
        prices: Callable[[str, str], Awaitable[Decimal | None]],  # (exchange, symbol)
        output: Path,
        names: Iterable[str] = (),
        ids: Iterable[int] = (),
        pnl_history: Path | None = None,
        sharpe_period: timedelta = timedelta(days=1),
        funding: Path | None = None,
    ) -> None:
        self._ledger = ledger
        self._prices = prices
        self._output = output
        self._history_path = pnl_history
        self._history: PnLHistoryDB | None = None
        self._funding_path = funding
        self._funding: FundingDB | None = None
        self._sharpe_period = sharpe_period
        # strategy → (newest history row id, its Sharpe): recomputed only
        # when a new row has been recorded
        self._sharpe_cache: dict[str, tuple[int | None, Sharpe]] = {}
        self._publish: Callable[[str, bytes], Awaitable[None]] | None = None
        self._watched: list[int] = []
        self.add(names=names, ids=ids)

    @property
    def watched(self) -> list[int]:
        return list(self._watched)

    # ------------------------------------------------------------- functions

    def resolve(self, names: Iterable[str] = (), ids: Iterable[int] = ()) -> list[int]:
        """Ids for *names* and *ids*, in order, each once.  Raises
        :class:`UnknownStrategyError` listing every unknown one."""
        names, ids = list(names), list(ids)
        self._ledger.reload()
        by_name = self._ledger.strategy_ids()
        known = set(by_name.values())
        unknown_names = [n for n in names if n not in by_name]
        unknown_ids = [i for i in ids if i not in known]
        if unknown_names or unknown_ids:
            raise UnknownStrategyError(unknown_names, unknown_ids)
        resolved: list[int] = []
        for num in [*(by_name[n] for n in names), *ids]:
            if num not in resolved:
                resolved.append(num)
        return resolved

    def add(self, *, names: Iterable[str] = (), ids: Iterable[int] = ()) -> list[int]:
        """Put strategies on the watched list — all or nothing.  Returns the
        ids newly added (ones already watched are skipped)."""
        new = [num for num in self.resolve(names, ids) if num not in self._watched]
        self._watched += new
        return new

    async def info(self, ids: Iterable[int]) -> list[dict[str, Any]]:
        """Info for just these strategy ids.  Raises on unknown ones."""
        resolved = self.resolve(ids=ids)
        names = {num: name for name, num in self._ledger.strategy_ids().items()}
        prices: dict[tuple[str, str], Decimal | None] = {}
        return [await self._one(num, names[num], prices) for num in resolved]

    # ---------------------------------------------------------------- output

    def attach(self, publish: Callable[[str, bytes], Awaitable[None]]) -> None:
        """Also publish each watched strategy's PnL on ``pnl.<strategy>``
        at every write (e.g. ``nc.publish``) — what the dashboard's
        per-strategy charts read."""
        self._publish = publish

    async def write(self) -> None:
        """Write info about every watched strategy to the JSON file, and
        publish each one's PnL if a publisher is attached.

        Written to a temporary file and renamed, so a reader never sees a
        half-written file.
        """
        now = datetime.now(UTC).isoformat()
        strategies = await self.info(self._watched)
        funding = self._funding_db()
        unattributed = funding.by_symbol(UNATTRIBUTED) if funding else {}
        data = {
            "updated_at": now,
            "strategies": strategies,
            # funding no strategy accounts for (see engine/order/funding.py)
            "unattributed_funding": {
                "total": str(sum(unattributed.values(), Decimal(0))),
                "by_symbol": {s: str(a) for s, a in unattributed.items()},
            },
        }
        self._output.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._output.with_name(self._output.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self._output)

        if self._publish is not None:
            for info in strategies:
                payload = dashboard_pnl(info, now)
                if payload is not None:
                    await self._publish(f"pnl.{info['name']}", json.dumps(payload).encode())

    def close(self) -> None:
        if self._history is not None:
            self._history.close()
        if self._funding is not None:
            self._funding.close()

    def _funding_db(self) -> FundingDB | None:
        """The funding file, opened once the first funding run created it."""
        if self._funding is None and self._funding_path is not None and self._funding_path.exists():
            self._funding = FundingDB(self._funding_path, readonly=True)
        return self._funding

    async def run(self, interval: float) -> None:
        while True:
            try:
                await self.write()
            except Exception:
                log.exception("writing %s failed", self._output)
            await asyncio.sleep(interval)

    # --------------------------------------------------------------- helpers

    def _sharpe(self, name: str) -> Sharpe | None:
        """Sharpe from the PnL history, or ``None`` before the first daily
        run has created the history file."""
        if self._history is None:
            if self._history_path is None or not self._history_path.exists():
                return None
            self._history = PnLHistoryDB(self._history_path, readonly=True)
        last = self._history.last_id(name)
        cached = self._sharpe_cache.get(name)
        if cached is None or cached[0] != last:
            cached = (last, sharpe(self._history.history(name), self._sharpe_period))
            self._sharpe_cache[name] = cached
        return cached[1]

    async def _one(
        self, num: int, name: str, prices: dict[tuple[str, str], Decimal | None]
    ) -> dict[str, Any]:
        snap = self._ledger.snapshot(name)  # one consistent read
        last_trades = self._ledger.trades(name, limit=_LAST_TRADES)

        values = await value_holdings(snap.holdings, self._prices, prices)
        unrealized_total = total_unrealized(values)
        holdings = [
            {
                "exchange": v.holding.exchange,
                "symbol": v.holding.symbol,
                "quantity": str(v.holding.quantity),
                "avg_entry_price": str(v.holding.avg_entry_price),
                "price": _s(v.price),
                "unrealized_pnl": _s(v.unrealized_pnl),
                "realized_pnl": str(v.holding.realized_pnl),
                "fees_paid": str(v.holding.fees_paid),
            }
            for v in values
        ]
        ratio = self._sharpe(name)
        funding_db = self._funding_db()
        funding = funding_db.by_symbol(name) if funding_db else {}
        received = sum(funding.values(), Decimal(0))
        net = net_pnl(snap.realized_pnl, unrealized_total, received, snap.fees_paid)
        return {
            "id": num,
            "name": name,
            "orders": snap.counter,
            "holdings": holdings,
            "totals": {
                "realized_pnl": str(snap.realized_pnl),
                "unrealized_pnl": _s(unrealized_total),
                "funding": str(received),
                "fees_paid": str(snap.fees_paid),
                "net_pnl": _s(net),
            },
            "funding_by_symbol": {s: str(a) for s, a in funding.items()},
            # None until the daily PnL history has at least 2 returns
            "sharpe": {
                "value": ratio.value if ratio else None,
                "returns": ratio.returns if ratio else 0,
                "period_days": self._sharpe_period / timedelta(days=1),
            },
            "trades_by_status": snap.trades_by_status,
            "unfinished": snap.unfinished,
            "last_trades": [
                {
                    "order_link_id": t.order_link_id,
                    "exchange": t.exchange,
                    "symbol": t.symbol,
                    "side": t.side.value,
                    "status": t.status,
                    "requested_qty": str(t.requested_qty),
                    "filled_qty": str(t.filled_qty),
                    "avg_price": _s(t.avg_price),
                    "created_at": t.created_at.isoformat(),
                }
                for t in last_trades
            ],
        }


# ------------------------------------------------------------------ NATS


async def handle_add(worker: StrategyInfoWorker, data: bytes) -> dict[str, Any]:
    """Reply for a ``control.strategy_info.add`` request."""
    try:
        body = json.loads(data or b"{}")
        added = worker.add(
            names=[str(n) for n in body.get("names", [])],
            ids=[int(i) for i in body.get("ids", [])],
        )
    except (ValueError, TypeError, AttributeError) as exc:
        return {"error": str(exc)}
    if added:
        await worker.write()  # the file shows them right away
    return {"added": added, "watched": worker.watched}


async def handle_get(worker: StrategyInfoWorker, data: bytes) -> dict[str, Any]:
    """Reply for a ``control.strategy_info.get`` request."""
    try:
        body = json.loads(data or b"{}")
        return {"strategies": await worker.info([int(i) for i in body.get("ids", [])])}
    except (ValueError, TypeError, AttributeError) as exc:
        return {"error": str(exc)}


async def serve(nc: nats.aio.client.Client, worker: StrategyInfoWorker) -> None:
    async def _add(msg: nats.aio.msg.Msg) -> None:
        await msg.respond(json.dumps(await handle_add(worker, msg.data)).encode())

    async def _get(msg: nats.aio.msg.Msg) -> None:
        await msg.respond(json.dumps(await handle_get(worker, msg.data)).encode())

    await nc.subscribe(ADD_SUBJECT, cb=_add)
    await nc.subscribe(GET_SUBJECT, cb=_get)
    await asyncio.get_running_loop().create_future()


# ------------------------------------------------------------------ main


def _env_list(name: str) -> list[str]:
    return [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m workers.strategy_info",
        description="Write live info about a set of strategies to a JSON file.",
    )
    p.add_argument("--name", action="append", default=[], help="strategy name (repeatable)")
    p.add_argument("--id", action="append", type=int, default=[], help="strategy id (repeatable)")
    p.add_argument("--ledger", type=Path, default=_QRF_DIR / "ledger.db")
    p.add_argument("--output", type=Path, default=_QRF_DIR / "strategy_info.json")
    p.add_argument(
        "--pnl-history",
        type=Path,
        default=_QRF_DIR / "pnl_history.db",
        help="PnL history written by workers.snapshot_all, for the Sharpe ratio",
    )
    p.add_argument(
        "--funding",
        type=Path,
        default=_QRF_DIR / "funding.db",
        help="funding split written by workers.snapshot_all",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("STRATEGY_INFO_INTERVAL", "5")),
        help="seconds between writes (default 5)",
    )
    args = p.parse_args(argv)
    if not args.name and not args.id:
        args.name = _env_list("STRATEGY_INFO_NAMES")
        args.id = [int(i) for i in _env_list("STRATEGY_INFO_IDS")]
    return args


async def _main(args: argparse.Namespace) -> int:
    while not args.ledger.exists():
        log.info("waiting for %s (created by the consolidator)", args.ledger)
        await asyncio.sleep(5)

    ledger = LedgerDB(args.ledger, readonly=True)
    prices = BybitPrices()
    try:
        try:
            worker = StrategyInfoWorker(
                ledger, prices, args.output, args.name, args.id, args.pnl_history,
                funding=args.funding,
            )
        except UnknownStrategyError as exc:
            log.error("%s", exc)
            return 2
        log.info(
            "watching %s -> %s every %ss", worker.watched, args.output, args.interval
        )
        nc = await nats.connect(NATS_URL)
        worker.attach(nc.publish)  # dashboard charts: pnl.<strategy>
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(worker.run(args.interval))
                tg.create_task(serve(nc, worker))
        finally:
            await nc.drain()
            worker.close()
    finally:
        await prices.aclose()
        ledger.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return asyncio.run(_main(_parse(argv)))
    except KeyboardInterrupt:
        log.info("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
