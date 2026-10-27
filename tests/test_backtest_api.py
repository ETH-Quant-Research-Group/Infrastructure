"""The webapp's history endpoints (webapp/api/backtest.py) and the starter
set's pass (engine/backtest/seed.py) — offline: a temporary cache, no Bybit."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.backtest import data as data_module
from engine.backtest import seed as seed_module
from engine.backtest.seed import Seeder
from webapp.api import backtest

if TYPE_CHECKING:
    from pathlib import Path

_BARS = "time,open,high,low,close,volume\n" + "".join(
    f"{1788220800 + i * 3600},{100 + i},{102 + i},{99 + i},{101 + i},5\n"
    for i in range(48)
)  # 2026-09-01 00:00 UTC onwards, 1h
_FUNDING = "timestamp,fundingRate\n1788249600000,0.0001\n1788278400000,-0.0002\n"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(data_module, "DEFAULT_CACHE", tmp_path)
    monkeypatch.setattr(backtest, "DEFAULT_CACHE", tmp_path)
    monkeypatch.setattr(backtest, "_UPLOADS", tmp_path / "uploads")
    # No network: rules and listing dates already "downloaded".
    (tmp_path / "instrument_rules.json").write_text(
        json.dumps(
            [
                {
                    "exchange": "bybit_demo",
                    "symbol": "ETHUSDT",
                    "qty_step": "0.01",
                    "min_qty": "0.01",
                    "max_market_qty": None,
                    "min_notional": None,
                    "trading": True,
                },
            ]
        )
    )
    (tmp_path / "listings.json").write_text(
        json.dumps(
            {
                "fetched_at": datetime.now(UTC).isoformat(),
                "listings": {"ETHUSDT": 1500000000000},
            }
        )
    )
    app = FastAPI()
    app.include_router(backtest.router, prefix="/api/backtest")
    return TestClient(app)


def _preview(client: TestClient, kind: str, symbol: str, body: str) -> Any:
    return client.post(
        f"/api/backtest/upload/preview?kind={kind}&symbol={symbol}",
        content=body.encode(),
        headers={"Content-Type": "text/csv"},
    )


def test_upload_preview_then_save_then_use(client: TestClient) -> None:
    bars = _preview(client, "bars", "ethusdt", _BARS)
    assert bars.status_code == 200, bars.text
    preview = bars.json()
    assert (preview["symbol"], preview["interval"], preview["rows"]) == (
        "ETHUSDT",
        "1h",
        48,
    )
    assert preview["known_to_bybit"] is True
    funding = _preview(client, "funding", "ETHUSDT", _FUNDING).json()

    saved = client.post(
        "/api/backtest/upload/save",
        json={
            "name": "my-eth",
            "bars_token": preview["token"],
            "funding_token": funding["token"],
            "note": "test",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["written"] == {"bars": 48, "funding": 2}

    listed = {
        d["name"]: d for d in client.get("/api/backtest/datasets").json()["datasets"]
    }
    assert listed["my-eth"]["saved"] and listed["my-eth"]["note"] == "test"
    assert listed["my-eth"]["symbols"]["ETHUSDT"]["intervals"]["1h"]["count"] == 48
    assert "bybit" in listed

    cov = client.post(
        "/api/backtest/coverage",
        json={
            "symbols": ["ETHUSDT"],
            "warmup": 0,
            "runs": [
                {
                    "start": "2026-09-01",
                    "end": "2026-09-02",
                    "interval": "8h",
                    "dataset": "my-eth",
                }
            ],
        },
    ).json()["runs"][0]
    assert cov["symbols"]["ETHUSDT"]["bars_source"] == "my-eth"
    assert cov["symbols"]["ETHUSDT"]["funding_source"] == "my-eth"
    assert cov["ready"]

    assert client.delete("/api/backtest/datasets/my-eth").status_code == 200
    assert client.delete("/api/backtest/datasets/my-eth").status_code == 404
    assert client.delete("/api/backtest/datasets/bybit").status_code == 400


def test_a_symbol_bybit_doesnt_list_needs_rules(client: TestClient) -> None:
    preview = _preview(client, "bars", "MYCOINUSDT", _BARS).json()
    assert preview["known_to_bybit"] is False

    refused = client.post(
        "/api/backtest/upload/save",
        json={"name": "mine", "bars_token": preview["token"]},
    )
    assert refused.status_code == 400 and "order-size rules" in refused.json()["detail"]

    saved = client.post(
        "/api/backtest/upload/save",
        json={
            "name": "mine",
            "bars_token": preview["token"],
            "save": False,
            "rules": {"qty_step": "0.1", "min_qty": "1"},
        },
    )
    assert saved.status_code == 200, saved.text
    mine = next(
        d
        for d in client.get("/api/backtest/datasets").json()["datasets"]
        if d["name"] == "mine"
    )
    assert not mine["saved"] and mine["expires_at"]


def test_upload_problems_are_explained(client: TestClient) -> None:
    assert _preview(client, "trades", "ETHUSDT", _BARS).status_code == 400
    assert _preview(client, "bars", "ETH/USDT", _BARS).status_code == 400
    bad = _preview(client, "bars", "ETHUSDT", "a,b\n1,2\n")
    assert bad.status_code == 400 and "column" in bad.json()["detail"]

    funding = _preview(client, "funding", "ETHUSDT", _FUNDING).json()
    wrong_kind = client.post(
        "/api/backtest/upload/save", json={"name": "x", "bars_token": funding["token"]}
    )
    assert wrong_kind.status_code == 400
    assert (
        client.post("/api/backtest/upload/save", json={"name": "bybit"}).status_code
        == 400
    )
    expired = client.post(
        "/api/backtest/upload/save", json={"name": "x", "bars_token": "0" * 32}
    )
    assert expired.status_code == 404


def test_bad_periods_are_refused(client: TestClient) -> None:
    def cov(**run: str) -> int:
        body = {
            "symbols": ["ETHUSDT"],
            "runs": [
                {"start": "2026-09-01", "end": "2026-09-02", "interval": "1h", **run}
            ],
        }
        return client.post("/api/backtest/coverage", json=body).status_code

    assert cov() == 200
    assert cov(end="2026-08-01") == 400
    assert cov(interval="3h") == 400
    assert cov(dataset="../etc") == 400
    assert cov(start="soon") == 400


# ------------------------------------------------------------- starter set


def test_starter_set_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fetched: list[str] = []

    async def top(n: int) -> list[str]:
        return ["AUSDT", "BUSDT"][:n]

    async def download(symbols: list[str], *args: Any, **kwargs: Any) -> None:
        if symbols == ["BUSDT"]:
            raise RuntimeError("Bybit said no")
        fetched.extend(symbols)

    async def nothing(*args: Any, **kwargs: Any) -> Any:
        return {}

    monkeypatch.setattr(seed_module, "top_symbols", top)
    monkeypatch.setattr(seed_module, "download_history", download)
    monkeypatch.setattr(seed_module, "listing_dates", nothing)
    monkeypatch.setattr(seed_module, "ensure_rules", nothing)

    seeder = Seeder(top=2, cache_dir=tmp_path)
    asyncio.run(seeder.run_once())

    assert fetched == ["AUSDT"]
    status = seeder.status
    assert (status["done"], status["running"], status["symbols"]) == (
        2,
        False,
        ["AUSDT", "BUSDT"],
    )
    assert status["errors"] == [
        "BUSDT: Bybit said no"
    ]  # one failure doesn't stop the pass
    assert status["last_finished"]


def test_starter_set_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HISTORY_SEED", "off")
    monkeypatch.setenv("HISTORY_SEED_TOP", "7")
    seeder = Seeder.from_env()
    assert seeder.top == 7 and seeder.status["enabled"] is False
    asyncio.run(seeder.run_forever())  # returns at once when off
