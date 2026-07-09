from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from dashboard import store as _store
from dashboard.store import (
    broker_exchange_states,
    broker_pnl_history,
    pnl_history,
    pnl_latest,
)

router = APIRouter()
log = logging.getLogger(__name__)


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
        "latest": _store.broker_pnl_latest,
        "series": series,
    }


@router.get("/fund")
async def get_fund() -> dict:
    """Fund-level snapshot: AUM, PnL & available balance summed across all brokers.

    PnL is taken from the atomic aggregate ``broker.pnl`` snapshot (published by
    the consolidator after *both* legs are computed in the same loop iteration).
    This avoids the timing mismatch where individual per-exchange messages arrive
    a few ms apart, causing the fund total to momentarily spike before the second
    leg updates — which matters for delta-neutral strategies where the two legs
    should cancel.
    """
    total_aum = 0.0
    total_wallet = 0.0
    total_available = 0.0
    brokers_with_aum = []
    brokers_without_aum = []

    for exchange, state in broker_exchange_states.items():
        if state.get("total_equity"):
            total_aum += float(state["total_equity"])
            total_wallet += float(state.get("total_wallet_balance", "0") or "0")
            total_available += float(state.get("available_balance", "0") or "0")
            brokers_with_aum.append(exchange)
        else:
            brokers_without_aum.append(exchange)

    # Use the atomic aggregate published after both legs are summed.
    if _store.broker_pnl_latest:
        total_realized = float(_store.broker_pnl_latest["total_realized"])
        total_unrealized = float(_store.broker_pnl_latest["total_unrealized"])
    else:
        # Fallback before the first aggregate arrives
        total_realized = sum(float(s.get("total_realized", 0) or 0) for s in broker_exchange_states.values())
        total_unrealized = sum(float(s.get("total_unrealized", 0) or 0) for s in broker_exchange_states.values())

    return {
        "total_aum": round(total_aum, 4) or None,
        "total_wallet_balance": round(total_wallet, 4) or None,
        "total_available": round(total_available, 4) or None,
        "total_pnl": round(total_realized + total_unrealized, 4),
        "total_realized": round(total_realized, 4),
        "total_unrealized": round(total_unrealized, 4),
        "aum_covers": brokers_with_aum,
        "aum_excludes": brokers_without_aum,
        "num_brokers": len(broker_exchange_states),
    }


_8H_SECONDS = 8 * 3600  # 28 800


