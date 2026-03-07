from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter

from dashboard.store import fills_by_strategy, orders

router = APIRouter()

_ACTIVE_WINDOW = timedelta(seconds=30)


@router.get("/")
async def get_orders() -> dict:
    """Full order history, newest first."""
    return {"orders": list(reversed(orders))}


@router.get("/strategy/{strategy_id}")
async def get_strategy_fills(strategy_id: str) -> dict:
    """Fill history for a single strategy, newest first."""
    fills = fills_by_strategy.get(strategy_id, [])
    return {"fills": list(reversed(fills))}


@router.get("/active")
async def get_active_orders() -> dict:
    """Orders placed within the last 30 seconds."""
    cutoff = datetime.now(UTC) - _ACTIVE_WINDOW
    active = [o for o in orders if datetime.fromisoformat(o["placed_at"]) >= cutoff]
    return {"orders": list(reversed(active))}
