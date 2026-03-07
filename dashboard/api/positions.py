from __future__ import annotations

from fastapi import APIRouter, HTTPException

from dashboard.store import closed_positions, positions

router = APIRouter()


@router.get("/")
async def get_positions() -> dict:
    """Open positions plus recently closed ones (retained for 24 h)."""
    return {"positions": list(positions.values()) + list(closed_positions.values())}


@router.get("/{symbol}")
async def get_position(symbol: str) -> dict:
    """Current broker position for a specific symbol."""
    pos = positions.get(symbol)
    if pos is None:
        raise HTTPException(status_code=404, detail=f"No open position for '{symbol}'")
    return {"position": pos}
