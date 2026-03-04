from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/pnl")
async def get_pnl() -> dict:
    """Time-series PnL across all strategies."""
    return {"pnl": []}


@router.get("/metrics")
async def get_metrics() -> dict:
    """Aggregate risk / performance metrics."""
    return {
        "total_return": None,
        "sharpe_ratio": None,
        "max_drawdown": None,
        "win_rate": None,
    }
