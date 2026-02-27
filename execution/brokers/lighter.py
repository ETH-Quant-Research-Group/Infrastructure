from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING

import lighter
from lighter.signer_client import SignerClient

from execution.types import (
    Order,
    OrderResult,
    OrderSide,
    OrderType,
    PerpOrder,
    PerpPosition,
    TimeInForce,
)
from interfaces.broker import BaseBroker

if TYPE_CHECKING:
    from lighter.models.account_position import AccountPosition as _LighterPosition
    from lighter.models.order import Order as _LighterOrder


# Map canonical enums → Lighter integer constants
_ORDER_TYPE: dict[OrderType, int] = {
    OrderType.LIMIT: SignerClient.ORDER_TYPE_LIMIT,
    OrderType.MARKET: SignerClient.ORDER_TYPE_MARKET,
    OrderType.STOP_LOSS: SignerClient.ORDER_TYPE_STOP_LOSS,
    OrderType.STOP_LOSS_LIMIT: SignerClient.ORDER_TYPE_STOP_LOSS_LIMIT,
    OrderType.TAKE_PROFIT: SignerClient.ORDER_TYPE_TAKE_PROFIT,
    OrderType.TAKE_PROFIT_LIMIT: SignerClient.ORDER_TYPE_TAKE_PROFIT_LIMIT,
}

_TIF: dict[TimeInForce, int] = {
    TimeInForce.IOC: SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
    TimeInForce.GTT: SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
    TimeInForce.POST_ONLY: SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY,
}


