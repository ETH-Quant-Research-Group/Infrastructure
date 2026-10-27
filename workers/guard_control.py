"""See or reset the consolidator's per-strategy guards.

::

    docker compose exec consolidator python -m workers.guard_control status
    docker compose exec consolidator python -m workers.guard_control reset FundingArbBybitStrategy
    docker compose exec consolidator python -m workers.guard_control close FundingArbBybitStrategy

``status`` lists every strategy's guard: the PnL it checks (counted from its
last reset), its limit, whether it's tripped and whether a trip closes its
positions.

``reset`` lifts a trip.  The guard re-arms from the strategy's **current**
PnL — its loss limit counts from there, so a strategy still below the old
line isn't tripped again straight away.  A strategy whose container the
manager stopped on the trip must then be started / redeployed again.

``close`` closes a **tripped** strategy's own positions (from the ledger),
as GUARD_CLOSE_ON_TRIP / CLOSE_ON_TRIP do on the trip itself — for when that
is off, or to retry after a close order failed.  Other strategies' positions
are never touched.

Talks to the running consolidator over NATS (it owns the guard state); fails
if the consolidator isn't running.  Exit code 0 = done, 2 = failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import nats

from config import NATS_URL

RESET_SUBJECT = "control.guard.reset"
STATUS_SUBJECT = "control.guard.status"
CLOSE_SUBJECT = "control.guard.close"


async def request(subject: str, body: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    nc = await nats.connect(NATS_URL)
    try:
        msg = await nc.request(subject, json.dumps(body).encode(), timeout=timeout)
        return json.loads(msg.data)  # type: ignore[no-any-return]
    finally:
        await nc.drain()


def format_status(guards: list[dict[str, Any]]) -> str:
    if not guards:
        return "No strategies known to the guard yet."
    lines = []
    for g in guards:
        state = (
            f"TRIPPED at {g['tripped_at']} (PnL {g['tripped_pnl']})"
            if g["tripped"]
            else "active"
        )
        pnl = g["pnl"] if g["pnl"] is not None else "unknown (price missing)"
        what = "incl. unrealized" if g["include_unrealized"] else "realized + funding - fees"
        lines.append(
            f"{g['strategy']}: {state}; PnL {pnl} ({what}, since reset at"
            f" {g['baseline']}), limit -{g['max_loss']}"
            + ("; a trip closes its positions" if g.get("close_on_trip") else "")
        )
    return "\n".join(lines)


async def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m workers.guard_control",
        description="See or reset the consolidator's per-strategy guards.",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="list every strategy's guard")
    reset = sub.add_parser("reset", help="lift a strategy's trip (re-arms from its current PnL)")
    reset.add_argument("strategy")
    close = sub.add_parser("close", help="close a tripped strategy's own positions")
    close.add_argument("strategy")
    args = p.parse_args(argv)

    try:
        if args.command == "status":
            reply = await request(STATUS_SUBJECT, {})
        elif args.command == "close":
            reply = await request(CLOSE_SUBJECT, {"strategy": args.strategy})
        else:
            reply = await request(RESET_SUBJECT, {"strategy": args.strategy})
    except Exception as exc:
        print(f"Could not reach the consolidator over NATS ({exc}) - is it running?")
        return 2

    if "error" in reply:
        print(f"Error: {reply['error']}")
        return 2
    if args.command == "status":
        print(format_status(reply["guards"]))
    elif args.command == "close":
        print(f"Closing the positions of {reply['closing']} - see the consolidator log.")
    else:
        print(f"Guard reset for {reply['reset']}: re-armed from PnL {reply['baseline']}.")
        print("If the manager stopped its container on the trip, start / redeploy it again.")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    sys.exit(main())
