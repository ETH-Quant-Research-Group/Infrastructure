from __future__ import annotations

from fastapi import APIRouter, HTTPException

from dashboard.store import (
    broker_exchange_states,
    broker_pnl_history,
    broker_pnl_latest,
    pnl_history,
    pnl_latest,
)

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


_BUCKET_SECONDS = 30  # downsample resolution for charts


@router.get("/aggregate")
async def get_aggregate() -> dict:
    """Aggregate PnL across all strategies, downsampled to 30s buckets."""
    import datetime as _dt

    per_strategy: dict[str, dict[int, float]] = {}
    for sid, records in pnl_history.items():
        by_bucket: dict[int, float] = {}
        for r in records:
            try:
                t = int(_dt.datetime.fromisoformat(r["timestamp"]).timestamp())
                bucket = (t // _BUCKET_SECONDS) * _BUCKET_SECONDS
                by_bucket[bucket] = float(r["total"])  # latest in bucket wins
            except (KeyError, ValueError):
                pass
        per_strategy[sid] = by_bucket

    all_buckets: set[int] = set()
    for d in per_strategy.values():
        all_buckets.update(d)

    combined: dict[int, float] = {}
    for t in all_buckets:
        combined[t] = sum(d.get(t, 0.0) for d in per_strategy.values())

    series = [{"time": t, "value": v} for t, v in sorted(combined.items())]
    total = sum(float(r["total"]) for r in pnl_latest.values())
    return {"series": series, "total": total}


@router.get("/broker")
async def get_broker_pnl() -> dict:
    """Broker's authoritative PnL snapshot + history, downsampled to 30s buckets."""
    import datetime as _dt

    by_bucket: dict[int, float] = {}
    for r in broker_pnl_history:
        try:
            t = int(_dt.datetime.fromisoformat(r["timestamp"]).timestamp())
            bucket = (t // _BUCKET_SECONDS) * _BUCKET_SECONDS
            by_bucket[bucket] = float(r["total"])  # latest in bucket wins
        except (KeyError, ValueError):
            pass
    series = [{"time": t, "value": v} for t, v in sorted(by_bucket.items())]
    return {
        "latest": broker_pnl_latest,
        "series": series,
    }


@router.get("/fund")
async def get_fund() -> dict:
    """Fund-level snapshot: AUM, PnL & available balance summed across all brokers."""
    total_aum = 0.0
    total_wallet = 0.0
    total_available = 0.0
    total_realized = 0.0
    total_unrealized = 0.0
    brokers_with_aum = []
    brokers_without_aum = []

    for exchange, state in broker_exchange_states.items():
        realized = float(state.get("total_realized", "0") or "0")
        unrealized = float(state.get("total_unrealized", "0") or "0")
        total_realized += realized
        total_unrealized += unrealized

        if state.get("total_equity"):
            total_aum += float(state["total_equity"])
            total_wallet += float(state.get("total_wallet_balance", "0") or "0")
            total_available += float(state.get("available_balance", "0") or "0")
            brokers_with_aum.append(exchange)
        else:
            brokers_without_aum.append(exchange)

    return {
        "total_aum": round(total_aum, 4) or None,
        "total_wallet_balance": round(total_wallet, 4) or None,
        "total_available": round(total_available, 4) or None,
        "total_pnl": round(total_realized + total_unrealized, 4),
        "total_realized": round(total_realized, 4),
        "total_unrealized": round(total_unrealized, 4),
        # AUM only covers brokers that report wallet balance (e.g. BybitBroker).
        # Paper brokers don't have a real account balance so they're excluded.
        "aum_covers": brokers_with_aum,
        "aum_excludes": brokers_without_aum,
        "num_brokers": len(broker_exchange_states),
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
