from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from webapp.api import (
    deploy,
    market,
    ops,
    orders,
    performance,
    positions,
    strategies,
    topology,
)
from webapp.ws.manager import router as ws_router

app = FastAPI(title="Infrastructure Dashboard")

app.include_router(market.router, prefix="/api/market", tags=["market"])
app.include_router(positions.router, prefix="/api/positions", tags=["positions"])
app.include_router(orders.router, prefix="/api/orders", tags=["orders"])
app.include_router(strategies.router, prefix="/api/strategies", tags=["strategies"])
app.include_router(performance.router, prefix="/api/performance", tags=["performance"])
app.include_router(topology.router, prefix="/api/topology", tags=["topology"])
app.include_router(deploy.router, prefix="/api/deploy", tags=["deploy"])
app.include_router(ops.router, prefix="/api/ops", tags=["ops"])
app.include_router(ws_router, prefix="/ws", tags=["websocket"])

_FRONTEND = Path(__file__).parent / "frontend" / "dist"
if _FRONTEND.exists():
    from starlette.responses import FileResponse
    from starlette.staticfiles import StaticFiles as _SF

    # Mount static assets (JS/CSS/images) under /assets so they are served
    # directly without the html=True catch-all intercepting WebSocket upgrades.
    app.mount("/assets", _SF(directory=_FRONTEND / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str):
        if full_path.startswith("api/"):
            from fastapi import HTTPException

            raise HTTPException(status_code=404)
        return FileResponse(_FRONTEND / "index.html")
