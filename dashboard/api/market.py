from __future__ import annotations

from fastapi import APIRouter, HTTPException

from dashboard.store import bars_store

router = APIRouter()

async def _fetch_bars_binance(symbol: str, interval: str) -> list:
    """Fetch the last 60 bars from Binance Futures public kline API."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": symbol, "interval": interval, "limit": "60"},
            )
        data = r.json()
        if not isinstance(data, list):
            return []
        return [
            {
                "time": int(row[0]) // 1000,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
            for row in data
        ]
    except Exception:
        return []


@router.get("/bars")
async def get_bars(symbol: str = "BTCUSDT", interval: str = "1m") -> dict:
    """Stored time bars for a symbol/interval, oldest first.

    Falls back to a direct Bybit public API fetch when the in-memory store
    is empty (e.g. right after a dashboard backend restart).
    """
    key = f"{symbol}_{interval}"
    bars = bars_store.get(key)
    if not bars:
        bars = await _fetch_bars_binance(symbol, interval)
        if bars:
            bars_store[key] = bars
    if not bars:
        raise HTTPException(status_code=404, detail=f"No bars for {symbol} {interval}")
    bars_sorted = sorted(bars, key=lambda b: b["time"])
    return {"symbol": symbol, "interval": interval, "bars": bars_sorted}


@router.get("/symbols")
async def get_symbols() -> dict:
    """All symbol+interval pairs that have received bar data.

    Also returns pairs inferred from registered strategy topics so the
    frontend can discover intervals even right after a backend restart
    before any NATS bars have arrived.
    """
    from dashboard.store import registered_strategies

    pairs = []
    seen: set[str] = set()

    for key in bars_store:
        sym, _, interval = key.partition("_")
        pairs.append({"symbol": sym, "interval": interval, "count": len(bars_store[key])})
        seen.add(key)

    # Fallback: derive known pairs from registered strategy topics
    for strat in registered_strategies.values():
        for topic in strat.get("topics", []):
            parts = topic.split(".")
            if len(parts) >= 4 and parts[2] == "bars":
                sym, interval = parts[1], parts[3]
                key = f"{sym}_{interval}"
                if key not in seen:
                    pairs.append({"symbol": sym, "interval": interval, "count": 0})
                    seen.add(key)

    return {"pairs": pairs}
