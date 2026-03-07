from __future__ import annotations

from fastapi import APIRouter

from dashboard.store import registered_strategies

router = APIRouter()


@router.get("/")
async def get_topology() -> dict:
    return {
        "strategies": list(registered_strategies.values()),
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
