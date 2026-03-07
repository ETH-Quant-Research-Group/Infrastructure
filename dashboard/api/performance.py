from __future__ import annotations

from fastapi import APIRouter, HTTPException

from dashboard.store import pnl_history, pnl_latest

router = APIRouter()


@router.get("/pnl")
async def get_pnl() -> dict:
    """Latest PnL snapshot for every active strategy."""
    return {"pnl": list(pnl_latest.values())}


@router.get("/pnl/{strategy_id}")
async def get_pnl_strategy(strategy_id: str) -> dict:
    """Latest snapshot + full history for a single strategy."""
    if strategy_id not in pnl_latest:
        raise HTTPException(status_code=404, detail=f"No PnL data for '{strategy_id}'")
    return {
        "latest": pnl_latest[strategy_id],
        "history": pnl_history.get(strategy_id, []),
    }


@router.get("/metrics")
async def get_metrics() -> dict:
    """Aggregate risk / performance metrics."""
    return {
        "total_return": None,
        "sharpe_ratio": None,
        "max_drawdown": None,
        "win_rate": None,
    }
