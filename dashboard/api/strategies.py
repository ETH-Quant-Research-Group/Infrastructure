from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def get_strategies() -> dict:
    """Registered strategies and their current guard status."""
    return {"strategies": []}


@router.get("/signals")
async def get_signals() -> dict:
    """Latest target positions emitted by each strategy."""
    return {"signals": []}
