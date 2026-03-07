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


class BarRecord(TypedDict):
    time: int  # unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float


# bars_store["{symbol}_{interval}"] → list of BarRecord, oldest first, capped at 500.
bars_store: dict[str, list[BarRecord]] = {}
_MAX_BARS = 500


def record_bar(data: dict, subject: str) -> None:
    # Subject format: futures.{SYMBOL}.bars.{interval}  e.g. futures.BTCUSDT.bars.1m
    parts = subject.split(".")
    if len(parts) < 4:
        return
    sym, interval = parts[1], parts[3]
    try:
        t = int(datetime.fromisoformat(str(data["timestamp"])).timestamp())
    except (KeyError, ValueError):
        return
    bar: BarRecord = {
        "time": t,
        "open": float(data.get("open", 0)),
        "high": float(data.get("high", 0)),
        "low": float(data.get("low", 0)),
        "close": float(data.get("close", 0)),
        "volume": float(data.get("volume", 0)),
    }
    key = f"{sym}_{interval}"
    history = bars_store.setdefault(key, [])
    # Replace last bar if same timestamp (live update of in-progress candle)
    if history and history[-1]["time"] == t:
        history[-1] = bar
    else:
        history.append(bar)
        if len(history) > _MAX_BARS:
            del history[: len(history) - _MAX_BARS]


class PositionRecord(TypedDict):
    symbol: str
    quantity: str  # signed: positive = long, negative = short
    avg_entry_price: str
    unrealized_pnl: str
    realized_pnl: str
    status: str  # "open" | "closed"
    closed_at: str | None  # ISO-8601, set when position transitions to closed


_CLOSED_RETENTION_SECONDS = 60 * 60 * 24  # 24 hours

# Open positions keyed by symbol.
positions: dict[str, PositionRecord] = {}
# Recently closed positions keyed by symbol, retained for _CLOSED_RETENTION_SECONDS.
closed_positions: dict[str, PositionRecord] = {}


def record_positions(snapshot: list[dict]) -> None:
    """Diff the broker snapshot against the current store.

    - New / updated symbols go into ``positions``.
    - Symbols that disappear from the snapshot are moved to ``closed_positions``
      and kept for up to 24 hours.
    - Expired closed positions are pruned on every call.
    """
    now = datetime.now(UTC)

    # Prune expired closed positions
    expired = [
        sym
        for sym, rec in closed_positions.items()
        if rec["closed_at"] is not None
        and (now - datetime.fromisoformat(rec["closed_at"])).total_seconds()
        > _CLOSED_RETENTION_SECONDS
    ]
    for sym in expired:
        del closed_positions[sym]

    incoming = set()
    for p in snapshot:
        sym = str(p.get("symbol", ""))
        if not sym:
            continue
        incoming.add(sym)
        positions[sym] = {
            "symbol": sym,
            "quantity": str(p.get("quantity", "0")),
            "avg_entry_price": str(p.get("avg_entry_price", "0")),
            "unrealized_pnl": str(p.get("unrealized_pnl", "0")),
            "realized_pnl": str(p.get("realized_pnl", "0")),
            "status": "open",
            "closed_at": None,
        }
        # If it was previously closed but re-opened, remove from closed store
        closed_positions.pop(sym, None)

    # Detect symbols that just disappeared → move to closed
    for sym in list(positions.keys()):
        if sym not in incoming:
            rec = positions.pop(sym)
            rec["status"] = "closed"
            rec["closed_at"] = now.isoformat()
            closed_positions[sym] = rec


class FillRecord(TypedDict):
    strategy_id: str
    symbol: str
    quantity: str  # signed: positive = bought, negative = sold
    fill_price: str
    filled_at: str  # ISO-8601


# Per-strategy fill history, capped at 500 entries each.
fills_by_strategy: dict[str, list[FillRecord]] = {}
_MAX_FILLS = 500


def record_fill(data: dict) -> None:
    sid = str(data.get("strategy_id", ""))
    if not sid:
        return
    record: FillRecord = {
        "strategy_id": sid,
        "symbol": str(data.get("symbol", "")),
        "quantity": str(data.get("quantity", "")),
        "fill_price": str(data.get("fill_price", "")),
        "filled_at": datetime.now(UTC).isoformat(),
    }
    history = fills_by_strategy.setdefault(sid, [])
    history.append(record)
    if len(history) > _MAX_FILLS:
        del history[: len(history) - _MAX_FILLS]


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
