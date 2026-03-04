from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def get_positions() -> dict:
    """Current open positions across all strategies."""
    return {"positions": []}


@router.get("/{symbol}")
async def get_position(symbol: str) -> dict:
    """Current position for a specific symbol."""
    return {"symbol": symbol, "position": None}