class LighterBroker(BaseBroker):
    """Execution broker for the Lighter.xyz perpetuals exchange.

    Implements :class:`~interfaces.broker.BaseBroker` and accepts
    :class:`~execution.types.PerpOrder` objects.

    **Symbol resolution** — Lighter identifies markets by integer index.
    Pass a ``symbol_map`` to declare your symbols upfront::

        symbol_map = {"BTC-USDC": 0, "ETH-USDC": 1}

    **Price & quantity encoding** — Lighter uses integer fixed-point.
    ``price_decimals=2`` (default) means a :class:`~decimal.Decimal` price
    of ``2500.50`` maps to integer ``250050``.  ``base_scale=10**8``
    (default) maps ``0.1 ETH`` to ``10_000_000`` integer units.

    **Order IDs** — :meth:`place_order` returns the ZK rollup ``tx_hash``
    as ``order_id``.  :meth:`cancel_order` expects an ``order_id`` in
    ``"{market_index}:{order_index}"`` format, which is what
    :meth:`open_orders` populates.

    Usage::

        from execution.brokers.lighter import LighterBroker
        from execution.types import PerpOrder, OrderSide, OrderType
        from decimal import Decimal

        async with LighterBroker(
            account_index=123,
            api_private_keys={0: "0xdeadbeef..."},
            symbol_map={"ETH-USDC": 1},
        ) as broker:
            order = PerpOrder(
                symbol="ETH-USDC",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=Decimal("0.1"),
                price=Decimal("2500.00"),
            )
            result = await broker.place_order(order)
            if result.error:
                print("failed:", result.error)
            else:
                print("tx_hash:", result.order_id)
    """

    URL_MAINNET = "https://mainnet.zklighter.elliptic.co"
    URL_TESTNET = "https://testnet.zklighter.elliptic.co"

    def __init__(
        self,
        account_index: int,
        api_private_keys: dict[int, str],
        symbol_map: dict[str, int],
        *,
        url: str = URL_MAINNET,
        price_decimals: int = 2,
        base_scale: int = 10**8,
    ) -> None:
        self._signer = SignerClient(url, account_index, api_private_keys)
        self._order_api = lighter.OrderApi(self._signer.api_client)
        self._account_api = lighter.AccountApi(self._signer.api_client)
        self._account_index = account_index
        self._symbol_map = symbol_map
        self._index_map = {v: k for k, v in symbol_map.items()}  # reverse lookup
        self._price_scale = 10**price_decimals
        self._base_scale = base_scale

    # ----------------------------------------------------------------- trading

    async def place_order(self, order: Order) -> OrderResult:
        """Sign and submit a :class:`~execution.types.PerpOrder` to Lighter.

        Raises :exc:`TypeError` if *order* is not a
        :class:`~execution.types.PerpOrder`.
        """
        if not isinstance(order, PerpOrder):
            cls = type(order).__name__
            raise TypeError(
                f"LighterBroker.place_order requires a PerpOrder, got {cls}"
            )

        market_index = self._resolve(order.symbol)
        client_idx = int(order.client_order_id) if order.client_order_id else 0
        trigger = (
            _to_int(order.trigger_price, self._price_scale)
            if order.trigger_price is not None
            else SignerClient.NIL_TRIGGER_PRICE
        )

        _tx, resp, err = await self._signer.create_order(
            market_index,
            client_idx,
            _to_int(order.quantity, self._base_scale),
            _to_int(order.price, self._price_scale),
            order.side is OrderSide.SELL,
            _ORDER_TYPE[order.order_type],
            _TIF[order.time_in_force],
            order.reduce_only,
            trigger,
            order.order_expiry,
        )
        if err:
            return OrderResult(order_id=None, order=order, error=err)
        return OrderResult(
            order_id=resp.tx_hash if resp else None,
            order=order,
            error=None,
        )

    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an order by its ``"{market_index}:{order_index}"`` id.

        Obtain the correct *order_id* from :meth:`open_orders`.
        """
        try:
            market_str, index_str = order_id.split(":", 1)
            market_index, order_index = int(market_str), int(index_str)
        except ValueError:
            return OrderResult(
                order_id=None,
                order=None,
                error=(
                    f"Invalid order_id format {order_id!r}."
                    " Expected '<market>:<index>'."
                ),
            )

        _tx, resp, err = await self._signer.cancel_order(market_index, order_index)
        if err:
            return OrderResult(order_id=None, order=None, error=err)
        return OrderResult(
            order_id=resp.tx_hash if resp else None,
            order=None,
            error=None,
        )

    async def cancel_all_orders(self, symbol: str | None = None) -> OrderResult:
        """Cancel every open order for this account across all markets.

        Note: Lighter does not support per-symbol cancellation at the
        protocol level; *symbol* is accepted for interface compatibility
        but ignored — all markets are cancelled.
        """
        timestamp_ms = int(time.time() * 1_000)
        _tx, resp, err = await self._signer.cancel_all_orders(
            SignerClient.CANCEL_ALL_TIF_IMMEDIATE, timestamp_ms
        )
        if err:
            return OrderResult(order_id=None, order=None, error=err)
        return OrderResult(
            order_id=resp.tx_hash if resp else None,
            order=None,
            error=None,
        )

    # --------------------------------------------------------------- read-only

    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        """Return open orders, optionally filtered to *symbol*.

        If *symbol* is ``None``, all markets in ``symbol_map`` are queried.
        Each returned :class:`~execution.types.PerpOrder` carries
        ``client_order_id`` encoded as ``"{market_index}:{order_index}"``
        so it can be passed directly to :meth:`cancel_order`.
        """
        auth, _ = self._signer.create_auth_token_with_expiry()
        markets: list[tuple[str, int]] = (
            [(symbol, self._resolve(symbol))]
            if symbol is not None
            else list(self._symbol_map.items())
        )
        orders: list[Order] = []
        for sym, market_index in markets:
            resp = await self._order_api.account_active_orders(
                self._account_index, market_index, auth=auth
            )
            for raw in resp.orders:
                orders.append(
                    _to_perp_order(raw, sym, self._price_scale, self._base_scale)
                )
        return orders

    async def position(self, symbol: str) -> PerpPosition | None:
        """Return the current perp position for *symbol*, or ``None`` if flat."""
        market_index = self._resolve(symbol)
        resp = await self._account_api.account(
            by="index", value=str(self._account_index)
        )
        if not resp.accounts:
            return None
        for raw in resp.accounts[0].positions or []:
            if raw.market_id == market_index:
                return _to_perp_position(raw, symbol)
        return None

    # ------------------------------------------------------ market data helpers

    async def best_bid(self, symbol: str) -> Decimal:
        """Return the current best bid price for *symbol*."""
        ob = await self._order_api.order_book_orders(self._resolve(symbol), 1)
        return Decimal(ob.bids[0].price)

    async def best_ask(self, symbol: str) -> Decimal:
        """Return the current best ask price for *symbol*."""
        ob = await self._order_api.order_book_orders(self._resolve(symbol), 1)
        return Decimal(ob.asks[0].price)

    # ----------------------------------------------------------------- lifecycle

    async def aclose(self) -> None:
        await self._signer.api_client.close()

    # ----------------------------------------------------------------- helpers

    def _resolve(self, symbol: str) -> int:
        try:
            return self._symbol_map[symbol]
        except KeyError:
            raise ValueError(
                f"Unknown symbol {symbol!r}. Add it to symbol_map."
            ) from None


class LighterMainnetBroker(LighterBroker):
    """Lighter mainnet broker — URL is hardcoded; cannot be overridden."""

    def __init__(
        self,
        account_index: int,
        api_private_keys: dict[int, str],
        symbol_map: dict[str, int],
        *,
        price_decimals: int = 2,
        base_scale: int = 10**8,
    ) -> None:
        super().__init__(
            account_index,
            api_private_keys,
            symbol_map,
            url=LighterBroker.URL_MAINNET,
            price_decimals=price_decimals,
            base_scale=base_scale,
        )


class LighterTestnetBroker(LighterBroker):
    """Lighter testnet broker — URL is hardcoded; cannot be overridden."""

    def __init__(
        self,
        account_index: int,
        api_private_keys: dict[int, str],
        symbol_map: dict[str, int],
        *,
        price_decimals: int = 2,
        base_scale: int = 10**8,
    ) -> None:
        super().__init__(
            account_index,
            api_private_keys,
            symbol_map,
            url=LighterBroker.URL_TESTNET,
            price_decimals=price_decimals,
            base_scale=base_scale,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_int(value: Decimal, scale: int) -> int:
    """Scale a human-readable :class:`~decimal.Decimal` to a Lighter integer."""
    return int(value * scale)


_LIGHTER_TYPE_MAP: dict[str, OrderType] = {
    "limit": OrderType.LIMIT,
    "market": OrderType.MARKET,
    "stop-loss": OrderType.STOP_LOSS,
    "stop-loss-limit": OrderType.STOP_LOSS_LIMIT,
    "take-profit": OrderType.TAKE_PROFIT,
    "take-profit-limit": OrderType.TAKE_PROFIT_LIMIT,
}

_LIGHTER_TIF_MAP: dict[str, TimeInForce] = {
    "good-till-time": TimeInForce.GTT,
    "immediate-or-cancel": TimeInForce.IOC,
    "post-only": TimeInForce.POST_ONLY,
}


def _to_perp_order(
    raw: _LighterOrder,
    symbol: str,
    price_scale: int,
    base_scale: int,
) -> PerpOrder:
    """Convert a Lighter API order to a :class:`~execution.types.PerpOrder`."""
    trigger_raw = Decimal(raw.trigger_price)
    return PerpOrder(
        symbol=symbol,
        side=OrderSide.SELL if raw.is_ask else OrderSide.BUY,
        order_type=_LIGHTER_TYPE_MAP.get(raw.type, OrderType.LIMIT),
        quantity=Decimal(raw.initial_base_amount),
        price=Decimal(raw.price),
        time_in_force=_LIGHTER_TIF_MAP.get(raw.time_in_force, TimeInForce.GTT),
        client_order_id=f"{raw.market_index}:{raw.order_index}",
        reduce_only=raw.reduce_only,
        trigger_price=trigger_raw if trigger_raw else None,
        order_expiry=raw.order_expiry,
    )


def _to_perp_position(raw: _LighterPosition, symbol: str) -> PerpPosition:
    """Convert a Lighter API position to a :class:`~execution.types.PerpPosition`."""
    signed_qty = Decimal(raw.position) * raw.sign
    return PerpPosition(
        symbol=symbol,
        quantity=signed_qty,
        avg_entry_price=Decimal(raw.avg_entry_price),
        unrealized_pnl=Decimal(raw.unrealized_pnl),
        realized_pnl=Decimal(raw.realized_pnl),
        liquidation_price=Decimal(raw.liquidation_price)
        if raw.liquidation_price
        else None,
        funding_paid=Decimal(raw.total_funding_paid_out)
        if raw.total_funding_paid_out
        else None,
    )
