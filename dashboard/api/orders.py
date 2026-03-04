from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def get_orders() -> dict:
    """Full order history."""
    return {"orders": []}


@router.get("/active")
async def get_active_orders() -> dict:
    """Currently open / pending orders."""
    return {"orders": []}
