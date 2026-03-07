from __future__ import annotations

from fastapi import APIRouter

from dashboard.store import pnl_latest, registered_strategies

router = APIRouter()


@router.get("/")
async def get_topology() -> dict:
    # Merge explicitly registered strategies with any seen only via PnL data.
    # This recovers gracefully after a dashboard restart that missed registrations.
    strategies = {s["name"]: s for s in registered_strategies.values()}
    for sid in pnl_latest:
        if sid not in strategies:
            strategies[sid] = {"name": sid, "topics": [], "max_loss": "?"}

    return {
        "strategies": list(strategies.values()),
        "feed_server": {
            "publishes": [
                "futures.{symbol}.bars.{interval}",
                "futures.{symbol}.trades",
                "futures.{symbol}.funding_rate",
                "spot.{symbol}.trades",
            ],
            "listens": ["control.subscribe.>", "control.unsubscribe.>"],
        },
        "consolidator": {
            "subscribes": ["signals.targets.*"],
            "publishes": ["orders.placed.{symbol}"],
        },
        "dashboard": {"subscribes": ["futures.>", "signals.>", "orders.>"]},
    }
