"""Consolidator worker — nets strategy signals and routes broker orders.

Run with::

    python -m workers.consolidator_worker

Environment variables:

- ``NATS_URL``          (default: ``nats://localhost:4222``)
- ``DEFAULT_EXCHANGE``  (default: ``bybit_paper``)  — exchange used when a
                        strategy signal carries no ``exchange`` field.

Available exchanges (add more in ``_build_brokers()``):

    bybit_paper   BybitPaperBroker  — paper fills at live Bybit bid/ask
    paper         PaperBroker       — fills at last bar close, no fees

Strategies select an exchange by setting ``exchange="lighter"`` (or any other
registered name) on the :class:`~interfaces.signals.TargetPosition` they emit.
Signals with no exchange default to ``DEFAULT_EXCHANGE``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import httpx
import nats
import nats.aio.client
import nats.aio.msg

from config import NATS_URL
from engine.data import codec
from engine.order.consolidator import OrderConsolidator
from engine.order.guard_registry import StrategyGuardRegistry
from execution.brokers.bybit import BybitBroker
from execution.brokers.bybit_spot import BybitSpotBroker
from execution.types import FillConfirmation, Order, OrderSide, OrderType, PerpOrder
from webapp.persistence import DashboardDB

if TYPE_CHECKING:
    from interfaces.broker import BaseBroker
    from interfaces.signals import TargetPosition

log = logging.getLogger(__name__)

_DEFAULT_EXCHANGE = os.getenv("DEFAULT_EXCHANGE", "bybit_demo").lower()

# Never published to the host — reachable only inside the compose network,
# same as webapp/api/deploy.py's use of this same URL.
_MANAGER_URL = "http://manager:9000"


def _resolve_default_max_loss() -> Decimal:
    raw = os.getenv("DEFAULT_MAX_LOSS", "1000").strip()
    try:
        return Decimal(raw)
    except InvalidOperation:
        log.warning("DEFAULT_MAX_LOSS=%r is not a valid number — using 1000", raw)
        return Decimal("1000")


# ---------------------------------------------------------------------------
# Instrument lot-size precision
#
# Bybit's order endpoints reject quantities with finer precision than the
# instrument's lot size.  Quantizing with ROUND_DOWN guarantees we never
# request more than we actually have on a Sell (which would 110030-error)
# and never over-order on a Buy (residual is reconciled on the next cycle).
#
# Sourced from Bybit's instruments-info endpoint as of 2026-05-02.  Add new
# symbols here when adding strategies.  ETHUSDT spot has 5-decimal precision
# which the previous implementation truncated to 3.
# ---------------------------------------------------------------------------

_LOT_SIZE: dict[str, dict[str, Decimal]] = {
    "bybit_demo": {
        "ETHUSDT": Decimal("0.01"),  # perp ETH
        "LINKUSDT": Decimal("1"),  # perp LINK (whole contracts)
    },
    "bybit_spot": {
        "ETHUSDT": Decimal("0.00001"),  # spot ETH
        "LINKUSDT": Decimal("0.001"),  # spot LINK
    },
}


def _quantize_to_lot(quantity: Decimal, exchange: str, symbol: str) -> Decimal:
    """Round ``quantity`` DOWN to the instrument's lot size.

    Falls back to a safe default per exchange family if the symbol is not
    in the cache (1 contract for perps, 0.001 base coin for spot).
    """
    lot = _LOT_SIZE.get(exchange, {}).get(symbol)
    if lot is None:
        lot = Decimal("0.001") if exchange.endswith("spot") else Decimal("1")
    return quantity.quantize(lot, rounding=ROUND_DOWN)


# ---------------------------------------------------------------------------
# Broker registry
#
# Add new brokers here.  Key = exchange name used in TargetPosition.exchange.
# ---------------------------------------------------------------------------


def _build_brokers() -> dict[str, BaseBroker]:
    """Instantiate every broker that should be active for this run."""
    brokers: dict[str, BaseBroker] = {
        "bybit_demo": BybitBroker(
            demo=True
        ),  # requires BYBIT_API_KEY + BYBIT_API_SECRET
        "bybit_spot": BybitSpotBroker(demo=True),
        # "bybit":      BybitBroker(demo=False),  # live trading — be careful
    }
    log.info(
        "Active brokers: %s  |  default: %s",
        list(brokers),
        _DEFAULT_EXCHANGE,
    )
    return brokers


# ---------------------------------------------------------------------------
# Order factory
#
# Returns the correct Order subtype for the target exchange.
# For cross-exchange strategies, inspect `symbol` or add an `exchange` arg
# and return EquityOrder / FXOrder as needed.
# ---------------------------------------------------------------------------


def _order_factory(
    symbol: str,
    side: OrderSide,
    quantity: Decimal,
    _price: Decimal,
    exchange: str = "",
) -> Order:
    # Quantize DOWN to the instrument's lot size.  ROUND_DOWN guarantees we
    # never order more than we have on a Sell (avoids 110030 insufficient
    # balance) and never over-buy on entry (residual is reconciled next cycle).
    quantity = _quantize_to_lot(quantity, exchange, symbol)
    return PerpOrder(
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        price=Decimal(0),
    )


# ---------------------------------------------------------------------------
# NATS coroutines
# ---------------------------------------------------------------------------


async def _bridge_targets(
    nc: nats.aio.client.Client,
    queue: asyncio.Queue[TargetPosition],
) -> None:
    async def _cb(msg: nats.aio.msg.Msg) -> None:
        queue.put_nowait(codec.decode_target(msg.data))

    await nc.subscribe("signals.targets.*", cb=_cb)
    await asyncio.get_running_loop().create_future()


async def _publish_placed_orders(
    nc: nats.aio.client.Client,
    placed_queue: asyncio.Queue[tuple[str, Order]],
) -> None:
    while True:
        exchange, order = await placed_queue.get()
        await nc.publish(
            f"orders.placed.{exchange}.{order.symbol}", codec.encode_order(order)
        )
        log.info(
            "placed order: %s %s %s @ %s",
            order.side,
            order.quantity,
            order.symbol,
            exchange,
        )


async def _publish_state(
    nc: nats.aio.client.Client,
    consolidator: OrderConsolidator,
    interval: float = 5.0,
) -> None:
    """Single source of truth for positions + broker PnL.

    Fetches ``list_positions()`` ONCE per cycle for each broker, then derives
    both ``positions.snapshot`` and ``broker.pnl[.exchange]`` from the same
    data.  This guarantees:

      sum(positions.snapshot[s].unrealized_pnl) == broker.pnl.total_unrealized

    so the per-symbol display on the strategy page always matches the
    fund-level PnL exactly (no timing drift between independent loops).

    Delta-neutral accounting (perp short + spot long hedge):

      perp realized   = cumRealisedPnl delta since persisted baseline
      perp unrealized = sum(pos.unrealized_pnl) from perp positions
      spot realized   = 0
      spot unrealized = sum((current_price - perp_entry) * spot_qty)
                        STATELESS — buying spot does NOT create PnL.

    Baseline persistence:
      The realized-PnL baseline is loaded from SQLite (``kv`` table, key
      ``baseline_realized:{exchange}``) at startup and only written once
      per exchange — at the very first start.  This means REALIZED on the
      dashboard reflects true since-strategy-inception cumulative PnL and
      never resets when this worker restarts.  To manually adjust the
      baseline (e.g. seed a known historical loss), update the SQLite kv
      row directly.
    """
    # Load (or initialise) per-exchange baselines from SQLite once.  The DB
    # connection lives only for the lifetime of this coroutine; we reopen
    # to write each new baseline so concurrent dashboard reads stay valid.
    db = DashboardDB()
    _baselines: dict[str, Decimal] = {}
    brokers = consolidator.brokers

    while True:
        await asyncio.sleep(interval)
        ts = datetime.now(UTC).isoformat()

        # ---- Step 1: fetch positions once per broker ----
        positions_by_broker: dict[str, list[Any]] = {}
        wallets: dict[str, dict[str, Any]] = {}
        for exchange, broker in brokers.items():
            try:
                if hasattr(broker, "list_positions"):
                    positions_by_broker[exchange] = await broker.list_positions()
                else:
                    symbols = consolidator.tracked_symbols_for(exchange)
                    positions_by_broker[exchange] = [
                        p
                        for s in symbols
                        if (p := await broker.position(s)) is not None
                    ]
            except Exception as exc:
                log.warning("list_positions failed for %s: %s", exchange, exc)
                positions_by_broker[exchange] = []

            if hasattr(broker, "wallet_balance"):
                try:
                    wallets[exchange] = await broker.wallet_balance() or {}
                except Exception as exc:
                    log.warning("wallet_balance failed for %s: %s", exchange, exc)
                    wallets[exchange] = {}

        # ---- Step 2: build positions.snapshot list ----
        snapshot: list[dict[str, Any]] = []
        for exchange, positions in positions_by_broker.items():
            for pos in positions:
                snapshot.append(
                    {
                        "symbol": pos.symbol,
                        "exchange": exchange,
                        "quantity": str(pos.quantity),
                        "avg_entry_price": str(pos.avg_entry_price),
                        "unrealized_pnl": str(pos.unrealized_pnl),
                        "realized_pnl": str(pos.realized_pnl),
                    }
                )

        # ---- Step 3: override spot unrealized using perp entry prices ----
        # Bybit spot API has no native uPnL — derive from perp entry as reference.
        perp_entries: dict[str, Decimal] = {}
        for p in snapshot:
            if p["exchange"] == "bybit_demo":
                entry = Decimal(p["avg_entry_price"])
                if entry > Decimal(0):
                    perp_entries[p["symbol"]] = entry

        for p in snapshot:
            if p["exchange"] == "bybit_spot" and p["symbol"] in perp_entries:
                spot_qty = Decimal(p["quantity"])
                current_price = Decimal(p["avg_entry_price"])
                perp_entry = perp_entries[p["symbol"]]
                p["unrealized_pnl"] = str((current_price - perp_entry) * spot_qty)

        # ---- Step 4: publish positions.snapshot ----
        await nc.publish("positions.snapshot", json.dumps(snapshot).encode())
        log.debug("published positions snapshot: %d positions", len(snapshot))

        # ---- Step 5: derive broker PnL from the SAME snapshot ----
        # Crucial: by reading from `snapshot` (not re-querying), we guarantee
        # broker.pnl.total_unrealized == sum(positions.snapshot.uPnL).
        total_realized = Decimal(0)
        total_unrealized = Decimal(0)

        for exchange, broker in brokers.items():
            payload: dict[str, Any] | None = None
            wb = wallets.get(exchange, {})
            broker_positions = [p for p in snapshot if p["exchange"] == exchange]

            if "spotCoinEquity" in wb:
                # ---- Spot leg ----
                equity = Decimal(str(wb.get("spotCoinEquity", "0") or "0"))
                spot_unrealized = sum(
                    (Decimal(p["unrealized_pnl"]) for p in broker_positions),
                    Decimal(0),
                )
                total_unrealized += spot_unrealized
                payload = {
                    "exchange": exchange,
                    "total_realized": "0",
                    "total_unrealized": str(spot_unrealized),
                    "total": str(spot_unrealized),
                    "total_equity": str(equity),
                    "timestamp": ts,
                }

            elif wb:
                # ---- Perp leg ----
                total_equity = Decimal(str(wb.get("totalEquity", "0") or "0"))
                wallet = Decimal(str(wb.get("totalWalletBalance", "0") or "0"))
                available = str(wb.get("totalAvailableBalance", "") or "")
                coins = wb.get("coin", [])

                # Use uPnL values from the snapshot — same numbers shown on website.
                pnl_unrealized = sum(
                    (Decimal(p["unrealized_pnl"]) for p in broker_positions),
                    Decimal(0),
                )
                # Lifetime cumulative realized PnL: read from the wallet's
                # USDT cumRealisedPnl field, NOT from the sum of currently-open
                # positions' realized_pnl.  The latter goes to 0 the moment a
                # position closes (the position vanishes from list_positions),
                # which after a baseline shift produces phantom +REALIZED jumps
                # equal to abs(baseline) every time we exit.
                # cumRealisedPnl in the wallet response includes every closed
                # trade since account inception and never resets.
                cum_realized = Decimal(0)
                for c in coins if isinstance(coins, list) else []:
                    if c.get("coin") == "USDT":
                        cum_realized = Decimal(str(c.get("cumRealisedPnl", "0") or "0"))
                        break
                realized_key = f"{exchange}_cum_realized"
                if realized_key not in _baselines:
                    # First touch this process lifetime: try SQLite first; if
                    # nothing persisted yet, this is the truly-first start and
                    # we capture today's cum_realized as the inception
                    # baseline.  After that, never write again — the same
                    # baseline persists across all future restarts.
                    db_key = f"baseline_realized:{exchange}"
                    persisted = db.get_kv(db_key)
                    if persisted is not None:
                        _baselines[realized_key] = Decimal(persisted)
                        log.info(
                            "Loaded persisted baseline for %s: %s "
                            "(since-inception REALIZED preserved)",
                            exchange,
                            persisted,
                        )
                    else:
                        _baselines[realized_key] = cum_realized
                        db.set_kv(db_key, str(cum_realized))
                        log.info(
                            "Set initial baseline for %s: %s (first start ever)",
                            exchange,
                            cum_realized,
                        )
                pnl_realized = cum_realized - _baselines[realized_key]
                pnl_total = pnl_realized + pnl_unrealized

                # AUM = totalEquity minus spot-coin values (tracked separately)
                spot_usd = Decimal(
                    str(
                        sum(
                            float(c.get("usdValue", 0) or 0)
                            for c in (coins if isinstance(coins, list) else [])
                            if c.get("coin") not in ("USDT", "USDC")
                        )
                    )
                )
                perp_equity = total_equity - spot_usd

                total_realized += pnl_realized
                total_unrealized += pnl_unrealized

                payload = {
                    "exchange": exchange,
                    "total_realized": str(pnl_realized),
                    "total_unrealized": str(pnl_unrealized),
                    "total": str(pnl_total),
                    "total_equity": str(perp_equity),
                    "total_wallet_balance": str(wallet),
                    "available_balance": available,
                    "timestamp": ts,
                }

            elif hasattr(broker, "total_realized_pnl") and hasattr(
                broker, "total_unrealized_pnl"
            ):
                # Fallback for paper broker
                realized = broker.total_realized_pnl
                unrealized = broker.total_unrealized_pnl
                total_realized += realized
                total_unrealized += unrealized
                payload = {
                    "exchange": exchange,
                    "total_realized": str(realized),
                    "total_unrealized": str(unrealized),
                    "total": str(realized + unrealized),
                    "timestamp": ts,
                }

            if payload is not None:
                await nc.publish(f"broker.pnl.{exchange}", json.dumps(payload).encode())

        # Aggregate across all brokers
        await nc.publish(
            "broker.pnl",
            json.dumps(
                {
                    "exchange": "all",
                    "total_realized": str(total_realized),
                    "total_unrealized": str(total_unrealized),
                    "total": str(total_realized + total_unrealized),
                    "timestamp": ts,
                }
            ).encode(),
        )


async def _sync_guard_config(
    guard_registry: StrategyGuardRegistry,
    interval: float = 10.0,
) -> None:
    """Poll the manager for admin-configured max_loss per deployed strategy,
    and hard-stop any container whose guard has just tripped.

    Polling the manager (rather than trusting anything published by a
    strategy's own container) is deliberate: the manager is the only source
    allowed to say what max_loss should be — it reflects exactly what the
    admin set on the /internal Deploy page. A strategy image cannot grant
    itself a bigger loss allowance than the fund's default this way.
    """
    async with httpx.AsyncClient(base_url=_MANAGER_URL, timeout=10) as client:
        while True:
            await asyncio.sleep(interval)

            deployed_names: set[str] = set()
            try:
                resp = await client.get("/deployed")
                resp.raise_for_status()
                for record in resp.json().get("deployed", []):
                    deployed_names.add(record["name"])
                    raw = (record.get("env") or {}).get("MAX_DRAWDOWN", "").strip()
                    max_loss = guard_registry.default_max_loss
                    if raw:
                        try:
                            max_loss = Decimal(raw)
                        except InvalidOperation:
                            log.warning(
                                "MAX_DRAWDOWN=%r for %r is not a valid number — "
                                "using default %s",
                                raw,
                                record["name"],
                                max_loss,
                            )
                    # Always configure (even at the default) so a strategy is
                    # known to the registry — and shows up in the Network
                    # page's guard status — from the moment it's deployed,
                    # not only after its first fill.
                    guard_registry.configure(record["name"], max_loss)
            except httpx.HTTPError as exc:
                log.warning("could not sync guard config from manager: %s", exc)

            for strategy_id in guard_registry.pop_newly_tripped():
                if strategy_id not in deployed_names:
                    # Not something manager deployed (e.g. a compose-managed
                    # strategy-* service) — the financial halt already
                    # applied in the consolidator; there's just no container
                    # for manager to stop here.
                    log.warning(
                        "guard TRIPPED for strategy_id=%r (not manager-deployed "
                        "— signals are halted but its container keeps running)",
                        strategy_id,
                    )
                    continue
                try:
                    resp = await client.post(f"/stop/{strategy_id}")
                    if resp.status_code >= 400:
                        log.error(
                            "manager refused to stop %r: %s", strategy_id, resp.text
                        )
                    else:
                        log.warning(
                            "guard trip: stopped container for strategy_id=%r",
                            strategy_id,
                        )
                except httpx.HTTPError as exc:
                    log.error(
                        "could not stop tripped strategy %r via manager: %s",
                        strategy_id,
                        exc,
                    )


async def _publish_guard_status(
    nc: nats.aio.client.Client,
    guard_registry: StrategyGuardRegistry,
    interval: float = 5.0,
) -> None:
    """Broadcast ``strategy.guard.<id>`` for every strategy currently not
    halted. The Network page's ACTIVE/HALTED badge (webapp/frontend/src/
    subpage/Network.jsx) treats recent presence of this subject as the real
    guard signal — unlike ``strategy.heartbeat.<id>``, which only proves the
    container is alive, not that its signals are actually being acted on. A
    halted strategy simply stops appearing here, so the badge goes stale and
    flips red within one TTL window, whether or not the container itself
    is still running (e.g. a halted compose-managed strategy that manager
    can't stop).
    """
    while True:
        await asyncio.sleep(interval)
        for strategy_id in guard_registry.active_ids():
            await nc.publish(f"strategy.guard.{strategy_id}", b"")


async def _publish_fills(
    nc: nats.aio.client.Client,
    fills_queue: asyncio.Queue[FillConfirmation],
) -> None:
    while True:
        fill = await fills_queue.get()
        await nc.publish(f"fills.{fill.strategy_id}", codec.encode_fill(fill))
        log.info(
            "fill: %s qty=%s price=%s → %s",
            fill.symbol,
            fill.quantity,
            fill.fill_price,
            fill.strategy_id,
        )


async def _replay_recent_orders(
    nc: nats.aio.client.Client,
    brokers: dict[str, Any],
    delay: float = 3.0,
) -> None:
    """Publish recent Bybit fill history to NATS shortly after startup.

    This re-populates the dashboard's Orders panel after a backend restart
    without requiring the backend to have direct Bybit credentials.
    Only replays fills from the current UTC day to avoid flooding with history.
    """
    await asyncio.sleep(delay)
    from datetime import UTC, datetime

    today = datetime.now(UTC).date().isoformat()
    for exchange, broker in brokers.items():
        if not hasattr(broker, "recent_fills"):
            continue
        try:
            fills = await broker.recent_fills(limit=100)
        except Exception as exc:
            log.warning("Could not replay recent fills for %s: %s", exchange, exc)
            continue
        published = 0
        for f in fills:
            # Only replay today's orders — skip older history
            if f.get("created_time", "") != today:
                continue
            payload = json.dumps(
                {
                    "symbol": f.get("symbol", ""),
                    "side": f.get("side", ""),
                    "order_type": f.get("order_type", ""),
                    "quantity": f.get("quantity", "0"),
                    "price": f.get("price", "0"),
                    "reduce_only": f.get("reduce_only", False),
                    "exchange": exchange,
                }
            ).encode()
            symbol = f.get("symbol", "unknown")
            await nc.publish(f"orders.placed.{exchange}.{symbol}", payload)
            published += 1
        if published:
            log.info("Replayed %d recent orders for %s", published, exchange)


async def _serve_replay_requests(
    nc: nats.aio.client.Client,
    brokers: dict[str, Any],
) -> None:
    """Listen for on-demand replay requests from the dashboard after restarts."""

    async def _on_request(msg: nats.aio.msg.Msg) -> None:
        await _replay_recent_orders(nc, brokers, delay=0.0)

    await nc.subscribe("control.replay.orders", cb=_on_request)
    await asyncio.get_running_loop().create_future()


async def _feed_market_prices(
    nc: nats.aio.client.Client,
    brokers: dict[str, BaseBroker],
) -> None:
    """Push bar/funding prices to every broker that accepts update_market_price()."""

    def _update_all(symbol: str, price: Decimal) -> None:
        for broker in brokers.values():
            if hasattr(broker, "update_market_price"):
                broker.update_market_price(symbol, price)

    async def _on_bar(msg: nats.aio.msg.Msg) -> None:
        try:
            event = codec.decode(msg.data)
            _update_all(event.symbol, event.close)  # type: ignore[union-attr]
        except Exception as exc:
            log.debug("_on_bar decode error: %s", exc)

    async def _on_funding(msg: nats.aio.msg.Msg) -> None:
        try:
            event = codec.decode(msg.data)
            _update_all(event.symbol, event.mark_price)  # type: ignore[union-attr]
        except Exception as exc:
            log.debug("_on_funding decode error: %s", exc)

    await nc.subscribe("futures.*.bars.*", cb=_on_bar)
    await nc.subscribe("futures.*.funding_rate", cb=_on_funding)
    await asyncio.get_running_loop().create_future()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    brokers = _build_brokers()
    target_queue: asyncio.Queue[TargetPosition] = asyncio.Queue()
    placed_queue: asyncio.Queue[tuple[str, Order]] = asyncio.Queue()
    fills_queue: asyncio.Queue[FillConfirmation] = asyncio.Queue()

    guard_registry = StrategyGuardRegistry(default_max_loss=_resolve_default_max_loss())
    consolidator = OrderConsolidator(
        target_queue=target_queue,
        brokers=brokers,
        default_exchange=_DEFAULT_EXCHANGE,
        min_order_size=Decimal("1"),  # ignore sub-1-unit residuals from rounding
        placed_queue=placed_queue,
        fills_queue=fills_queue,
        order_factory=_order_factory,
        guard_registry=guard_registry,
    )

    nc = await nats.connect(NATS_URL)
    try:
        # Seed consolidator with live broker positions so that restarts while
        # positions are open do not produce spurious orders.
        await consolidator.seed_from_broker()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_bridge_targets(nc, target_queue))
            tg.create_task(consolidator.run())
            tg.create_task(_publish_placed_orders(nc, placed_queue))
            tg.create_task(_publish_fills(nc, fills_queue))
            tg.create_task(_publish_state(nc, consolidator, interval=1.0))
            tg.create_task(_feed_market_prices(nc, brokers))
            tg.create_task(_serve_replay_requests(nc, brokers))
            tg.create_task(_sync_guard_config(guard_registry))
            tg.create_task(_publish_guard_status(nc, guard_registry))
    finally:
        for broker in brokers.values():
            await broker.aclose()
        await nc.drain()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped")
