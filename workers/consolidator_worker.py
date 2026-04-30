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
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import nats
import nats.aio.client
import nats.aio.msg

from config import NATS_URL
from decimal import ROUND_HALF_UP
from engine.data import codec
from engine.order.consolidator import OrderConsolidator
from execution.brokers.bybit import BybitBroker
from execution.brokers.bybit_spot import BybitSpotBroker
from execution.types import FillConfirmation, Order, OrderSide, OrderType, PerpOrder

if TYPE_CHECKING:
    from interfaces.broker import BaseBroker
    from interfaces.signals import TargetPosition

log = logging.getLogger(__name__)

_DEFAULT_EXCHANGE = os.getenv("DEFAULT_EXCHANGE", "bybit_demo").lower()


# ---------------------------------------------------------------------------
# Broker registry
#
# Add new brokers here.  Key = exchange name used in TargetPosition.exchange.
# ---------------------------------------------------------------------------


def _build_brokers() -> dict[str, BaseBroker]:
    """Instantiate every broker that should be active for this run."""
    brokers: dict[str, BaseBroker] = {
        "bybit_demo": BybitBroker(demo=True),   # requires BYBIT_API_KEY + BYBIT_API_SECRET
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
    symbol: str, side: OrderSide, quantity: Decimal, _price: Decimal,
    exchange: str = "",
) -> Order:
    # Spot orders use fractional quantities (e.g. 5.994 ETH).
    # Perp contracts are sized in whole contracts — round to integer.
    if exchange == "bybit_spot":
        quantity = quantity.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    else:
        quantity = quantity.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
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


async def _publish_broker_pnl(
    nc: nats.aio.client.Client,
    brokers: dict[str, BaseBroker],
    interval: float = 5.0,
) -> None:
    """Publish PnL snapshots for every broker.

    Delta-neutral accounting (perp short + spot long hedge):

      perp realized   = USDT wallet-balance delta since session start.
                        This is the only true cash flow: funding payments
                        credit/debit USDT every 8 h and fees are charged on
                        each trade.  It NEVER fluctuates with price.

      perp unrealized = totalUnrealisedPnl (mark-to-market on open perp).
                        Negative for a short when price rises, positive when
                        price falls.

      spot realized   = 0  (no cash is received until coins are actually sold)

      spot unrealized = spotCoinEquity delta since session start.
                        Positive when price rises (mirrors perp unrealized sign).

    Aggregate:
      total_realized   ≈ accumulated funding income   (stacks up every 8 h, never
                          reverts unless negative funding)
      total_unrealized ≈ 0 for a well-hedged position (perp and spot legs cancel)
      total            = realized + unrealized
    """
    _baselines: dict[str, Decimal] = {}

    while True:
        await asyncio.sleep(interval)
        ts = datetime.now(UTC).isoformat()
        total_realized = Decimal(0)
        total_unrealized = Decimal(0)

        for exchange, broker in brokers.items():
            payload: dict[str, Any] | None = None

            if hasattr(broker, "wallet_balance"):
                wb = await broker.wallet_balance()
                if not wb:
                    continue

                if "spotCoinEquity" in wb:
                    # ---- Spot leg ----
                    # The change in coin USD-value is unrealized (not cash).
                    # It offsets the perp unrealized so the fund net ≈ 0.
                    equity = Decimal(str(wb.get("spotCoinEquity", "0") or "0"))
                    if exchange not in _baselines:
                        _baselines[exchange] = equity
                    spot_unrealized = equity - _baselines[exchange]

                    total_unrealized += spot_unrealized
                    payload = {
                        "exchange": exchange,
                        "total_realized": "0",
                        "total_unrealized": str(spot_unrealized),
                        "total": str(spot_unrealized),
                        "total_equity": str(equity),
                        "timestamp": ts,
                    }

                else:
                    # ---- Perp leg ----
                    total_equity = Decimal(str(wb.get("totalEquity", "0") or "0"))
                    unrealized = Decimal(str(wb.get("totalUnrealisedPnl", "0") or "0"))
                    wallet = Decimal(str(wb.get("totalWalletBalance", "0") or "0"))
                    available = str(wb.get("totalAvailableBalance", "") or "")
                    coins = wb.get("coin", [])

                    # Realized PnL = delta of cumRealisedPnl since startup.
                    # Baselining means historical losses from previous sessions
                    # are excluded — only new funding payments and fees count.
                    positions = await broker.list_positions()
                    cum_realized = sum(
                        (p.realized_pnl for p in positions), Decimal(0)
                    )
                    realized_key = f"{exchange}_cum_realized"
                    if realized_key not in _baselines:
                        _baselines[realized_key] = cum_realized
                    pnl_realized = cum_realized - _baselines[realized_key]

                    # Perp MTM: negative for short when price rises.
                    pnl_unrealized = unrealized
                    pnl_total = pnl_realized + pnl_unrealized

                    # AUM = totalEquity minus spot-coin values (spot tracked separately)
                    spot_usd = Decimal(str(sum(
                        float(c.get("usdValue", 0) or 0)
                        for c in (coins if isinstance(coins, list) else [])
                        if c.get("coin") not in ("USDT", "USDC")
                    )))
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

            elif hasattr(broker, "total_realized_pnl") and hasattr(broker, "total_unrealized_pnl"):
                # Fallback for brokers without wallet_balance (e.g. paper broker)
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


