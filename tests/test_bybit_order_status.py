from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from execution.brokers.bybit import BybitBroker
from execution.brokers.bybit_spot import BybitSpotBroker
from execution.types import OrderSide, OrderState, OrderStatus


def _order(**overrides: Any) -> dict[str, Any]:
    """A filled linear market order, shaped like a /v5/order/realtime item."""
    raw: dict[str, Any] = {
        "orderId": "fd4300ae-7847-404e-b947-b46980a4d140",
        "orderLinkId": "3_1042",
        "symbol": "ETHUSDT",
        "side": "Sell",
        "orderStatus": "Filled",
        "qty": "0.10",
        "cumExecQty": "0.10",
        "leavesQty": "0",
        "avgPrice": "2510.5",
        "cumExecValue": "251.05",
        "cumExecFee": "0.13807750",
        "cumFeeDetail": {"USDT": "0.13807750"},
        "rejectReason": "EC_NoError",
        "updatedTime": "1684738540561",
    }
    raw.update(overrides)
    return raw


def _ok(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"retCode": 0, "retMsg": "OK", "result": {"list": items}}


def _mock(
    broker: BybitBroker | BybitSpotBroker,
    responses: dict[str, dict[str, Any]],
    seen: list[httpx.Request] | None = None,
) -> None:
    """Route the broker's HTTP calls to canned responses keyed by URL path."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=responses.get(request.url.path, _ok([])))

    broker._client = httpx.AsyncClient(
        base_url="https://api-demo.bybit.com", transport=httpx.MockTransport(handler)
    )


def _perp() -> BybitBroker:
    return BybitBroker(api_key="k", api_secret="s", demo=True)


def _spot() -> BybitSpotBroker:
    return BybitSpotBroker(api_key="k", api_secret="s", demo=True)


def test_filled_perp_order_is_parsed() -> None:
    broker = _perp()
    _mock(broker, {"/v5/order/realtime": _ok([_order()])})

    status = asyncio.run(broker.order_status(client_order_id="3_1042"))

    assert status == OrderStatus(
        order_id="fd4300ae-7847-404e-b947-b46980a4d140",
        client_order_id="3_1042",
        symbol="ETHUSDT",
        side=OrderSide.SELL,
        state=OrderState.FILLED,
        quantity=Decimal("0.10"),
        filled_qty=Decimal("0.10"),
        leaves_qty=Decimal("0"),
        avg_price=Decimal("2510.5"),
        filled_value=Decimal("251.05"),
        fees={"USDT": Decimal("0.13807750")},
        reject_reason=None,
        raw_status="Filled",
        updated_at=status.updated_at if status else None,
    )
    assert status is not None and status.state.is_final


def test_lookup_sends_order_link_id_and_settle_coin() -> None:
    broker = _perp()
    seen: list[httpx.Request] = []
    _mock(broker, {"/v5/order/realtime": _ok([_order()])}, seen)

    asyncio.run(broker.order_status(client_order_id="3_1042"))

    params = seen[0].url.params
    assert params["category"] == "linear"
    assert params["orderLinkId"] == "3_1042"
    assert params["settleCoin"] == "USDT"
    assert "X-BAPI-SIGN" in seen[0].headers


def test_partly_filled_ioc_is_cancelled_with_filled_qty() -> None:
    broker = _perp()
    _mock(
        broker,
        {
            "/v5/order/realtime": _ok(
                [_order(orderStatus="Cancelled", cumExecQty="0.04", leavesQty="0")]
            )
        },
    )

    status = asyncio.run(broker.order_status(order_id="abc"))

    assert status is not None
    assert status.state is OrderState.CANCELLED
    assert status.filled_qty == Decimal("0.04")


def test_rejected_order_has_reason_and_no_price() -> None:
    broker = _perp()
    _mock(
        broker,
        {
            "/v5/order/realtime": _ok(
                [
                    _order(
                        orderStatus="Rejected",
                        cumExecQty="0",
                        avgPrice="",
                        cumExecValue="0",
                        cumFeeDetail={},
                        cumExecFee="0",
                        rejectReason="EC_NoImmediateQtyToFill",
                    )
                ]
            )
        },
    )

    status = asyncio.run(broker.order_status(order_id="abc"))

    assert status is not None
    assert status.state is OrderState.REJECTED
    assert status.reject_reason == "EC_NoImmediateQtyToFill"
    assert status.avg_price is None
    assert status.fees == {}


def test_open_order_is_not_final() -> None:
    broker = _perp()
    _mock(broker, {"/v5/order/realtime": _ok([_order(orderStatus="New")])})

    status = asyncio.run(broker.order_status(order_id="abc"))

    assert status is not None
    assert status.state is OrderState.OPEN
    assert not status.state.is_final


def test_falls_back_to_order_history() -> None:
    broker = _perp()
    seen: list[httpx.Request] = []
    _mock(broker, {"/v5/order/history": _ok([_order()])}, seen)

    status = asyncio.run(broker.order_status(client_order_id="3_1042"))

    assert status is not None and status.state is OrderState.FILLED
    assert [r.url.path for r in seen] == ["/v5/order/realtime", "/v5/order/history"]


def test_unknown_order_returns_none() -> None:
    broker = _perp()
    _mock(broker, {})

    assert asyncio.run(broker.order_status(client_order_id="9_9")) is None


def test_api_error_raises_instead_of_returning_none() -> None:
    broker = _perp()
    _mock(broker, {"/v5/order/realtime": {"retCode": 10001, "retMsg": "bad params"}})

    with pytest.raises(RuntimeError, match="bad params"):
        asyncio.run(broker.order_status(order_id="abc"))


def test_needs_an_id() -> None:
    with pytest.raises(ValueError):
        asyncio.run(_perp().order_status())


@pytest.mark.parametrize(("make", "category"), [(_perp, "linear"), (_spot, "spot")])
def test_cancel_sends_symbol(make: Any, category: str) -> None:
    broker = make()
    seen: list[httpx.Request] = []
    _mock(broker, {"/v5/order/cancel": _ok([])}, seen)

    result = asyncio.run(broker.cancel_order("abc", symbol="ETHUSDT"))

    assert result.ok
    assert json.loads(seen[0].content) == {
        "category": category,
        "orderId": "abc",
        "symbol": "ETHUSDT",
    }


def test_cancel_reports_bybit_refusal() -> None:
    broker = _perp()
    _mock(
        broker,
        {"/v5/order/cancel": {"retCode": 110001, "retMsg": "order not exists or too late to cancel"}},
    )

    result = asyncio.run(broker.cancel_order("abc", symbol="ETHUSDT"))

    assert result.error == "order not exists or too late to cancel"


def test_order_history_walks_7_day_windows_and_pages() -> None:
    broker = _perp()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        cursor = request.url.params.get("cursor")
        tag = f"{len(seen)}"
        page: dict[str, Any] = {"list": [_order(orderLinkId=f"1_{tag}", orderId=tag)]}
        if not cursor and len(seen) == 1:
            page["nextPageCursor"] = "page2"  # first window has two pages
        return httpx.Response(200, json={"retCode": 0, "result": page})

    broker._client = httpx.AsyncClient(
        base_url="https://api-demo.bybit.com", transport=httpx.MockTransport(handler)
    )
    start = datetime(2026, 10, 1, tzinfo=UTC)
    orders = asyncio.run(broker.order_history(start, start + timedelta(days=10)))

    assert [o.client_order_id for o in orders] == ["1_1", "1_2", "1_3"]
    windows = [(int(r.url.params["startTime"]), int(r.url.params["endTime"])) for r in seen]
    assert windows[0] == windows[1]  # same window, second page
    assert seen[1].url.params["cursor"] == "page2"
    assert all(end - begin < 7 * 86_400_000 for begin, end in windows)
    assert windows[2][0] == windows[0][1]  # next window starts where the last ended
    assert windows[2][1] == int((start + timedelta(days=10)).timestamp() * 1000)
    assert all(r.url.params["settleCoin"] == "USDT" for r in seen)


def test_order_history_raises_instead_of_returning_a_partial_history() -> None:
    broker = _spot()
    _mock(broker, {"/v5/order/history": {"retCode": 10002, "retMsg": "too far back"}})
    start = datetime(2026, 10, 1, tzinfo=UTC)

    with pytest.raises(RuntimeError, match="too far back"):
        asyncio.run(broker.order_history(start, start + timedelta(days=1)))


def test_created_time_is_parsed() -> None:
    broker = _perp()
    _mock(broker, {"/v5/order/realtime": _ok([_order(createdTime="1684738540000")])})

    status = asyncio.run(broker.order_status(order_id="abc"))

    assert status is not None
    assert status.created_at == datetime.fromtimestamp(1684738540, tz=UTC)


def test_spot_buy_fee_in_base_coin() -> None:
    broker = _spot()
    seen: list[httpx.Request] = []
    _mock(
        broker,
        {
            "/v5/order/realtime": _ok(
                [
                    _order(
                        side="Buy",
                        orderStatus="Filled",
                        qty="1.001",
                        cumExecQty="1.001",
                        cumExecFee="",
                        cumFeeDetail={"ETH": "0.001001"},
                    )
                ]
            )
        },
        seen,
    )

    status = asyncio.run(broker.order_status(client_order_id="3_1043"))

    assert status is not None
    assert status.fees == {"ETH": Decimal("0.001001")}
    assert seen[0].url.params["category"] == "spot"
    assert "settleCoin" not in seen[0].url.params
