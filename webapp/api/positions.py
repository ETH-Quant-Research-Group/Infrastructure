from __future__ import annotations

from fastapi import APIRouter, HTTPException

from webapp.store import closed_positions, positions

router = APIRouter()


@router.get("/")
async def get_positions() -> dict:
    """Open positions plus recently closed ones (retained for 24 h)."""
    return {"positions": list(positions.values()) + list(closed_positions.values())}


@router.get("/{symbol}")
async def get_position(symbol: str) -> dict:
    """All open positions for a specific symbol across all exchanges."""
    matches = [p for key, p in positions.items() if key.endswith(f"_{symbol}")]
    if not matches:
        raise HTTPException(status_code=404, detail=f"No open position for '{symbol}'")
    return {"positions": matches}