async def _publish_positions(
    nc: nats.aio.client.Client,
    consolidator: OrderConsolidator,
    interval: float = 5.0,
) -> None:
    """Query every broker for its open positions and publish a unified snapshot."""
    while True:
        await asyncio.sleep(interval)
        snapshot: list[dict[str, Any]] = []
        for exchange, broker in consolidator.brokers.items():
            # Prefer list_positions() which returns all open positions without
            # needing in-memory tracking (survives consolidator restarts).
            if hasattr(broker, "list_positions"):
                positions = await broker.list_positions()
            else:
                symbols = consolidator.tracked_symbols_for(exchange)
                positions = [p for s in symbols if (p := await broker.position(s)) is not None]
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
        # Cross-reference perp avg entry prices to compute spot unrealized PnL.
        # Bybit spot API has no concept of unrealized PnL — we derive it using
        # the corresponding perp leg's entry price as the reference (both legs
        # were entered simultaneously at approximately the same price).
        # spot_unrealized = (current_price - perp_entry) * spot_qty
        perp_entries: dict[str, Decimal] = {}
        for p in snapshot:
            if p["exchange"] == "bybit_demo":
                entry = Decimal(p["avg_entry_price"])
                if entry > Decimal(0):
                    perp_entries[p["symbol"]] = entry

        for p in snapshot:
            if p["exchange"] == "bybit_spot" and p["symbol"] in perp_entries:
                spot_qty = Decimal(p["quantity"])
                # avg_entry_price for spot = current price (usdValue/qty from wallet)
                current_price = Decimal(p["avg_entry_price"])
                perp_entry = perp_entries[p["symbol"]]
                p["unrealized_pnl"] = str((current_price - perp_entry) * spot_qty)

        payload = json.dumps(snapshot).encode()
        await nc.publish("positions.snapshot", payload)
        log.debug("published positions snapshot: %d positions", len(snapshot))


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
            payload = json.dumps({
                "symbol": f.get("symbol", ""),
                "side": f.get("side", ""),
                "order_type": f.get("order_type", ""),
                "quantity": f.get("quantity", "0"),
                "price": f.get("price", "0"),
                "reduce_only": f.get("reduce_only", False),
            }).encode()
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
        format="%(asctime)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    brokers = _build_brokers()
    target_queue: asyncio.Queue[TargetPosition] = asyncio.Queue()
    placed_queue: asyncio.Queue[tuple[str, Order]] = asyncio.Queue()
    fills_queue: asyncio.Queue[FillConfirmation] = asyncio.Queue()

    consolidator = OrderConsolidator(
        target_queue=target_queue,
        brokers=brokers,
        default_exchange=_DEFAULT_EXCHANGE,
        min_order_size=Decimal("1"),  # ignore sub-1-unit residuals from rounding
        placed_queue=placed_queue,
        fills_queue=fills_queue,
        order_factory=_order_factory,
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
            tg.create_task(_publish_positions(nc, consolidator, interval=3.0))
            tg.create_task(_publish_broker_pnl(nc, brokers, interval=3.0))
            tg.create_task(_feed_market_prices(nc, brokers))
            tg.create_task(_serve_replay_requests(nc, brokers))
    finally:
        for broker in brokers.values():
            await broker.aclose()
        await nc.drain()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped")
