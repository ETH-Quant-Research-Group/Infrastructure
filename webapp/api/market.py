from __future__ import annotations

from fastapi import APIRouter, HTTPException

from webapp.store import bars_store

router = APIRouter()

# Bybit V5 interval mapping — must match data/connectors/bybit_linear.py:35-38.
_BYBIT_INTERVALS: dict[str, str] = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "12h": "720",
    "1d": "D",
    "1w": "W",
    "1M": "M",
    # 8h is NOT natively supported — aggregated from 1h below.
}

_8H_MS = 8 * 3_600 * 1_000  # 28 800 000


def _aggregate_1h_to_8h(bars_1h: list[dict]) -> list[dict]:
    """Group 1h bars into 8h bars at settlement boundaries (00:00, 08:00, 16:00 UTC).

    Uses the same bucketing math as data/connectors/bybit_linear.py:335.
    """
    if not bars_1h:
        return []
    buckets: dict[int, list[dict]] = {}
    for bar in bars_1h:
        bucket_ms = (bar["time"] * 1000 // _8H_MS) * _8H_MS
        buckets.setdefault(bucket_ms, []).append(bar)

    result: list[dict] = []
    for bucket_ms in sorted(buckets):
        group = buckets[bucket_ms]
        result.append(
            {
                "time": bucket_ms // 1000,
                "open": group[0]["open"],
                "high": max(b["high"] for b in group),
                "low": min(b["low"] for b in group),
                "close": group[-1]["close"],
                "volume": sum(b["volume"] for b in group),
            }
        )
    return result


async def _fetch_bars_bybit(symbol: str, interval: str) -> list:
    """Fetch the last 60 bars from Bybit V5 linear kline API (public, no auth).

    For 8h: fetches 200 x 1h bars and aggregates into 8h at settlement boundaries.
    """
    try:
        import httpx

        # 8h is not natively supported — fetch 1h and aggregate.
        if interval == "8h":
            bybit_iv = "60"
            limit = "200"
        else:
            bybit_iv = _BYBIT_INTERVALS.get(interval)
            if bybit_iv is None:
                return []
            limit = "60"

        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.bybit.com/v5/market/kline",
                params={
                    "category": "linear",
                    "symbol": symbol,
                    "interval": bybit_iv,
                    "limit": limit,
                },
            )
        body = r.json()
        rows = body.get("result", {}).get("list", [])
        if not rows:
            return []

        # Bybit row: [startTime, open, high, low, close, volume, turnover]
        # Bybit returns newest-first — reverse for oldest-first.
        bars = [
            {
                "time": int(row[0]) // 1000,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
            for row in reversed(rows)
        ]

        if interval == "8h":
            bars = _aggregate_1h_to_8h(bars)

        return bars
    except Exception:
        return []


@router.get("/bars")
async def get_bars(symbol: str = "BTCUSDT", interval: str = "1m") -> dict:
    """Stored time bars for a symbol/interval, oldest first.

    Falls back to a direct Bybit V5 public API fetch when the in-memory store
    is empty (e.g. right after a dashboard backend restart).
    """
    key = f"{symbol}_{interval}"
    bars = bars_store.get(key)
    if not bars:
        bars = await _fetch_bars_bybit(symbol, interval)
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
    from webapp.store import registered_strategies

    pairs = []
    seen: set[str] = set()

    for key in bars_store:
        sym, _, interval = key.partition("_")
        pairs.append(
            {"symbol": sym, "interval": interval, "count": len(bars_store[key])}
        )
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
