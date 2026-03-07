"""In-memory store for dashboard state populated by the NATS bridge."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TypedDict


class OrderRecord(TypedDict):
    symbol: str
    side: str
    order_type: str
    quantity: str
    price: str
    reduce_only: bool
    placed_at: str  # ISO-8601


# Capped at 500 entries — newest appended last.
orders: list[OrderRecord] = []
_MAX_ORDERS = 500


registered_strategies: dict[str, dict] = {}


def register_strategy(data: dict) -> None:
    registered_strategies[data["name"]] = data


def unregister_strategy(name: str) -> None:
    registered_strategies.pop(name, None)


def record_order(data: dict[str, object]) -> None:
    record: OrderRecord = {
        "symbol": str(data.get("symbol", "")),
        "side": str(data.get("side", "")),
        "order_type": str(data.get("order_type", "")),
        "quantity": str(data.get("quantity", "")),
        "price": str(data.get("price", "")),
        "reduce_only": bool(data.get("reduce_only", False)),
        "placed_at": datetime.now(UTC).isoformat(),
    }
    orders.append(record)
    if len(orders) > _MAX_ORDERS:
        del orders[: len(orders) - _MAX_ORDERS]


class PnLRecord(TypedDict):
    strategy_id: str
    total_realized: str
    total_unrealized: str
    total: str
    timestamp: str  # ISO-8601


# Latest snapshot per strategy + capped history (newest last).
pnl_latest: dict[str, PnLRecord] = {}
pnl_history: dict[str, list[PnLRecord]] = {}
_MAX_PNL_HISTORY = 1000


def record_pnl(data: dict) -> None:
    sid = data["strategy_id"]
    record: PnLRecord = {
        "strategy_id": sid,
        "total_realized": data["total_realized"],
        "total_unrealized": data["total_unrealized"],
        "total": data["total"],
        "timestamp": data["timestamp"],
    }
    pnl_latest[sid] = record
    history = pnl_history.setdefault(sid, [])
    history.append(record)
    if len(history) > _MAX_PNL_HISTORY:
        del history[: len(history) - _MAX_PNL_HISTORY]
