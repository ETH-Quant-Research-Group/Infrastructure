"""Tests for C1 (funding history pagination) and C4 (funding interval).

    uv run pytest tests/test_funding_fixes.py --no-cov -q
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

import strategies.funding_arb_bybit as strat_mod
from data.connectors.bybit_linear import BybitLinearConnector
from data.types import FundingRate

H8 = 8 * 3_600_000
T0 = 1_640_995_200_000            # 2022-01-01 00:00 UTC


# --------------------------------------------------------------------------
# A fake /v5/market/funding/history that follows Bybit's documented rules:
#   - at most `limit` (<= 200) rows, NEWEST first, inside [startTime, endTime]
#   - only startTime  -> error (retCode != 0, HTTP 200)
#   - only endTime    -> 200 rows up to endTime
# --------------------------------------------------------------------------

def fake_bybit(n_settlements: int, calls: list[dict]) -> httpx.MockTransport:
    history = [T0 + i * H8 for i in range(n_settlements)]

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        calls.append(q)
        start, end = q.get("startTime"), q.get("endTime")
        if start and not end:
            return httpx.Response(200, json={"retCode": 10001, "retMsg": "params error", "result": {}})
        lo = int(start) if start else 0
        hi = int(end) if end else history[-1]
        rows = [t for t in history if lo <= t <= hi]
        rows = sorted(rows, reverse=True)[: min(int(q.get("limit", 200)), 200)]
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"list": [
            {"symbol": q["symbol"], "fundingRate": "0.0001", "fundingRateTimestamp": str(t)} for t in rows]}})

    return httpx.MockTransport(handler)


async def fetch(n: int, *, start_ms: int, end_ms: int | None):
    calls: list[dict] = []
    conn = BybitLinearConnector()
    await conn._client.aclose()
    conn._client = httpx.AsyncClient(base_url="https://api.bybit.com", transport=fake_bybit(n, calls))
    try:
        rows = [r async for r in conn.fetch_funding_rates("ETHUSDT", start_ms=start_ms, end_ms=end_ms)]
    finally:
        await conn.aclose()
    return rows, calls


def test_fetches_full_history_not_just_200():
    n = 5288                                   # ~4.8 years of 8h settlements
    rows, calls = asyncio.run(fetch(n, start_ms=T0, end_ms=T0 + (n - 1) * H8))
    ts = [r["funding_time_ms"] for r in rows]
    assert len(ts) == n                        # the old loop returned 200
    assert ts == sorted(ts) and len(set(ts)) == n   # oldest first, no duplicates
    assert len(calls) == -(-n // 200)          # one request per 200 rows


def test_respects_start_bound():
    n = 1000
    start = T0 + 300 * H8
    rows, _ = asyncio.run(fetch(n, start_ms=start, end_ms=T0 + (n - 1) * H8))
    assert rows[0]["funding_time_ms"] == start and len(rows) == 700


def test_never_sends_start_time_alone():
    """Bybit rejects startTime-only requests; end_ms=None must still work."""
    rows, calls = asyncio.run(fetch(450, start_ms=T0, end_ms=None))
    assert all("endTime" in c for c in calls)
    assert len(rows) == 450


def test_bybit_error_is_raised_not_swallowed():
    async def run():
        conn = BybitLinearConnector()
        await conn._client.aclose()
        conn._client = httpx.AsyncClient(
            base_url="https://api.bybit.com",
            transport=httpx.MockTransport(lambda r: httpx.Response(
                200, json={"retCode": 10001, "retMsg": "params error", "result": {}})))
        try:
            return [r async for r in conn.fetch_funding_rates("ETHUSDT", start_ms=T0, end_ms=T0 + H8)]
        finally:
            await conn.aclose()
    with pytest.raises(RuntimeError, match="10001"):
        asyncio.run(run())


# --------------------------------------------------------------------------
# Funding interval
# --------------------------------------------------------------------------

_CLOCK = [datetime(2026, 1, 1, tzinfo=UTC)]


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: ANN001
        return _CLOCK[0]


@pytest.fixture
def strategy(monkeypatch):
    monkeypatch.setattr(strat_mod, "datetime", _Clock)
    return strat_mod.FundingArbBybitStrategy(symbols=("SOLUSDT",))


def nft(h: float) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=h)


def test_learns_shorter_interval_immediately(strategy):
    strategy._update_funding_interval("SOLUSDT", nft(8))
    strategy._update_funding_interval("SOLUSDT", nft(12))          # one 4h step
    assert strategy._funding_interval_h["SOLUSDT"] == 4.0


def test_one_long_step_is_a_missed_tick_not_an_interval_change(strategy):
    for h in (4, 8, 12):
        strategy._update_funding_interval("SOLUSDT", nft(h))
    assert strategy._funding_interval_h["SOLUSDT"] == 4.0
    strategy._update_funding_interval("SOLUSDT", nft(20))          # 8h jump: missed tick
    assert strategy._funding_interval_h["SOLUSDT"] == 4.0
    strategy._update_funding_interval("SOLUSDT", nft(24))          # back to 4h steps
    assert strategy._funding_interval_h["SOLUSDT"] == 4.0
    strategy._update_funding_interval("SOLUSDT", nft(32))          # two 8h steps in a row
    strategy._update_funding_interval("SOLUSDT", nft(40))
    assert strategy._funding_interval_h["SOLUSDT"] == 8.0          # genuine change back


def test_ignores_invalid_steps_and_repeats(strategy):
    for h in (8, 8, 8, 11, 11):                                   # repeats, then a 3h step
        strategy._update_funding_interval("SOLUSDT", nft(h))
    assert strategy._funding_interval_h["SOLUSDT"] == 8.0


def _feed_hourly(strategy, rate: str, interval_h: int, hours: int) -> list:
    """Hourly funding ticks like the live ticker; returns emitted signals."""
    signals = []
    for h in range(hours):
        _CLOCK[0] = nft(h) + timedelta(seconds=1)
        next_settle = (h // interval_h + 1) * interval_h
        ev = FundingRate(symbol="SOLUSDT", funding_rate=Decimal(rate), mark_price=Decimal("150"),
                         timestamp=_CLOCK[0], next_funding_time=nft(next_settle))
        sig = strategy.on_funding_rate(ev)
        if sig is not None:
            signals.append((h, sig))
    return signals


def test_entry_uses_real_interval(strategy):
    """0.01% per settlement: 10.95% ann at 8h (below the 15% entry), 21.9% at 4h (above)."""
    assert _feed_hourly(strategy, "0.0001", interval_h=4, hours=40)    # enters
    other = strat_mod.FundingArbBybitStrategy(symbols=("SOLUSDT",))
    assert _feed_hourly(other, "0.0001", interval_h=8, hours=40) == []  # does not