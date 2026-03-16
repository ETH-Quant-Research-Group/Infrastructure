from __future__ import annotations

from fastapi import APIRouter

from dashboard.store import (
    record_bar,
    record_broker_exchange_pnl,
    record_broker_pnl,
    record_fill,
    record_order,
    record_pnl,
    record_positions,
    register_strategy,
    unregister_strategy,
)
from dashboard.ws.manager import manager

router = APIRouter()


@router.post("/positions")
async def push_positions(payload: list[dict]) -> dict:
    record_positions(payload)
    await manager.broadcast({"subject": "positions.snapshot", "data": payload})
    return {"ok": True}


@router.post("/pnl")
async def push_pnl(payload: dict) -> dict:
    record_pnl(payload)
    await manager.broadcast(
        {"subject": f"pnl.{payload.get('strategy_id', '')}", "data": payload}
    )
    return {"ok": True}


@router.post("/broker-pnl")
async def push_broker_pnl(payload: dict) -> dict:
    exchange = payload.get("exchange")
    if exchange and exchange != "all":
        record_broker_exchange_pnl(payload)
        await manager.broadcast({"subject": f"broker.pnl.{exchange}", "data": payload})
    else:
        record_broker_pnl(payload)
        await manager.broadcast({"subject": "broker.pnl", "data": payload})
    return {"ok": True}


@router.post("/orders")
async def push_order(payload: dict) -> dict:
    record_order(payload)
    symbol = payload.get("symbol", "unknown")
    await manager.broadcast({"subject": f"orders.placed.{symbol}", "data": payload})
    return {"ok": True}


@router.post("/fills")
async def push_fill(payload: dict) -> dict:
    record_fill(payload)
    await manager.broadcast({"subject": "fills.new", "data": payload})
    return {"ok": True}


@router.post("/bars")
async def push_bar(payload: dict) -> dict:
    """payload: { subject: "futures.BTCUSDT.bars.1m", data: { ...bar fields } }"""
    subject = payload.get("subject", "")
    data = payload.get("data", {})
    record_bar(data, subject)
    await manager.broadcast({"subject": subject, "data": data})
    return {"ok": True}


@router.post("/strategy/register")
async def push_strategy_register(payload: dict) -> dict:
    register_strategy(payload)
    await manager.broadcast(
        {"subject": f"strategy.register.{payload.get('name', '')}", "data": payload}
    )
    return {"ok": True}


@router.post("/strategy/unregister")
async def push_strategy_unregister(payload: dict) -> dict:
    name = payload.get("name", "")
    unregister_strategy(name)
    await manager.broadcast({"subject": f"strategy.unregister.{name}", "data": payload})
    return {"ok": True}
