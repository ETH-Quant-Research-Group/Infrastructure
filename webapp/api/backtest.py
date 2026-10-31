"""Backtests from the /internal Deploy page (mode "backtest").

- ``POST /markets``: what a strategy image trades (asked from the image
  itself, by the manager).
- ``POST /coverage``: for each requested period, which months of bars /
  funding are already downloaded, per symbol, and when each symbol was
  listed on Bybit.  Read from the shared history cache, no download.
- ``POST /fetch``: download what's missing (SSE progress).  Done here, not in
  the backtest: the backtest container has no network on purpose.
- ``POST /run``: run the image's backtests on the manager (SSE progress).
- ``GET /results``, ``GET /results/{id}``: saved results.

The history database (``history.db``, :mod:`engine.backtest.history_db`):

- ``GET /seed``, ``POST /seed``: the starter set's status / gather now
  (:mod:`engine.backtest.seed`, also run daily in the background).
- ``GET /datasets``, ``DELETE /datasets/{name}``: what's stored.
- ``POST /upload/preview?kind=bars|funding&symbol=X`` (the CSV as the body):
  what was understood and found, and a token for saving.
- ``POST /upload/save``: store an upload as a dataset — saved for good, or
  only for a day ("for this backtest").

Behind oauth2-proxy like /api/deploy (not in OAUTH2_PROXY_SKIP_AUTH_REGEX).
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, cast

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from engine.backtest.data import (
    DEFAULT_CACHE,
    INTERVAL_SECONDS,
    PERP_EXCHANGE,
    SPOT_EXCHANGE,
    coverage,
    ensure_rules,
    listing_dates,
    load_history,
)
from engine.backtest.history_db import BYBIT, HistoryDB
from engine.backtest.seed import Seeder
from engine.backtest.upload import (
    ParsedUpload,
    UploadError,
    parse_bars,
    parse_funding,
    to_fraction,
)
from engine.data import codec
from execution.types import InstrumentRules

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter()

_MANAGER_URL = "http://manager:9000"
_MAX_RUNS = 20
_MAX_UPLOAD = 200 * 1024 * 1024
_UPLOADS = DEFAULT_CACHE / "uploads"
_TOKEN_TTL = timedelta(days=1)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,30}$")
_fetch_lock = asyncio.Lock()  # one download at a time: they write the same database
seeder = Seeder.from_env(lock=_fetch_lock)  # started by workers/webapp.py


class Period(BaseModel):
    start: str
    end: str
    interval: str
    dataset: str = BYBIT


class DataRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=50)
    runs: list[Period] = Field(min_length=1, max_length=_MAX_RUNS)
    warmup: int = Field(default=60, ge=0, le=10_000)


class MarketsRequest(BaseModel):
    image: str


class RunRequest(BaseModel):
    name: str
    image: str
    env: dict[str, str] = {}
    runs: list[Period] = Field(min_length=1, max_length=_MAX_RUNS)


def _when(text: str) -> datetime:
    try:
        when = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(400, f"not a date: {text!r}") from exc
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _period(p: Period) -> tuple[datetime, datetime, str, str]:
    start, end = _when(p.start), _when(p.end)
    if end <= start:
        raise HTTPException(400, f"{p.start} -> {p.end}: end must be after start")
    if p.interval not in INTERVAL_SECONDS:
        raise HTTPException(400, f"unsupported interval {p.interval!r}")
    if not _NAME_RE.match(p.dataset):
        raise HTTPException(400, f"bad dataset name {p.dataset!r}")
    return start, end, p.interval, p.dataset


def _event(event: str, **fields: Any) -> bytes:
    return f"data: __EVENT__{json.dumps({'event': event, **fields})}\n\n".encode()


@router.post("/markets")
async def markets(req: MarketsRequest) -> dict[str, Any]:
    # Pulling a large image can take minutes.
    try:
        async with httpx.AsyncClient(base_url=_MANAGER_URL, timeout=1200) as client:
            resp = await client.post("/backtest/markets", json=req.model_dump())
    except httpx.RequestError as exc:
        raise HTTPException(502, f"manager unreachable: {exc}") from exc
    data: dict[str, Any] = resp.json()
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, data.get("error", "failed"))
    return data


@router.post("/coverage")
async def data_coverage(req: DataRequest) -> dict[str, Any]:
    listings = await listing_dates()
    return {
        "runs": [
            coverage(
                req.symbols,
                interval,
                start,
                end,
                warmup_bars=req.warmup,
                listings=listings,
                dataset=dataset,
            )
            for start, end, interval, dataset in map(_period, req.runs)
        ]
    }


async def _stream_fetch(req: DataRequest) -> AsyncIterator[bytes]:
    periods = [_period(p) for p in req.runs]
    lines: asyncio.Queue[str | None] = asyncio.Queue()

    async def work() -> None:
        try:
            async with _fetch_lock:
                await ensure_rules()
                for start, end, interval, dataset in periods:
                    lines.put_nowait(
                        f"{start:%Y-%m-%d} -> {end:%Y-%m-%d} ({interval}, {dataset})"
                    )
                    await load_history(
                        req.symbols,
                        interval,
                        start,
                        end,
                        dataset=dataset,
                        warmup_bars=req.warmup,
                        progress=lines.put_nowait,
                    )
        except Exception as exc:  # reported to the page, not raised
            lines.put_nowait(f"__ERROR__{exc}")
        finally:
            lines.put_nowait(None)

    task = asyncio.create_task(work())
    try:
        while (line := await lines.get()) is not None:
            if line.startswith("__ERROR__"):
                yield _event("error", detail=line.removeprefix("__ERROR__"))
                return
            yield f"data: {line}\n\n".encode()
        yield _event("done")
    finally:
        await task


@router.post("/fetch")
async def fetch(req: DataRequest) -> StreamingResponse:
    for p in req.runs:
        _period(p)  # refuse bad input before the stream starts
    return StreamingResponse(_stream_fetch(req), media_type="text/event-stream")


async def _stream_run(req: RunRequest) -> AsyncIterator[bytes]:
    try:
        async with (
            # Several long backtests in a row: no overall timeout.
            httpx.AsyncClient(base_url=_MANAGER_URL, timeout=None) as client,  # nosec B113
            client.stream("POST", "/backtest/run", json=req.model_dump()) as resp,
        ):
            if resp.status_code >= 400:
                body = await resp.aread()
                yield _event("error", detail=body.decode(errors="replace"))
                return
            async for chunk in resp.aiter_bytes():
                yield chunk
    except httpx.RequestError as exc:
        yield _event("error", detail=f"manager unreachable: {exc}")


@router.post("/run")
async def run(req: RunRequest) -> StreamingResponse:
    for p in req.runs:
        _period(p)
    return StreamingResponse(_stream_run(req), media_type="text/event-stream")


async def _get(path: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(base_url=_MANAGER_URL, timeout=30) as client:
            resp = await client.get(path)
    except httpx.RequestError as exc:
        raise HTTPException(502, f"manager unreachable: {exc}") from exc
    data: dict[str, Any] = resp.json()
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, data.get("error", "failed"))
    return data


@router.get("/results")
async def results() -> dict[str, Any]:
    return await _get("/backtests")


@router.get("/results/{backtest_id}")
async def result(backtest_id: str) -> dict[str, Any]:
    return await _get(f"/backtests/{backtest_id}")


# ------------------------------------------------------------- starter set


@router.get("/seed")
async def seed_status() -> dict[str, Any]:
    return seeder.status


@router.post("/seed")
async def seed_now() -> dict[str, Any]:
    if not seeder.status["running"]:
        asyncio.create_task(seeder.run_once())  # noqa: RUF006 - status is the handle
    return seeder.status


# --------------------------------------------------------------- datasets


def _span(item: Any) -> dict[str, Any] | None:
    if item is None:
        return None
    first, last, count = item
    return {"first": first.isoformat(), "last": last.isoformat(), "count": count}


@router.get("/datasets")
async def datasets() -> dict[str, Any]:
    db = HistoryDB.open(DEFAULT_CACHE)
    assert db is not None
    try:
        db.drop_expired()
        return {
            "datasets": [
                {
                    "name": d.name,
                    "source": d.source,
                    "saved": d.saved,
                    "note": d.note,
                    "created_at": d.created_at.isoformat(),
                    "expires_at": d.expires_at.isoformat() if d.expires_at else None,
                    "symbols": {
                        symbol: {
                            "intervals": {
                                iv: _span(s)
                                for iv, s in cast(
                                    "dict[str, Any]", info["intervals"]
                                ).items()
                            },
                            "funding": _span(info["funding"]),
                        }
                        for symbol, info in sorted(d.series.items())
                    },
                }
                for d in db.datasets()
            ]
        }
    finally:
        db.close()


@router.delete("/datasets/{name}")
async def delete_dataset(name: str) -> dict[str, Any]:
    if name == BYBIT:
        raise HTTPException(400, "the Bybit data can't be deleted from here")
    db = HistoryDB.open(DEFAULT_CACHE)
    assert db is not None
    try:
        async with _fetch_lock:
            if not db.delete(name):
                raise HTTPException(404, f"no dataset {name!r}")
    finally:
        db.close()
    return {"deleted": name}


# ----------------------------------------------------------------- uploads


def _bybit_symbols() -> set[str]:
    """Perp symbols Bybit has order-size rules for (from the cached rules)."""
    path = DEFAULT_CACHE / "instrument_rules.json"
    if not path.exists():
        return set()
    return {
        d["symbol"]
        for d in json.loads(path.read_text())
        if d.get("exchange") == PERP_EXCHANGE
    }


def _drop_old_tokens() -> None:
    if not _UPLOADS.exists():
        return
    cutoff = datetime.now(UTC) - _TOKEN_TTL
    for path in _UPLOADS.glob("*.json"):
        if datetime.fromtimestamp(path.stat().st_mtime, tz=UTC) < cutoff:
            path.unlink(missing_ok=True)


@router.post("/upload/preview")
async def upload_preview(request: Request, kind: str, symbol: str) -> dict[str, Any]:
    """Read an uploaded CSV (the request body) and show what was understood;
    nothing goes into the database yet."""
    symbol = symbol.strip().upper()
    if kind not in ("bars", "funding"):
        raise HTTPException(400, "kind must be bars or funding")
    if not _SYMBOL_RE.match(symbol):
        raise HTTPException(400, f"bad symbol {symbol!r} (e.g. ETHUSDT)")
    if int(request.headers.get("content-length") or 0) > _MAX_UPLOAD:
        raise HTTPException(413, "file too large (200 MB at most)")
    body = await request.body()
    if len(body) > _MAX_UPLOAD:
        raise HTTPException(413, "file too large (200 MB at most)")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "the file isn't UTF-8 text (CSV expected)") from exc
    try:
        parsed = (parse_bars if kind == "bars" else parse_funding)(text, symbol)
    except UploadError as exc:
        raise HTTPException(400, str(exc)) from exc

    _drop_old_tokens()
    _UPLOADS.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    (_UPLOADS / f"{token}.json").write_text(
        json.dumps(
            {
                "kind": kind,
                "symbol": symbol,
                "interval": parsed.interval,
                "rows": [json.loads(codec.encode(r)) for r in parsed.rows],
            }
        )
    )
    await ensure_rules()
    return {
        **parsed.summary(),
        "token": token,
        "known_to_bybit": symbol in _bybit_symbols(),
    }


class RulesIn(BaseModel):
    qty_step: str
    min_qty: str | None = None
    min_notional: str | None = None
    max_market_qty: str | None = None


class SaveRequest(BaseModel):
    name: str
    bars_token: str | None = None
    funding_token: str | None = None
    funding_in_percent: bool = False
    save: bool = True  # False = kept for a day only ("for this backtest")
    note: str = ""
    rules: RulesIn | None = None


def _load_token(token: str, kind: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise HTTPException(400, "bad upload token")
    path = _UPLOADS / f"{token}.json"
    if not path.exists():
        raise HTTPException(404, "upload expired - preview the file again")
    item: dict[str, Any] = json.loads(path.read_text())
    if item["kind"] != kind:
        raise HTTPException(400, f"that upload is {item['kind']}, not {kind}")
    return item


def _rules_from(symbol: str, r: RulesIn) -> InstrumentRules:
    def d(v: str | None) -> Decimal | None:
        if v is None or not v.strip():
            return None
        try:
            value = Decimal(v)
        except InvalidOperation as exc:
            raise HTTPException(400, f"not a number: {v!r}") from exc
        if not value.is_finite() or value <= 0:
            raise HTTPException(400, f"must be above 0: {v!r}")
        return value

    step = d(r.qty_step)
    if step is None:
        raise HTTPException(400, "the lot step is required")
    return InstrumentRules(
        symbol=symbol,
        qty_step=step,
        min_qty=d(r.min_qty),
        max_market_qty=d(r.max_market_qty),
        min_notional=d(r.min_notional),
    )


@router.post("/upload/save")
async def upload_save(req: SaveRequest) -> dict[str, Any]:
    """Store previewed uploads (bars and / or funding of one symbol) in the
    dataset *name*: created if new, added to if it's an upload already."""
    if not _NAME_RE.match(req.name) or req.name == BYBIT:
        raise HTTPException(400, "name: letters, digits, - _ . (not 'bybit')")
    if not req.bars_token and not req.funding_token:
        raise HTTPException(400, "nothing to save")
    bars = _load_token(req.bars_token, "bars") if req.bars_token else None
    funding = _load_token(req.funding_token, "funding") if req.funding_token else None
    symbols = {x["symbol"] for x in (bars, funding) if x}
    if len(symbols) != 1:
        raise HTTPException(
            400, "the bars and funding files must be for the same symbol"
        )
    symbol = symbols.pop()
    if symbol not in _bybit_symbols() and req.rules is None:
        raise HTTPException(
            400, f"Bybit doesn't list {symbol}: enter its order-size rules"
        )
    rules = _rules_from(symbol, req.rules) if req.rules is not None else None

    written: dict[str, int] = {}
    db = HistoryDB.open(DEFAULT_CACHE)
    assert db is not None
    try:
        async with _fetch_lock:
            existing = next((d for d in db.datasets() if d.name == req.name), None)
            if existing is not None and existing.source != "upload":
                raise HTTPException(400, f"{req.name!r} isn't an upload dataset")
            db.dataset_id(req.name, source="upload", saved=req.save, note=req.note)
            if req.save:
                db.save(req.name)
            if bars:
                written["bars"] = db.write_bars(
                    req.name,
                    symbol,
                    bars["interval"],
                    [codec.decode(json.dumps(b).encode()) for b in bars["rows"]],  # type: ignore[misc]
                )
            if funding:
                parsed = parse_funding_rows(funding["rows"], symbol)
                if req.funding_in_percent:
                    parsed = to_fraction(parsed)
                written["funding"] = db.write_funding(req.name, symbol, parsed.rows)
            if rules is not None:
                for exchange in (PERP_EXCHANGE, SPOT_EXCHANGE):
                    db.write_rules(req.name, exchange, rules)
    finally:
        db.close()
    for token in (req.bars_token, req.funding_token):
        if token:
            (_UPLOADS / f"{token}.json").unlink(missing_ok=True)
    return {
        "dataset": req.name,
        "symbol": symbol,
        "saved": req.save,
        "written": written,
    }


def parse_funding_rows(rows: list[Any], symbol: str) -> ParsedUpload:
    """Funding rows kept from a preview, back as a :class:`ParsedUpload`."""
    return ParsedUpload(
        "funding", symbol, None, [codec.decode(json.dumps(r).encode()) for r in rows]
    )
