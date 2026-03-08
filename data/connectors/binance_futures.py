from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

import httpx
import websockets

from data.connectors.types import (
    KlineInterval,
    RawFundingRate,
    RawKline,
    RawMarkPrice,
    RawTrade,
)

_KLINE_PAGE = 1_000  # Binance max klines per request
_FUNDING_PAGE = 1_000  # Binance max funding records per request


class BinanceFuturesConnector:
    """Async connector for the Binance USD-M Futures REST and WebSocket APIs.

    Returns raw, Binance-shaped structs — normalization to canonical types
    is handled separately in ``data.normalizers.binance_futures``.

    The base URL targets USD-M perpetuals (``fapi.binance.com``).  All
    symbols must use the USDT-margined naming convention (e.g. ``BTCUSDT``).

    Usage (historical klines)::

        async with BinanceFuturesConnector() as conn:
            async for kline in conn.fetch_klines(
                "BTCUSDT", KlineInterval.H8, start_ms=..., end_ms=...
            ):
                ...

    Usage (funding rates)::

        async with BinanceFuturesConnector() as conn:
            async for rate in conn.fetch_funding_rates(
                "BTCUSDT", start_ms=...
            ):
                ...
    """

    _REST_BASE = "https://fapi.binance.com"
    _WS_BASE = "wss://fstream.binance.com/ws"

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
        """Yield all closed futures klines in [*start_ms*, *end_ms*].

        Auto-paginates in batches of 1 000 — Binance's per-request cap.
        The kline payload is structurally identical to spot, so the same
        :class:`~data.connectors.types.RawKline` TypedDict is reused.
        """
        cursor = start_ms
        while cursor < end_ms:
            response = await self._client.get(
                "/fapi/v1/klines",
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

    async def fetch_funding_rates(
        self,
        symbol: str,
        *,
        start_ms: int,
        end_ms: int | None = None,
    ) -> AsyncGenerator[RawFundingRate, None]:
        """Yield historical funding rates for *symbol* from *start_ms*.

        Binance settles funding every 8 hours.  Each record represents one
        settlement.  The endpoint is paginated via ``startTime``; this method
        advances the cursor automatically until all records are returned or
        *end_ms* is reached.
        """
        cursor = start_ms
        while True:
            params: dict[str, Any] = {
                "symbol": symbol,
                "startTime": cursor,
                "limit": _FUNDING_PAGE,
            }
            if end_ms is not None:
                params["endTime"] = end_ms

            response = await self._client.get("/fapi/v1/fundingRate", params=params)
            response.raise_for_status()
            batch: list[dict[str, Any]] = response.json()

            for row in batch:
                yield _parse_funding_rate(row)

            if len(batch) < _FUNDING_PAGE:
                break
            # advance past the last record's settlement time
            cursor = int(batch[-1]["fundingTime"]) + 1

    async def fetch_current_funding_rate(self, symbol: str) -> RawMarkPrice:
        """Fetch the current mark price and most-recently-settled funding rate.

        Uses ``/fapi/v1/premiumIndex`` which returns the live mark price,
        index price, and the *next* scheduled funding time.
        """
        response = await self._client.get(
            "/fapi/v1/premiumIndex", params={"symbol": symbol}
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return _parse_mark_price(data)

    async def fetch_trades(
        self,
        symbol: str,
        *,
        limit: int = 1_000,
    ) -> AsyncGenerator[RawTrade, None]:
        """Yield recent futures trades for *symbol* (up to *limit*, max 1 000)."""
        response = await self._client.get(
            "/fapi/v1/trades",
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
        """Yield a :class:`RawKline` for each *closed* futures bar over WebSocket.

        Only emits once Binance marks the bar closed (``x == True``).
        """
        url = f"{self._WS_BASE}/{symbol.lower()}@kline_{interval.value}"
        async with websockets.connect(url) as ws:  # type: ignore[attr-defined]
            async for message in ws:
                data: dict[str, Any] = json.loads(message)
                k = data["k"]
                if k["x"]:  # x == True → bar is closed
                    yield _parse_ws_kline(k)

    async def stream_mark_price(
        self,
        symbol: str,
        *,
        update_speed: int = 3,
    ) -> AsyncGenerator[RawMarkPrice, None]:
        """Yield live mark-price and funding-rate updates over WebSocket.

        Binance pushes updates every *update_speed* seconds (1 or 3).
        Each message includes the current mark price, index price, live
        funding rate, and the timestamp of the next scheduled settlement.

        Args:
            symbol: e.g. ``"BTCUSDT"``
            update_speed: ``1`` for 1-second cadence, ``3`` (default) for
                the standard 3-second cadence.
        """
        suffix = "" if update_speed == 3 else "@1s"
        url = f"{self._WS_BASE}/{symbol.lower()}@markPrice{suffix}"
        async with websockets.connect(url) as ws:  # type: ignore[attr-defined]
            async for message in ws:
                data: dict[str, Any] = json.loads(message)
                yield _parse_ws_mark_price(data)

    async def stream_trades(
        self,
        symbol: str,
    ) -> AsyncGenerator[RawTrade, None]:
        """Yield a :class:`RawTrade` for every futures trade over WebSocket."""
        url = f"{self._WS_BASE}/{symbol.lower()}@trade"
        async with websockets.connect(url) as ws:  # type: ignore[attr-defined]
            async for message in ws:
                data: dict[str, Any] = json.loads(message)
                yield _parse_ws_trade(data)

    # ----------------------------------------------------------------- lifecycle

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BinanceFuturesConnector:
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


def _parse_funding_rate(row: dict[str, Any]) -> RawFundingRate:
    return RawFundingRate(
        symbol=row["symbol"],
        funding_time_ms=int(row["fundingTime"]),
        funding_rate=row["fundingRate"],
        mark_price=row.get("markPrice", "0"),
    )


def _parse_mark_price(data: dict[str, Any]) -> RawMarkPrice:
    return RawMarkPrice(
        symbol=data["symbol"],
        mark_price=data["markPrice"],
        index_price=data["indexPrice"],
        last_funding_rate=data["lastFundingRate"],
        next_funding_time_ms=int(data["nextFundingTime"]),
        time_ms=int(data["time"]),
    )


def _parse_ws_mark_price(data: dict[str, Any]) -> RawMarkPrice:
    # WebSocket mark-price event fields differ slightly from REST
    return RawMarkPrice(
        symbol=data["s"],
        mark_price=data["p"],
        index_price=data["i"],
        last_funding_rate=data["r"],
        next_funding_time_ms=int(data["T"]),
        time_ms=int(data["E"]),
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
    from decimal import Decimal

    quote_qty = str(Decimal(data["p"]) * Decimal(data["q"]))
    return RawTrade(
        id=data["t"],
        price=data["p"],
        qty=data["q"],
        quote_qty=quote_qty,
        time=data["T"],
        is_buyer_maker=data["m"],
    )
