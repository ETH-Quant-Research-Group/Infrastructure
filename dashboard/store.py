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

# ISO-8601 timestamp of the last futures.* message received from the feed server.
feed_server_last_seen: str | None = None


def record_feed_server_activity() -> None:
    global feed_server_last_seen
    feed_server_last_seen = datetime.now(UTC).isoformat()


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
    # Replace existing bar with same timestamp (handles both live updates and
    # historical seeds that arrive out of order or overlap with stored bars).
    for i, existing in enumerate(history):
        if existing["time"] == t:
            history[i] = bar
            return
    history.append(bar)
    if len(history) > _MAX_BARS:
        del history[: len(history) - _MAX_BARS]


class PositionRecord(TypedDict):
    symbol: str
    exchange: str
    quantity: str  # signed: positive = long, negative = short
    avg_entry_price: str
    unrealized_pnl: str
    realized_pnl: str
    status: str  # "open" | "closed"
    closed_at: str | None  # ISO-8601, set when position transitions to closed


_CLOSED_RETENTION_SECONDS = 60 * 60 * 24  # 24 hours

# Open positions keyed by "{exchange}_{symbol}".
positions: dict[str, PositionRecord] = {}
# Recently closed positions keyed by "{exchange}_{symbol}".
closed_positions: dict[str, PositionRecord] = {}

# Baseline realized PnL per position — captures first value on backend start
# so the dashboard always shows PnL relative to session start, not all-time.
_position_realized_baseline: dict[str, float] = {}


def record_positions(snapshot: list[dict]) -> None:
    """Diff the broker snapshot against the current store.

    - New / updated positions go into ``positions`` (keyed by exchange+symbol).
    - Positions that disappear from the snapshot are moved to ``closed_positions``.
    - Expired closed positions are pruned on every call.
    """
    now = datetime.now(UTC)

    # Prune expired closed positions
    expired = [
        k
        for k, rec in closed_positions.items()
        if rec["closed_at"] is not None
        and (now - datetime.fromisoformat(rec["closed_at"])).total_seconds()
        > _CLOSED_RETENTION_SECONDS
    ]
    for k in expired:
        del closed_positions[k]

    incoming = set()
    for p in snapshot:
        sym = str(p.get("symbol", ""))
        exchange = str(p.get("exchange", ""))
        if not sym:
            continue
        key = f"{exchange}_{sym}" if exchange else sym
        incoming.add(key)

        raw_realized = float(str(p.get("realized_pnl", "0") or "0"))
        # Capture baseline on first appearance so realized PnL starts at 0
        if key not in _position_realized_baseline:
            _position_realized_baseline[key] = raw_realized
        baselined_realized = raw_realized - _position_realized_baseline[key]

        positions[key] = {
            "symbol": sym,
            "exchange": exchange,
            "quantity": str(p.get("quantity", "0")),
            "avg_entry_price": str(p.get("avg_entry_price", "0")),
            "unrealized_pnl": str(p.get("unrealized_pnl", "0")),
            "realized_pnl": str(round(baselined_realized, 8)),
            "status": "open",
            "closed_at": None,
        }
        closed_positions.pop(key, None)

    # Detect positions that just disappeared → move to closed
    for key in list(positions.keys()):
        if key not in incoming:
            rec = positions.pop(key)
            rec["status"] = "closed"
            rec["closed_at"] = now.isoformat()
            closed_positions[key] = rec
            # Remove baseline so a fresh open of the same symbol gets a new baseline
            _position_realized_baseline.pop(key, None)


class FillRecord(TypedDict):
    strategy_id: str
    symbol: str
    quantity: str  # signed: positive = bought, negative = sold
    fill_price: str
    exchange: str
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
        "exchange": str(data.get("exchange", "")),
        "filled_at": datetime.now(UTC).isoformat(),
    }
    history = fills_by_strategy.setdefault(sid, [])
    history.append(record)
    if len(history) > _MAX_FILLS:
        del history[: len(history) - _MAX_FILLS]


class BrokerPnLRecord(TypedDict):
    total_realized: str
    total_unrealized: str
    total: str
    timestamp: str  # ISO-8601


broker_pnl_latest: BrokerPnLRecord | None = None
broker_pnl_history: list[BrokerPnLRecord] = []
_MAX_BROKER_PNL_HISTORY = 1000


# Per-exchange broker state, updated whenever broker.pnl.{exchange} arrives.
broker_exchange_states: dict[str, dict] = {}

def record_broker_exchange_pnl(data: dict) -> None:
    exchange = str(data.get("exchange", ""))
    if not exchange or exchange == "all":
        return

    # The consolidator already baselines each exchange to 0 at startup.
    # Pass values through directly — no second baseline needed here.
    total = float(data.get("total", "0") or "0")
    realized = float(data.get("total_realized", "0") or "0")
    unrealized = float(data.get("total_unrealized", "0") or "0")

    state: dict = {
        "exchange": exchange,
        "total": str(round(total, 8)),
        "total_realized": str(round(realized, 8)),
        "total_unrealized": str(round(unrealized, 8)),
        "last_seen": datetime.now(UTC).isoformat(),
    }
    if data.get("total_equity") is not None:
        state["total_equity"] = str(data["total_equity"])
    if data.get("total_wallet_balance") is not None:
        state["total_wallet_balance"] = str(data["total_wallet_balance"])
    if data.get("available_balance") is not None:
        state["available_balance"] = str(data["available_balance"])
    broker_exchange_states[exchange] = state


def record_broker_pnl(data: dict) -> None:
    global broker_pnl_latest
    # The consolidator already baselines the aggregate to 0 at startup.
    # Pass values through directly — no second baseline needed here.
    realized = float(data.get("total_realized", "0") or "0")
    unrealized = float(data.get("total_unrealized", "0") or "0")
    total = float(data.get("total", "0") or "0")

    record: BrokerPnLRecord = {
        "total_realized": str(round(realized, 8)),
        "total_unrealized": str(round(unrealized, 8)),
        "total": str(round(total, 8)),
        "timestamp": str(data.get("timestamp", "")),
    }
    broker_pnl_latest = record
    broker_pnl_history.append(record)
    if len(broker_pnl_history) > _MAX_BROKER_PNL_HISTORY:
        del broker_pnl_history[: len(broker_pnl_history) - _MAX_BROKER_PNL_HISTORY]


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
