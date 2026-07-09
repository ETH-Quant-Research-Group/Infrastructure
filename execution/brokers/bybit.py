"""Bybit broker — places real orders on Bybit's demo or live account.

Bybit's demo trading environment is functionally identical to live trading
but uses virtual funds.  Point this broker at demo by setting the
``BYBIT_DEMO=1`` environment variable (or passing ``demo=True``).

Required environment variables:
    BYBIT_API_KEY     — your Bybit API key
    BYBIT_API_SECRET  — your Bybit API secret

Optional:
    BYBIT_DEMO=1      — use demo trading endpoint (https://api-demo.bybit.com)
                        omit or set to 0 for live trading

Usage::

    broker = BybitBroker()          # reads creds from env
    await broker.place_order(order)
    pos = await broker.position("BTCUSDT")
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from decimal import Decimal
from typing import Any, cast

import httpx

from execution.types import (
    Order,
    OrderResult,
    OrderSide,
    OrderType,
    PerpOrder,
    PerpPosition,
)
from interfaces.broker import BaseBroker

_LIVE_URL = "https://api.bybit.com"
_DEMO_URL = "https://api-demo.bybit.com"

_RECV_WINDOW = "5000"


class BybitBroker(BaseBroker):
    """Broker that sends real orders to Bybit (demo or live).

    Authenticates every request with HMAC-SHA256 per the Bybit V5 API spec.
    All perpetual futures are routed to the ``linear`` category (USDT-margined).

    Set ``BYBIT_DEMO=1`` to trade on the demo account — same API, virtual funds.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        demo: bool = bool(os.getenv("BYBIT_DEMO")),
    ) -> None:
        self._api_key = api_key or os.environ["BYBIT_API_KEY"]
        self._api_secret = api_secret or os.environ["BYBIT_API_SECRET"]
        self._base_url = _DEMO_URL if demo else _LIVE_URL
        # PnL cache — updated on each position() call, keyed by symbol so that
        # querying multiple symbols does not overwrite each other.
        self._realized_pnl_cache: dict[str, Decimal] = {}  # PNL fix!!!
        self._unrealized_pnl_cache: dict[str, Decimal] = {}  # PNL fix!!!
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(10.0),
            headers={"Content-Type": "application/json"},
        )

    # ------------------------------------------------------------------ auth

    def _sign(self, timestamp: str, payload: str) -> str:
        raw = timestamp + self._api_key + _RECV_WINDOW + payload
        return hmac.new(
            self._api_secret.encode(), raw.encode(), hashlib.sha256
        ).hexdigest()

    def _auth_headers(self, timestamp: str, payload: str) -> dict[str, str]:
        return {
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-SIGN": self._sign(timestamp, payload),
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
        }

    @staticmethod
    def _ts() -> str:
        return str(int(time.time() * 1000))

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _bybit_side(side: OrderSide) -> str:
        return "Buy" if side is OrderSide.BUY else "Sell"

    @staticmethod
    def _bybit_order_type(order_type: OrderType) -> str:
        return "Market" if order_type is OrderType.MARKET else "Limit"

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(body, separators=(",", ":"))
        ts = self._ts()
        r = await self._client.post(
            path, content=payload, headers=self._auth_headers(ts, payload)
        )
        r.raise_for_status()
        return cast("dict[str, Any]", r.json())

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        sorted_params = dict(sorted(params.items()))
        query = "&".join(f"{k}={v}" for k, v in sorted_params.items())
        ts = self._ts()
        r = await self._client.get(
            path, params=sorted_params, headers=self._auth_headers(ts, query)
        )
        r.raise_for_status()
        return cast("dict[str, Any]", r.json())

    # ------------------------------------------------------------------ trading

    async def place_order(self, order: Order) -> OrderResult:
        client_oid = order.client_order_id or uuid.uuid4().hex[:16]
        body: dict[str, Any] = {
            "category": "linear",
            "symbol": order.symbol,
            "side": self._bybit_side(order.side),
            "orderType": self._bybit_order_type(order.order_type),
            "qty": str(order.quantity),
            "clientOrderId": client_oid,
        }
        if isinstance(order, PerpOrder) and order.reduce_only:
            body["reduceOnly"] = True
        if order.order_type is not OrderType.MARKET:
            body["price"] = str(order.price)
            body["timeInForce"] = "GTC"
        else:
            body["timeInForce"] = "IOC"

        try:
            resp = await self._post("/v5/order/create", body)
        except Exception as exc:
            return OrderResult(order_id=None, order=order, error=str(exc))

        ret_code = resp.get("retCode", -1)
        if ret_code != 0:
            return OrderResult(
                order_id=None, order=order, error=resp.get("retMsg", "unknown error")
            )

        order_id = resp["result"].get("orderId", client_oid)
        return OrderResult(order_id=order_id, order=order, error=None)

    async def cancel_order(self, order_id: str) -> OrderResult:
        try:
            resp = await self._post(
                "/v5/order/cancel",
                {
                    "category": "linear",
                    "orderId": order_id,
                },
            )
        except Exception as exc:
            return OrderResult(order_id=order_id, order=None, error=str(exc))

        if resp.get("retCode", -1) != 0:
            return OrderResult(order_id=order_id, order=None, error=resp.get("retMsg"))
        return OrderResult(order_id=order_id, order=None, error=None)

    async def cancel_all_orders(self, symbol: str | None = None) -> OrderResult:
        body: dict[str, Any] = {"category": "linear"}
        if symbol:
            body["symbol"] = symbol
        try:
            resp = await self._post("/v5/order/cancel-all", body)
        except Exception as exc:
            return OrderResult(order_id=None, order=None, error=str(exc))

        if resp.get("retCode", -1) != 0:
            return OrderResult(order_id=None, order=None, error=resp.get("retMsg"))
        return OrderResult(order_id=None, order=None, error=None)

    # ------------------------------------------------------------------ read-only

    async def recent_fills(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent filled orders from Bybit order history.

        Used on consolidator startup to replay today's fills into the dashboard
        so the Orders panel is populated even after a backend restart.
        Returns raw dicts with keys: symbol, side, qty, price, created_time.
        """
        try:
            resp = await self._get(
                "/v5/order/history",
                {"category": "linear", "limit": str(limit), "orderStatus": "Filled"},
            )
        except Exception:
            return []
        if resp.get("retCode", -1) != 0:
            return []
        result = []
        for o in resp.get("result", {}).get("list", []):
            # Convert Bybit ms timestamp to ISO date prefix for day filtering
            created_ms = int(o.get("createdTime", 0) or 0)
            from datetime import UTC, datetime
            created_iso = (
                datetime.fromtimestamp(created_ms / 1000, tz=UTC).date().isoformat()
                if created_ms else ""
            )
            result.append({
                "symbol": o.get("symbol", ""),
                "side": o.get("side", ""),
                "order_type": o.get("orderType", ""),
                "quantity": o.get("qty", "0"),
                "price": o.get("avgPrice") or o.get("price") or "0",
                "reduce_only": o.get("reduceOnly", False),
                "created_time": created_iso,
            })
        return result

    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        params: dict[str, Any] = {"category": "linear", "limit": "50"}
        if symbol:
            params["symbol"] = symbol
        try:
            resp = await self._get("/v5/order/realtime", params)
        except Exception:
            return []
        if resp.get("retCode", -1) != 0:
            return []
        orders = []
        for o in resp.get("result", {}).get("list", []):
            side = OrderSide.BUY if o["side"] == "Buy" else OrderSide.SELL
            ot = OrderType.MARKET if o["orderType"] == "Market" else OrderType.LIMIT
            orders.append(
                Order(
                    symbol=o["symbol"],
                    side=side,
                    order_type=ot,
                    quantity=Decimal(o["qty"]),
                    price=Decimal(o.get("price") or "0"),
                    client_order_id=o.get("clientOrderId"),
                )
            )
        return orders

    async def position(self, symbol: str) -> PerpPosition | None:
        try:
            resp = await self._get(
                "/v5/position/list",
                {
                    "category": "linear",
                    "symbol": symbol,
                },
            )
        except Exception:
            return None
        if resp.get("retCode", -1) != 0:
            return None

        items = resp.get("result", {}).get("list", [])
        # Net all entries (Bybit returns separate Buy/Sell rows in hedge mode).
        # Summing signed quantities gives the true net position so the
        # consolidator computes the correct broker_delta regardless of mode.
        net_qty = Decimal(0)
        total_realized = Decimal(0)
        total_unrealized = Decimal(0)
        mark = Decimal(0)
        avg_entry = Decimal(0)
        liq_price: str | None = None

        for item in items:
            qty = Decimal(item.get("size", "0"))
            if qty == Decimal(0):
                continue
            side_sign = Decimal(1) if item.get("side") == "Buy" else Decimal(-1)
            net_qty += qty * side_sign
            total_realized += Decimal(item.get("cumRealisedPnl", "0"))
            total_unrealized += Decimal(item.get("unrealisedPnl", "0"))
            mark = Decimal(item.get("markPrice", "0"))
            if not avg_entry:
                avg_entry = Decimal(item.get("avgPrice", "0"))
            if not liq_price:
                liq_price = item.get("liqPrice")

        # Update PnL cache per symbol so multi-symbol totals are correct
        self._realized_pnl_cache[symbol] = total_realized  # PNL fix!!!
        self._unrealized_pnl_cache[symbol] = total_unrealized  # PNL fix!!!

        if net_qty == Decimal(0):
            return None

        return PerpPosition(
            symbol=symbol,
            quantity=net_qty,
            avg_entry_price=avg_entry,
            unrealized_pnl=total_unrealized,
            realized_pnl=total_realized,
            mark_price=mark,
            liquidation_price=Decimal(liq_price) if liq_price else None,
            funding_paid=None,
        )

    async def list_positions(self) -> list[PerpPosition]:
        """Return all open linear positions from Bybit (no symbol filter).

        Nets Buy and Sell entries for the same symbol so callers always see
        a single signed quantity per symbol (mirrors one-way mode behaviour).
        """
        try:
            resp = await self._get(
                "/v5/position/list",
                {"category": "linear", "settleCoin": "USDT", "limit": "200"},
            )
        except Exception:
            return []
        if resp.get("retCode", -1) != 0:
            return []

        # Accumulate per symbol to net hedge-mode Buy/Sell entries.
        buckets: dict[str, dict[str, Any]] = {}
        for item in resp.get("result", {}).get("list", []):
            qty = Decimal(item.get("size", "0"))
            if qty == Decimal(0):
                continue
            sym = item.get("symbol", "")
            side_sign = Decimal(1) if item.get("side") == "Buy" else Decimal(-1)
            if sym not in buckets:
                buckets[sym] = {
                    "net_qty": Decimal(0),
                    "realized": Decimal(0),
                    "unrealized": Decimal(0),
                    "mark": Decimal(0),
                    "avg_entry": Decimal(0),
                    "liq_price": None,
                }
            b = buckets[sym]
            b["net_qty"] += qty * side_sign
            b["realized"] += Decimal(item.get("cumRealisedPnl", "0"))
            b["unrealized"] += Decimal(item.get("unrealisedPnl", "0"))
            b["mark"] = Decimal(item.get("markPrice", "0"))
            if not b["avg_entry"]:
                b["avg_entry"] = Decimal(item.get("avgPrice", "0"))
            if not b["liq_price"]:
                b["liq_price"] = item.get("liqPrice")

        positions = []
        for sym, b in buckets.items():
            if b["net_qty"] == Decimal(0):
                continue
            self._realized_pnl_cache[sym] = b["realized"]
            self._unrealized_pnl_cache[sym] = b["unrealized"]
            positions.append(PerpPosition(
                symbol=sym,
                quantity=b["net_qty"],
                avg_entry_price=b["avg_entry"],
                unrealized_pnl=b["unrealized"],
                realized_pnl=b["realized"],
                mark_price=b["mark"],
                liquidation_price=Decimal(b["liq_price"]) if b["liq_price"] else None,
                funding_paid=None,
            ))
        return positions

    # ------------------------------------------------------------------ account

    async def wallet_balance(self) -> dict[str, Any]:
        """Return the unified wallet balance from Bybit.

        Relevant fields in the returned dict:
            totalEquity         — total account value (AUM), including unrealized PnL
            totalWalletBalance  — deposited capital + realized PnL (excludes unrealized)
            totalAvailableBalance — free margin available to trade
            totalUnrealisedPnl  — sum of unrealized PnL across all positions
        """
        try:
            resp = await self._get(
                "/v5/account/wallet-balance", {"accountType": "UNIFIED"}
            )
        except Exception:
            return {}
        if resp.get("retCode", -1) != 0:
            return {}
        items = resp.get("result", {}).get("list", [])
        return items[0] if items else {}

    # ------------------------------------------------------------------ pnl

    @property
    def total_realized_pnl(self) -> Decimal:
        """Sum of cached realized PnL across all symbols queried via position()."""
        return sum(self._realized_pnl_cache.values(), Decimal(0))  # PNL fix!!!

    @property
    def total_unrealized_pnl(self) -> Decimal:
        """Sum of cached unrealized PnL across all symbols queried via position()."""
        return sum(self._unrealized_pnl_cache.values(), Decimal(0))  # PNL fix!!!

    # ------------------------------------------------------------------ lifecycle

    async def aclose(self) -> None:
        await self._client.aclose()
