"""Helpers shared by the Bybit perp and spot brokers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from execution.types import (
    FundingSettlement,
    InstrumentRules,
    OrderSide,
    OrderState,
    OrderStatus,
)

# Bybit v5 orderStatus → OrderState.  Anything not listed (e.g. "Triggered",
# only used for conditional orders) maps to OPEN, so callers polling for a
# final state keep waiting rather than treating it as done.
_STATE: dict[str, OrderState] = {
    "New": OrderState.OPEN,
    "Untriggered": OrderState.OPEN,
    "PartiallyFilled": OrderState.PARTIALLY_FILLED,
    "Filled": OrderState.FILLED,
    "Cancelled": OrderState.CANCELLED,
    "PartiallyFilledCanceled": OrderState.CANCELLED,  # spot only
    "Deactivated": OrderState.CANCELLED,
    "Rejected": OrderState.REJECTED,
}


def _dec(raw: object) -> Decimal:
    return Decimal(str(raw)) if raw not in (None, "") else Decimal(0)


def parse_order_status(raw: dict[str, Any], fee_coin: str | None) -> OrderStatus:
    """Build an :class:`OrderStatus` from one ``/v5/order/realtime`` or
    ``/v5/order/history`` list item.

    Fees come from ``cumFeeDetail`` (coin → amount).  ``cumExecFee`` is
    deprecated for linear and spot and only used as a fallback, attributed
    to *fee_coin* — pass ``None`` when the fee coin can't be known (spot).
    """
    fees = {
        coin: _dec(amount)
        for coin, amount in (raw.get("cumFeeDetail") or {}).items()
        if _dec(amount) != 0
    }
    if not fees and fee_coin is not None and _dec(raw.get("cumExecFee")) != 0:
        fees = {fee_coin: _dec(raw.get("cumExecFee"))}

    avg_price = _dec(raw.get("avgPrice"))
    reject_reason = raw.get("rejectReason") or None

    return OrderStatus(
        order_id=raw.get("orderId", ""),
        client_order_id=raw.get("orderLinkId") or None,
        symbol=raw.get("symbol", ""),
        side=OrderSide.BUY if raw.get("side") == "Buy" else OrderSide.SELL,
        state=_STATE.get(raw.get("orderStatus", ""), OrderState.OPEN),
        quantity=_dec(raw.get("qty")),
        filled_qty=_dec(raw.get("cumExecQty")),
        leaves_qty=_dec(raw.get("leavesQty")),
        avg_price=avg_price if avg_price > 0 else None,
        filled_value=_dec(raw.get("cumExecValue")),
        fees=fees,
        reject_reason=None if reject_reason == "EC_NoError" else reject_reason,
        raw_status=raw.get("orderStatus", ""),
        updated_at=_time(raw.get("updatedTime")),
        created_at=_time(raw.get("createdTime")),
    )


def _time(raw_ms: object) -> datetime | None:
    ms = int(str(raw_ms or 0))
    return datetime.fromtimestamp(ms / 1000, tz=UTC) if ms else None


# Bybit refuses history ranges (orders, transaction log) longer than 7 days.
_HISTORY_WINDOW = timedelta(days=7) - timedelta(seconds=1)
_HISTORY_PAGE = 50  # Bybit's maximum
_HISTORY_MAX_PAGES = 1000

Getter = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def fetch_paged(
    get: Getter,
    path: str,
    params: dict[str, Any],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Every list item of a Bybit history endpoint between *start* and *end*.

    Walks the range in 7-day windows (Bybit's limit) and follows
    ``nextPageCursor`` within each.  Any API error raises, and so does a
    window with implausibly many pages — a partial history must never pass
    for a complete one.  Items on a window boundary may appear twice.
    """
    items: list[dict[str, Any]] = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + _HISTORY_WINDOW, end)
        cursor = ""
        for _ in range(_HISTORY_MAX_PAGES):
            page_params: dict[str, Any] = {
                **params,
                "startTime": str(int(window_start.timestamp() * 1000)),
                "endTime": str(int(window_end.timestamp() * 1000)),
                "limit": str(_HISTORY_PAGE),
            }
            if cursor:
                page_params["cursor"] = cursor
            resp = await get(path, page_params)
            if resp.get("retCode", -1) != 0:
                raise RuntimeError(
                    f"Bybit {path} failed: {resp.get('retMsg', 'unknown error')}"
                )
            result = resp.get("result", {})
            items += result.get("list", [])
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
        else:
            raise RuntimeError(
                f"Bybit {path}: more than {_HISTORY_MAX_PAGES} pages"
                f" between {window_start} and {window_end}"
            )
        window_start = window_end
    return items


