"""Bybit linear futures connector — REST + WebSocket for USDT-margined perps.

Provides historical klines, funding rate history, and live WebSocket streams
for mark price / funding rates and klines.

Bybit does not offer a native 8h kline interval (available: 1,3,5,15,30,60,
120,240,360,720,D,W,M in minutes).  This connector fetches 1h klines and
aggregates them into 8h bars at settlement boundaries (00:00, 08:00, 16:00 UTC)
when the caller requests ``KlineInterval.H8``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import httpx
import websockets

from data.connectors.types import KlineInterval, RawFundingRate, RawKline, RawMarkPrice

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

log = logging.getLogger(__name__)

_REST_BASE = "https://api.bybit.com"
_WS_PUBLIC = "wss://stream.bybit.com/v5/public/linear"
_KLINE_PAGE = 200  # Bybit max per request

# Map our KlineInterval enum to Bybit's interval parameter (minutes or D/W/M).
_BYBIT_INTERVALS: dict[str, str] = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360",
    "12h": "720", "1d": "D", "1w": "W", "1M": "M",
    # 8h is NOT natively supported — handled by aggregating 1h bars.
}

_8H_MS = 8 * 3_600 * 1_000  # 8 hours in milliseconds


class BybitLinearConnector:
    """Async connector for Bybit V5 linear (USDT-margined) futures."""

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=_REST_BASE, timeout=timeout,
        )

    # ------------------------------------------------------------------ REST

    async def fetch_klines(
        self,
        symbol: str,
        interval: KlineInterval,
        *,
        start_ms: int,
        end_ms: int,
    ) -> AsyncGenerator[RawKline, None]:
        """Yield klines in [start_ms, end_ms], oldest first.

        For 8h: fetches 1h bars and aggregates into 8h at settlement boundaries.
        """
        if interval is KlineInterval.H8:
            async for k in self._fetch_klines_8h(symbol, start_ms=start_ms, end_ms=end_ms):
                yield k
            return

        bybit_iv = _BYBIT_INTERVALS.get(interval.value)
        if bybit_iv is None:
            raise ValueError(f"Unsupported interval for Bybit: {interval}")

        all_klines: list[RawKline] = []
        cursor = end_ms
        while True:
            resp = await self._client.get(
                "/v5/market/kline",
                params={
                    "category": "linear",
                    "symbol": symbol,
                    "interval": bybit_iv,
                    "start": str(start_ms),
                    "end": str(cursor),
                    "limit": str(_KLINE_PAGE),
                },
            )
            resp.raise_for_status()
            rows = resp.json().get("result", {}).get("list", [])
            if not rows:
                break
            for row in rows:
                k = _parse_rest_kline(row)
                if k["open_time_ms"] >= start_ms:
                    all_klines.append(k)
            oldest_ms = int(rows[-1][0])
            if oldest_ms <= start_ms or len(rows) < _KLINE_PAGE:
                break
            cursor = oldest_ms - 1

        # Bybit returns newest-first; yield oldest-first.
        all_klines.sort(key=lambda k: k["open_time_ms"])
        for k in all_klines:
            yield k

    async def _fetch_klines_8h(
        self, symbol: str, *, start_ms: int, end_ms: int,
    ) -> AsyncGenerator[RawKline, None]:
        """Fetch 1h klines from Bybit and aggregate into 8h bars."""
        h1_bars: list[RawKline] = []
        async for k in self.fetch_klines(
            symbol, KlineInterval.H1, start_ms=start_ms, end_ms=end_ms,
        ):
            h1_bars.append(k)

        for bar in _aggregate_to_8h(h1_bars):
            yield bar

    async def fetch_funding_rates(
        self,
        symbol: str,
        *,
        start_ms: int,
        end_ms: int | None = None,
    ) -> AsyncGenerator[RawFundingRate, None]:
        """Yield historical funding rate settlements, oldest first."""
        all_rates: list[RawFundingRate] = []
        cursor_ms = start_ms
        while True:
            params: dict[str, str] = {
                "category": "linear",
                "symbol": symbol,
                "startTime": str(cursor_ms),
                "limit": "200",
            }
            if end_ms is not None:
                params["endTime"] = str(end_ms)
            resp = await self._client.get(
                "/v5/market/funding/history", params=params,
            )
            resp.raise_for_status()
            rows = resp.json().get("result", {}).get("list", [])
            if not rows:
                break
            for row in rows:
                all_rates.append(RawFundingRate(
                    symbol=row["symbol"],
                    funding_time_ms=int(row["fundingRateTimestamp"]),
                    funding_rate=row["fundingRate"],
                    mark_price="0",  # not in historical endpoint
                ))
            oldest_ms = int(rows[-1]["fundingRateTimestamp"])
            if oldest_ms <= cursor_ms or len(rows) < 200:
                break
            cursor_ms = oldest_ms + 1

        all_rates.sort(key=lambda r: r["funding_time_ms"])
        for r in all_rates:
            yield r

    # ------------------------------------------------------------ WebSocket

    async def stream_tickers(
        self, symbol: str,
    ) -> AsyncGenerator[RawMarkPrice, None]:
        """Stream live mark price + funding rate via Bybit tickers WebSocket.

        Yields a full snapshot on each update (merges deltas into state).
        Reconnects automatically on disconnect.
        """
        sub_msg = json.dumps({"op": "subscribe", "args": [f"tickers.{symbol}"]})
        state: dict[str, Any] = {}
        while True:
            try:
                async with websockets.connect(_WS_PUBLIC) as ws:
                    await ws.send(sub_msg)
                    async for message in ws:
                        data = json.loads(message)
                        if data.get("topic") != f"tickers.{symbol}":
                            continue
                        payload = data.get("data", {})
                        # Snapshot has all fields; delta only has changed fields.
                        if data.get("type") == "snapshot":
                            state = dict(payload)
                        else:
                            state.update(payload)
                        if "markPrice" in state and "fundingRate" in state:
                            yield RawMarkPrice(
                                symbol=symbol,
                                mark_price=state["markPrice"],
                                index_price=state.get("indexPrice", state["markPrice"]),
                                last_funding_rate=state["fundingRate"],
                                next_funding_time_ms=int(state.get("nextFundingTime", 0)),
                                time_ms=int(data.get("ts", 0)),
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "stream_tickers %s disconnected: %s — reconnecting in 5s",
                    symbol, exc,
                )
                state = {}
                await asyncio.sleep(5)

    async def stream_klines(
        self,
        symbol: str,
        interval: KlineInterval,
    ) -> AsyncGenerator[RawKline, None]:
        """Stream live closed klines via Bybit WebSocket.

        For 8h: subscribes to 1h klines and aggregates into 8h bars,
        emitting once every 8 hours at settlement boundaries.
        """
        if interval is KlineInterval.H8:
            async for bar in self._stream_klines_8h(symbol):
                yield bar
            return

        bybit_iv = _BYBIT_INTERVALS.get(interval.value)
        if bybit_iv is None:
            raise ValueError(f"Unsupported interval for Bybit WS: {interval}")

        topic = f"kline.{bybit_iv}.{symbol}"
        sub_msg = json.dumps({"op": "subscribe", "args": [topic]})
        while True:
            try:
                async with websockets.connect(_WS_PUBLIC) as ws:
                    await ws.send(sub_msg)
                    async for message in ws:
                        data = json.loads(message)
                        if data.get("topic") != topic:
                            continue
                        for k in data.get("data", []):
                            if k.get("confirm"):
                                yield _parse_ws_kline(k)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "stream_klines %s %s disconnected: %s — reconnecting in 5s",
                    symbol, interval.value, exc,
                )
                await asyncio.sleep(5)

    async def _stream_klines_8h(
        self, symbol: str,
    ) -> AsyncGenerator[RawKline, None]:
        """Subscribe to 1h klines and aggregate into 8h bars live.

        Emits one 8h bar each time a 1h kline completes a settlement window
        (00, 08, 16 UTC).  Detection uses bucket arithmetic on ``open_time_ms``
        rather than parsing ``close_time_ms`` — Bybit's WebSocket kline
        ``end`` field is the *inclusive* end of the period (e.g. 07:59:59.999
        for the 07:00–08:00 1h kline), so ``close_dt.hour`` is always one
        less than the boundary hour and ``minute`` is always 59.  The
        previous ``close_dt.hour in (0, 8, 16) and close_dt.minute == 0``
        check therefore never matched, and 8h bars never emitted (observed
        2026-05-06: strategy ran 3 days with zero bars in the buffer).
        """
        buffer: list[RawKline] = []
        async for k in self.stream_klines(symbol, KlineInterval.H1):
            buffer.append(k)
            # A 1h kline closes an 8h settlement period when the next 1h
            # boundary (open + 1h) lands on a multiple of 8h.  Equivalently:
            # the kline that opened at 07:00, 15:00, or 23:00 UTC.
            if (k["open_time_ms"] + 3_600_000) % _8H_MS == 0:
                agg = _aggregate_to_8h(buffer)
                if agg:
                    yield agg[-1]
                buffer.clear()
            # Prevent unbounded growth if alignment drifts.
            if len(buffer) > 16:
                buffer = buffer[-8:]

    # ----------------------------------------------------------------- lifecycle

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BybitLinearConnector:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


# ------------------------------------------------------------------ parsers


def _parse_rest_kline(row: list[str]) -> RawKline:
    """Parse a Bybit V5 REST kline row.

    Bybit returns: [startTime, openPrice, highPrice, lowPrice, closePrice,
                    volume, turnover]
    """
    open_ms = int(row[0])
    return RawKline(
        open_time_ms=open_ms,
        open=row[1],
        high=row[2],
        low=row[3],
        close=row[4],
        volume=row[5],
        close_time_ms=open_ms,  # placeholder — corrected during aggregation
        quote_volume=row[6] if len(row) > 6 else "0",
        trade_count=0,
        taker_buy_volume="0",
        taker_buy_quote_volume="0",
    )


def _parse_ws_kline(k: dict[str, Any]) -> RawKline:
    """Parse a Bybit V5 WebSocket kline event."""
    open_ms = int(k["start"])
    close_ms = int(k["end"])
    return RawKline(
        open_time_ms=open_ms,
        open=k["open"],
        high=k["high"],
        low=k["low"],
        close=k["close"],
        volume=k["volume"],
        close_time_ms=close_ms,
        quote_volume=k.get("turnover", "0"),
        trade_count=0,
        taker_buy_volume="0",
        taker_buy_quote_volume="0",
    )


# ------------------------------------------------------------------ 8h aggregation


def _aggregate_to_8h(h1_bars: list[RawKline]) -> list[RawKline]:
    """Group 1h bars into 8h bars at settlement boundaries (00, 08, 16 UTC)."""
    if not h1_bars:
        return []

    buckets: dict[int, list[RawKline]] = {}
    for bar in h1_bars:
        # Round down to the nearest 8h boundary.
        bucket_ms = (bar["open_time_ms"] // _8H_MS) * _8H_MS
        buckets.setdefault(bucket_ms, []).append(bar)

    result: list[RawKline] = []
    for bucket_ms in sorted(buckets):
        group = buckets[bucket_ms]
        from decimal import Decimal
        result.append(RawKline(
            open_time_ms=bucket_ms,
            open=group[0]["open"],
            high=str(max(Decimal(b["high"]) for b in group)),
            low=str(min(Decimal(b["low"]) for b in group)),
            close=group[-1]["close"],
            volume=str(sum(Decimal(b["volume"]) for b in group)),
            close_time_ms=bucket_ms + _8H_MS - 1,
            quote_volume=str(sum(Decimal(b["quote_volume"]) for b in group)),
            trade_count=sum(b["trade_count"] for b in group),
            taker_buy_volume="0",
            taker_buy_quote_volume="0",
        ))
    return result
