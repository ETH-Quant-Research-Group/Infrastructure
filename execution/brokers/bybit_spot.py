"""Bybit spot broker — places spot market orders on Bybit's demo or live account.

Uses ``category="spot"`` for the Bybit V5 API.  The "position" for spot is
the coin balance in the unified wallet (e.g. querying "ETHUSDT" returns the
ETH balance).

Environment variables (shared with BybitBroker):
    BYBIT_API_KEY    — Bybit API key
    BYBIT_API_SECRET — Bybit API secret
    BYBIT_DEMO=1     — use demo endpoint (default: live)
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
    Position,
)
from interfaces.broker import BaseBroker

_LIVE_URL = "https://api.bybit.com"
_DEMO_URL = "https://api-demo.bybit.com"
_RECV_WINDOW = "5000"


class BybitSpotBroker(BaseBroker):
    """Broker for Bybit spot markets (category="spot").

    Spot "position" is the coin balance in the unified wallet.
    For example, ``position("ETHUSDT")`` returns the ETH balance.

    Set ``BYBIT_DEMO=1`` (or pass ``demo=True``) to trade on the demo account.
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

    @staticmethod
    def _bybit_side(side: OrderSide) -> str:
        return "Buy" if side is OrderSide.BUY else "Sell"

    @staticmethod
    def _bybit_order_type(order_type: OrderType) -> str:
        return "Market" if order_type is OrderType.MARKET else "Limit"

    @staticmethod
    def _base_coin(symbol: str) -> str:
        """Extract base coin from a unified symbol, e.g. 'ETHUSDT' → 'ETH'."""
        for quote in ("USDT", "USDC", "BTC", "ETH"):
            if symbol.endswith(quote):
                return symbol[: -len(quote)]
        return symbol

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
            "category": "spot",
            "symbol": order.symbol,
            "side": self._bybit_side(order.side),
            "orderType": self._bybit_order_type(order.order_type),
            "qty": str(order.quantity),
            "clientOrderId": client_oid,
        }
        if order.order_type is not OrderType.MARKET:
            body["price"] = str(order.price)
            body["timeInForce"] = "GTC"
        else:
            body["timeInForce"] = "IOC"
            body["marketUnit"] = "baseCoin"  # qty in base asset (e.g. LINK), not quote (USDT)

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
                {"category": "spot", "orderId": order_id},
            )
        except Exception as exc:
            return OrderResult(order_id=order_id, order=None, error=str(exc))

        if resp.get("retCode", -1) != 0:
            return OrderResult(order_id=order_id, order=None, error=resp.get("retMsg"))
        return OrderResult(order_id=order_id, order=None, error=None)

    async def cancel_all_orders(self, symbol: str | None = None) -> OrderResult:
        body: dict[str, Any] = {"category": "spot"}
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

    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        params: dict[str, Any] = {"category": "spot", "limit": "50"}
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

    async def position(self, symbol: str) -> Position | None:
        """Return the spot holding as a Position derived from the unified wallet.

        ``quantity`` is in base-asset units (e.g. ETH for "ETHUSDT"), positive = long.
        ``avg_entry_price`` is 0 — Bybit doesn't track avg entry for spot balances.
        Returns ``None`` if the balance is zero (treated as flat by the consolidator).
        """
        coin = self._base_coin(symbol)
        try:
            resp = await self._get(
                "/v5/account/wallet-balance", {"accountType": "UNIFIED"}
            )
        except Exception:
            return None
        if resp.get("retCode", -1) != 0:
            return None

        items = resp.get("result", {}).get("list", [])
        if not items:
            return None

        for c in items[0].get("coin", []):
            if c.get("coin") == coin:
                qty = Decimal(c.get("walletBalance", "0"))
                if qty <= Decimal(0):
                    return None
                return Position(
                    symbol=symbol,
                    quantity=qty,
                    avg_entry_price=Decimal(0),
                    unrealized_pnl=Decimal(0),
                    realized_pnl=Decimal(0),
                )
        return None

    async def list_positions(self) -> list[Position]:
        """Return all open spot holdings as Position objects.

        Scans every non-stablecoin coin in the Bybit Unified wallet and
        returns one Position per coin with a non-zero balance.  This lets
        the position publisher discover spot holdings without needing
        in-memory tracking (i.e. survives consolidator restarts).
        """
        try:
            resp = await self._get(
                "/v5/account/wallet-balance", {"accountType": "UNIFIED"}
            )
        except Exception:
            return []
        if resp.get("retCode", -1) != 0:
            return []
        items = resp.get("result", {}).get("list", [])
        if not items:
            return []
        positions: list[Position] = []
        for c in items[0].get("coin", []):
            coin = c.get("coin", "")
            if coin in ("USDT", "USDC"):
                continue
            qty = Decimal(c.get("walletBalance", "0") or "0")
            if qty <= Decimal(0):
                continue
            # Derive current price from usdValue / qty so the Assets page can
            # display a meaningful price and position value for spot holdings.
            usd_value = Decimal(str(c.get("usdValue", "0") or "0"))
            current_price = (usd_value / qty) if qty > Decimal(0) else Decimal(0)
            positions.append(
                Position(
                    symbol=f"{coin}USDT",
                    quantity=qty,
                    avg_entry_price=current_price,
                    unrealized_pnl=Decimal(0),
                    realized_pnl=Decimal(0),
                )
            )
        return positions

    async def wallet_balance(self) -> dict[str, Any]:
        """Return spot-leg wallet metrics for PnL tracking.

        Returns ``spotCoinEquity`` — the total USD value of all non-stablecoin
        coin holdings in the Bybit Unified account.  This lets the consolidator
        track the spot leg's equity independently from the perp leg
        (``BybitBroker.wallet_balance`` returns the full account equity which
        already includes spot coin values, so we must split them to avoid
        double-counting when summing across both brokers).
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
        if not items:
            return {}
        coins = items[0].get("coin", [])
        spot_usd = sum(
            float(c.get("usdValue", 0) or 0)
            for c in coins
            if c.get("coin") not in ("USDT", "USDC")
        )
        return {"spotCoinEquity": str(spot_usd)}

    # ------------------------------------------------------------------ lifecycle

    async def aclose(self) -> None:
        await self._client.aclose()
