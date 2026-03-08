from __future__ import annotations

from fastapi import APIRouter, HTTPException

from dashboard.store import bars_store

router = APIRouter()


@router.get("/bars")
async def get_bars(symbol: str = "BTCUSDT", interval: str = "1m") -> dict:
    """Stored time bars for a symbol/interval, oldest first."""
    key = f"{symbol}_{interval}"
    bars = bars_store.get(key)
    if not bars:
        raise HTTPException(status_code=404, detail=f"No bars for {symbol} {interval}")
    return {"symbol": symbol, "interval": interval, "bars": bars}


@router.get("/symbols")
async def get_symbols() -> dict:
    """All symbol+interval pairs that have received bar data."""
    pairs = []
    for key in bars_store:
        sym, _, interval = key.partition("_")
        pairs.append(
            {"symbol": sym, "interval": interval, "count": len(bars_store[key])}
        )
    return {"pairs": pairs}