async def fetch_order_history(
    get: Getter,
    category: str,
    fee_coin: str | None,
    start: datetime,
    end: datetime,
) -> list[OrderStatus]:
    """Every order created between *start* and *end*, any status."""
    params: dict[str, Any] = {"category": category}
    if category == "linear":
        params["settleCoin"] = "USDT"
    raw = await fetch_paged(get, "/v5/order/history", params, start, end)
    return [parse_order_status(o, fee_coin) for o in raw]


def parse_funding_settlement(raw: dict[str, Any]) -> FundingSettlement:
    """One ``/v5/account/transaction-log`` item of type ``SETTLEMENT``.

    ``change`` is the cash change (positive = received); ``size`` + ``side``
    the account's position at settlement.  Missing / empty fields are read
    defensively — the position is ``None`` if it isn't reported.
    """
    symbol = raw.get("symbol", "")
    settled_at = _time(raw.get("transactionTime")) or datetime.fromtimestamp(0, tz=UTC)
    size, side = raw.get("size"), raw.get("side")
    position: Decimal | None = None
    if size not in (None, ""):
        if side == "Buy":
            position = _dec(size)
        elif side == "Sell":
            position = -_dec(size)
        elif _dec(size) == 0:
            position = Decimal(0)
    return FundingSettlement(
        settlement_id=str(raw.get("id") or f"{symbol}-{raw.get('transactionTime')}"),
        symbol=symbol,
        settled_at=settled_at,
        amount=_dec(raw.get("change")),
        position=position,
    )


async def fetch_funding_settlements(
    get: Getter, start: datetime, end: datetime
) -> list[FundingSettlement]:
    """Every USDT-perp funding payment on the unified account in the range."""
    params = {
        "accountType": "UNIFIED",
        "category": "linear",
        "currency": "USDT",
        "type": "SETTLEMENT",
    }
    raw = await fetch_paged(get, "/v5/account/transaction-log", params, start, end)
    return [parse_funding_settlement(item) for item in raw]


def parse_instrument_rules(raw: dict[str, Any]) -> InstrumentRules | None:
    """One ``/v5/market/instruments-info`` item → its order-size rules.

    Linear: ``qtyStep``, ``minOrderQty``, ``maxMktOrderQty``,
    ``minNotionalValue``.  Spot: ``basePrecision`` (the step),
    ``minOrderQty``, ``maxOrderQty``, ``minOrderAmt``.  ``None`` if no step
    is reported at all.
    """
    lot = raw.get("lotSizeFilter") or {}
    step = lot.get("qtyStep") or lot.get("basePrecision")
    if not step:
        return None

    def opt(*keys: str) -> Decimal | None:
        for key in keys:
            if lot.get(key) not in (None, ""):
                return Decimal(str(lot[key]))
        return None

    return InstrumentRules(
        symbol=raw.get("symbol", ""),
        qty_step=Decimal(str(step)),
        min_qty=opt("minOrderQty"),
        max_market_qty=opt("maxMktOrderQty", "maxOrderQty"),
        min_notional=opt("minNotionalValue", "minOrderAmt"),
        trading=raw.get("status", "Trading") == "Trading",
    )


async def fetch_instrument_rules(get: Getter, category: str) -> list[InstrumentRules]:
    """Every symbol's order-size rules in *category* (``linear`` / ``spot``).
    Follows the page cursor; any API error raises."""
    rules: list[InstrumentRules] = []
    cursor = ""
    for _ in range(_HISTORY_MAX_PAGES):
        params: dict[str, Any] = {"category": category, "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        resp = await get("/v5/market/instruments-info", params)
        if resp.get("retCode", -1) != 0:
            raise RuntimeError(
                f"Bybit /v5/market/instruments-info failed: {resp.get('retMsg', 'unknown error')}"
            )
        result = resp.get("result", {})
        rules += [r for item in result.get("list", []) if (r := parse_instrument_rules(item))]
        cursor = result.get("nextPageCursor") or ""
        if not cursor:
            return rules
    raise RuntimeError("Bybit /v5/market/instruments-info: too many pages")


def order_lookup_params(
    category: str,
    order_id: str | None,
    client_order_id: str | None,
    symbol: str | None,
) -> dict[str, Any]:
    """Query params for looking up one order by ``orderId`` / ``orderLinkId``."""
    if not order_id and not client_order_id:
        raise ValueError("order_status() needs order_id or client_order_id")
    params: dict[str, Any] = {"category": category}
    if order_id:
        params["orderId"] = order_id
    if client_order_id:
        params["orderLinkId"] = client_order_id
    if symbol:
        params["symbol"] = symbol
    elif category == "linear":
        # Linear queries require one of symbol / baseCoin / settleCoin.
        params["settleCoin"] = "USDT"
    return params