def _bucket_to_8h(
    history: list[dict],
) -> list[tuple[int, float]]:
    """Bucket PnL snapshots to 8h settlement periods (00:00, 08:00, 16:00 UTC).

    Returns ``(bucket_timestamp, total_pnl)`` sorted oldest-first, using the
    last snapshot value in each bucket.
    """
    import datetime as _dt

    buckets: dict[int, float] = {}
    for r in history:
        try:
            t = int(_dt.datetime.fromisoformat(r["timestamp"]).timestamp())
            bucket = (t // _8H_SECONDS) * _8H_SECONDS
            buckets[bucket] = float(r["total"])
        except (KeyError, ValueError):
            continue
    return sorted(buckets.items())


_MIN_DD_SAMPLES = 60          # require >= 60 snapshots (~5 min @ 5s) before publishing dd
_MIN_DD_ELAPSED_SEC = 3600    # require >= 1 hour of history before publishing dd


@router.get("/metrics")
async def get_metrics() -> dict:
    """Aggregate risk / performance metrics.

    - **total_return**: cumulative PnL / initial equity (AUM - PnL)
    - **sharpe_ratio**: annualized, computed on 8h settlement period returns;
      annualization factor = sqrt(1095) for 3 settlements/day * 365 days;
      uses population std (industry standard)
    - **max_drawdown**: absolute peak-to-trough drawdown in PnL ($) divided
      by current AUM (stable denominator).  Returns null until at least
      _MIN_DD_SAMPLES snapshots and _MIN_DD_ELAPSED_SEC of history exist —
      avoids spurious huge-percent readings from tiny early variance.
    - **win_rate**: fraction of 8h settlement periods with positive return
    """
    import datetime as _dt

    total_return = None
    sharpe = None
    max_dd = None
    win_rate = None

    aum = sum(
        float(s.get("total_equity", 0) or 0)
        for s in broker_exchange_states.values()
    )

    # ---- Total Return ----
    if _store.broker_pnl_latest:
        total_pnl = float(_store.broker_pnl_latest["total"])
        initial_equity = aum - total_pnl if aum > 0 else 0.0
        if initial_equity > 0:
            total_return = round(total_pnl / initial_equity, 6)

    # ---- 8h bucketed returns (for Sharpe + win rate) ----
    bucketed = _bucket_to_8h(broker_pnl_history)
    if len(bucketed) >= 3:
        returns = [
            bucketed[i][1] - bucketed[i - 1][1]
            for i in range(1, len(bucketed))
        ]
        n = len(returns)
        mean_r = sum(returns) / n
        variance = sum((r - mean_r) ** 2 for r in returns) / n
        std_r = variance**0.5

        # Sharpe: annualize with sqrt(1095) for 8h periods
        if std_r > 0:
            sharpe = round((mean_r / std_r) * (1095**0.5), 4)

        # Win rate: fraction of positive 8h returns
        wins = sum(1 for r in returns if r > 0)
        win_rate = round(wins / n, 4)

    # ---- Max Drawdown ----
    # Absolute dollar drawdown / AUM.  Two guardrails:
    #   1. Require enough samples (avoid noise from a few seconds of data)
    #   2. Require enough elapsed time (avoid 5s spikes counting as drawdowns)
    # The dollar-based formula avoids the (peak - total) / peak explosion when
    # peak is near zero but total dips slightly negative.
    if len(broker_pnl_history) >= _MIN_DD_SAMPLES and aum > 0:
        try:
            first_ts = _dt.datetime.fromisoformat(
                broker_pnl_history[0]["timestamp"]
            ).timestamp()
            last_ts = _dt.datetime.fromisoformat(
                broker_pnl_history[-1]["timestamp"]
            ).timestamp()
            elapsed = last_ts - first_ts
        except (KeyError, ValueError):
            elapsed = 0.0

        if elapsed >= _MIN_DD_ELAPSED_SEC:
            peak = float("-inf")
            worst_abs_dd = 0.0  # in dollars
            for r in broker_pnl_history:
                try:
                    total = float(r["total"])
                except (KeyError, ValueError):
                    continue
                if total > peak:
                    peak = total
                abs_dd = peak - total  # always >= 0 once peak is set
                if abs_dd > worst_abs_dd:
                    worst_abs_dd = abs_dd
            if worst_abs_dd > 0:
                max_dd = round(worst_abs_dd / aum, 6)

    return {
        "total_return": total_return,
        "sharpe_ratio": sharpe,
        "max_drawdown": max_dd,
        "win_rate": win_rate,
    }


# ---------------------------------------------------------------------------
# Funding income vs trading fees breakdown
#
# Polls Bybit's transaction-log endpoint to surface what the strategy actually
# *earned* from funding payments, separately from what it *paid* in fees.
# Cached in SQLite so the endpoint is cheap and survives dashboard restarts.
# Refresh policy: re-fetch from Bybit at most once per hour.
# ---------------------------------------------------------------------------

_FUNDING_FEES_CACHE_KEY = "funding_fees_cache_v1"
_FUNDING_FEES_REFRESH_S = 3600   # 1 hour


async def _fetch_funding_and_fees(symbols: list[str], lookback_hours: int = 720) -> dict:
    """Pull SETTLEMENT (funding) and TRADE (fee) entries from Bybit transaction-log.

    Bybit's ``/v5/account/transaction-log`` silently returns an empty list
    when ``endTime - startTime`` exceeds ~7 days (observed: 48h returns
    expected data, 168h+ returns nothing).  We therefore chunk the lookback
    into ``_CHUNK_HOURS`` slices and aggregate, with full pagination per slice.

    Returns aggregated breakdown::

        {
          "per_symbol": {
            "ETHUSDT": {"funding": +0.94, "fees": -15.31, "net": -14.37, "settlements": 6, "trades": 12},
            ...
          },
          "totals": {"funding": ..., "fees": ..., "net": ...},
          "fetched_at": "2026-05-02T17:50:00+00:00",
          "lookback_hours": 720,
        }
    """
    from execution.brokers.bybit import BybitBroker

    _CHUNK_HOURS = 48           # Bybit-safe window per request
    _MAX_PAGES_PER_CHUNK = 20   # cap pagination to avoid runaway loops

    broker = BybitBroker(demo=True)
    now_ms = int(time.time() * 1000)
    chunk_ms = _CHUNK_HOURS * 3600 * 1000
    total_ms = lookback_hours * 3600 * 1000
    per_symbol: dict[str, dict] = {}

    try:
        for sym in symbols:
            funding_total = 0.0
            fees_total = 0.0
            settlement_count = 0
            trade_count = 0

            chunk_end = now_ms
            chunk_start = max(now_ms - total_ms, chunk_end - chunk_ms)
            while chunk_end > now_ms - total_ms:
                cursor = ""
                for _ in range(_MAX_PAGES_PER_CHUNK):
                    params: dict = {
                        "accountType": "UNIFIED",
                        "category": "linear",
                        "currency": "USDT",
                        "symbol": sym,
                        "startTime": chunk_start,
                        "endTime": chunk_end,
                        "limit": 50,
                    }
                    if cursor:
                        params["cursor"] = cursor
                    try:
                        r = await broker._get("/v5/account/transaction-log", params)
                    except Exception as exc:
                        log.warning(
                            "transaction-log failed for %s [%d-%d]: %s",
                            sym, chunk_start, chunk_end, exc,
                        )
                        break
                    result = r.get("result") or {}
                    items = result.get("list") or []
                    for x in items:
                        txn_type = x.get("type", "")
                        change = float(x.get("change") or 0)
                        if txn_type == "SETTLEMENT":
                            funding_total += change
                            settlement_count += 1
                        elif txn_type == "TRADE":
                            fee = float(x.get("fee") or 0)
                            # Bybit returns fee as a positive number paid;
                            # cash-flow sign should be negative for fees out.
                            if fee > 0:
                                fees_total -= fee
                                trade_count += 1
                    cursor = result.get("nextPageCursor") or ""
                    if not cursor:
                        break
                # Step backward by one chunk
                chunk_end = chunk_start
                chunk_start = max(now_ms - total_ms, chunk_end - chunk_ms)
                if chunk_end <= now_ms - total_ms:
                    break

            per_symbol[sym] = {
                "funding": round(funding_total, 6),
                "fees": round(fees_total, 6),
                "net": round(funding_total + fees_total, 6),
                "settlements": settlement_count,
                "trades": trade_count,
            }
    finally:
        await broker.aclose()

    totals = {
        "funding": round(sum(s["funding"] for s in per_symbol.values()), 6),
        "fees": round(sum(s["fees"] for s in per_symbol.values()), 6),
        "net": round(sum(s["net"] for s in per_symbol.values()), 6),
    }
    return {
        "per_symbol": per_symbol,
        "totals": totals,
        "fetched_at": datetime.now(UTC).isoformat(),
        "lookback_hours": lookback_hours,
    }


@router.get("/funding-vs-fees")
async def get_funding_vs_fees(
    symbols: str = "ETHUSDT,LINKUSDT",
    lookback_hours: int = 720,
    refresh: bool = False,
) -> dict:
    """Funding income vs trading fees breakdown for the given symbols.

    Cached in SQLite for ``_FUNDING_FEES_REFRESH_S`` seconds.  Pass
    ``?refresh=true`` to force a re-fetch.
    """
    from dashboard.persistence import DashboardDB

    db = DashboardDB()
    cached_raw = db.get_kv(_FUNDING_FEES_CACHE_KEY)
    cached: dict | None = None
    if cached_raw:
        try:
            cached = json.loads(cached_raw)
            fetched_at = datetime.fromisoformat(cached["fetched_at"])
            age_s = (datetime.now(UTC) - fetched_at).total_seconds()
            if not refresh and age_s < _FUNDING_FEES_REFRESH_S:
                cached["cache_age_s"] = round(age_s)
                return cached
        except (json.JSONDecodeError, KeyError, ValueError):
            cached = None

    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        raise HTTPException(status_code=400, detail="At least one symbol required")

    try:
        fresh = await _fetch_funding_and_fees(symbol_list, lookback_hours)
    except Exception as exc:
        log.exception("funding-vs-fees fetch failed")
        if cached is not None:
            cached["stale"] = True
            cached["error"] = str(exc)
            return cached
        raise HTTPException(
            status_code=502, detail=f"Bybit fetch failed: {exc}"
        ) from exc

    db.set_kv(_FUNDING_FEES_CACHE_KEY, json.dumps(fresh))
    fresh["cache_age_s"] = 0
    return fresh
