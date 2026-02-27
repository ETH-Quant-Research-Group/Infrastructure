from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
from decimal import Decimal

import httpx
import websockets

from data.connectors.types import KlineInterval, RawKline, RawTrade

_KLINE_PAGE = 1_000  # Binance maximum klines per REST request


class BinanceConnector:
    """Async connector for the Binance REST and WebSocket APIs.

    Returns raw, Binance-shaped structs — normalization to canonical types
    is handled separately in ``data.normalizers.binance``.

    Usage (historical)::

        async with BinanceConnector() as conn:
            async for kline in conn.fetch_klines(
                "BTCUSDT", KlineInterval.H1, start_ms=..., end_ms=...
            ):
                ...

    Usage (live)::

        async with BinanceConnector() as conn:
            async for kline in conn.stream_klines("BTCUSDT", KlineInterval.M1):
                ...
    """

    _REST_BASE = "https://api.binance.com"
    _WS_BASE = "wss://stream.binance.com:9443/ws"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = 10.0,
    ) -> None:
        headers: dict[str, str] = {}
        if api_key:
            headers["X-MBX-APIKEY"] = api_key
        self._client = httpx.AsyncClient(
            base_url=self._REST_BASE,
            headers=headers,
            timeout=timeout,
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
        """Yield all closed klines in [start_ms, end_ms], auto-paginating.

        Binance caps responses at 1 000 klines per request; this method
        transparently issues as many requests as needed to cover the range.
        """
        cursor = start_ms
        while cursor < end_ms:
            response = await self._client.get(
                "/api/v3/klines",
                params={
                    "symbol": symbol,
                    "interval": interval.value,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": _KLINE_PAGE,
                },
            )
            response.raise_for_status()
            batch: list[list[Any]] = response.json()
            for row in batch:
                yield _parse_rest_kline(row)
            if len(batch) < _KLINE_PAGE:
                break
            # advance cursor past the close time of the last bar
            cursor = int(batch[-1][6]) + 1

    async def fetch_trades(
        self,
        symbol: str,
        *,
        limit: int = 1_000,
    ) -> AsyncGenerator[RawTrade, None]:
        """Yield recent trades for *symbol* (up to *limit*, max 1 000)."""
        response = await self._client.get(
            "/api/v3/trades",
            params={"symbol": symbol, "limit": limit},
        )
        response.raise_for_status()
        for row in response.json():
            yield _parse_rest_trade(row)

    # ------------------------------------------------------------ WebSocket

    async def stream_klines(
        self,
        symbol: str,
        interval: KlineInterval,
    ) -> AsyncGenerator[RawKline, None]:
        """Yield a :class:`RawKline` for each *closed* bar over WebSocket.

        Binance sends partial updates while a bar is forming; this method
        filters them out and only yields once the bar is marked closed.
        """
        url = f"{self._WS_BASE}/{symbol.lower()}@kline_{interval.value}"
        async with websockets.connect(url) as ws:  # type: ignore[attr-defined]
            async for message in ws:
                data: dict[str, Any] = json.loads(message)
                k = data["k"]
                if k["x"]:  # x == True → bar is closed
                    yield _parse_ws_kline(k)

    async def stream_trades(
        self,
        symbol: str,
    ) -> AsyncGenerator[RawTrade, None]:
        """Yield a :class:`RawTrade` for every executed trade over WebSocket."""
        url = f"{self._WS_BASE}/{symbol.lower()}@trade"
        async with websockets.connect(url) as ws:  # type: ignore[attr-defined]
            async for message in ws:
                data: dict[str, Any] = json.loads(message)
                yield _parse_ws_trade(data)

    # ----------------------------------------------------------------- lifecycle

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BinanceConnector:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


# ------------------------------------------------------------------ parsers


def _parse_rest_kline(row: list[Any]) -> RawKline:
    return RawKline(
        open_time_ms=int(row[0]),
        open=row[1],
        high=row[2],
        low=row[3],
        close=row[4],
        volume=row[5],
        close_time_ms=int(row[6]),
        quote_volume=row[7],
        trade_count=int(row[8]),
        taker_buy_volume=row[9],
        taker_buy_quote_volume=row[10],
    )


def _parse_ws_kline(k: dict[str, Any]) -> RawKline:
    return RawKline(
        open_time_ms=int(k["t"]),
        open=k["o"],
        high=k["h"],
        low=k["l"],
        close=k["c"],
        volume=k["v"],
        close_time_ms=int(k["T"]),
        quote_volume=k["q"],
        trade_count=int(k["n"]),
        taker_buy_volume=k["V"],
        taker_buy_quote_volume=k["Q"],
    )


def _parse_rest_trade(row: dict[str, Any]) -> RawTrade:
    return RawTrade(
        id=row["id"],
        price=row["price"],
        qty=row["qty"],
        quote_qty=row["quoteQty"],
        time=row["time"],
        is_buyer_maker=row["isBuyerMaker"],
    )


def _parse_ws_trade(data: dict[str, Any]) -> RawTrade:
    # WS trade stream omits quoteQty — compute it from price × qty
    quote_qty = str(Decimal(data["p"]) * Decimal(data["q"]))
    return RawTrade(
        id=data["t"],
        price=data["p"],
        qty=data["q"],
        quote_qty=quote_qty,
        time=data["T"],
        is_buyer_maker=data["m"],
    )
