from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from dashboard.api import market, orders, performance, positions, strategies
from dashboard.ws.manager import router as ws_router

app = FastAPI(title="Infrastructure Dashboard")

app.include_router(market.router, prefix="/api/market", tags=["market"])
app.include_router(positions.router, prefix="/api/positions", tags=["positions"])
app.include_router(orders.router, prefix="/api/orders", tags=["orders"])
app.include_router(strategies.router, prefix="/api/strategies", tags=["strategies"])
app.include_router(performance.router, prefix="/api/performance", tags=["performance"])
app.include_router(ws_router, prefix="/ws", tags=["websocket"])

_FRONTEND = Path(__file__).parent / "frontend"
app.mount("/", StaticFiles(directory=_FRONTEND, html=True), name="frontend")
