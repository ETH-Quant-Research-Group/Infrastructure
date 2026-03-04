from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/bars")
async def get_bars(symbol: str = "BTCUSDT", interval: str = "1m") -> dict:
    """Latest time bars for a symbol."""
    return {"symbol": symbol, "interval": interval, "bars": []}


@router.get("/trades")
async def get_trades(symbol: str = "BTCUSDT") -> dict:
    """Recent trades for a symbol."""
    return {"symbol": symbol, "trades": []}


@router.get("/funding")
async def get_funding(symbol: str = "BTCUSDT") -> dict:
    """Latest funding rates for a futures symbol."""
    return {"symbol": symbol, "funding_rates": []}
