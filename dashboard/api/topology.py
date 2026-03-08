from __future__ import annotations

from fastapi import APIRouter

from dashboard.store import broker_exchange_states, pnl_latest, registered_strategies

router = APIRouter()


@router.get("/")
async def get_topology() -> dict:
    # Merge explicitly registered strategies with any seen only via PnL data.
    # This recovers gracefully after a dashboard restart that missed registrations.
    strategies = {s["name"]: s for s in registered_strategies.values()}
    for sid in pnl_latest:
        if sid not in strategies:
            strategies[sid] = {"name": sid, "topics": [], "max_loss": "?"}

    # A broker is "active" if its last heartbeat was within 15 seconds.
    import datetime as _dt

    now = _dt.datetime.now(_dt.UTC)
    brokers = []
    for state in broker_exchange_states.values():
        try:
            last_seen = _dt.datetime.fromisoformat(state["last_seen"])
            active = (now - last_seen).total_seconds() < 15
        except (KeyError, ValueError):
            active = False
        brokers.append({**state, "active": active})

    return {
        "strategies": list(strategies.values()),
        "brokers": brokers,
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
            "publishes": ["orders.placed.{symbol}", "broker.pnl.{exchange}"],
        },
        "dashboard": {"subscribes": ["futures.>", "signals.>", "orders.>"]},
    }
