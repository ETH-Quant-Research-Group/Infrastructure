"""Container status + live log streaming for the /internal ops view.

No auth dependency here — oauth2-proxy's session cookie is the actual gate
(this path is deliberately kept out of OAUTH2_PROXY_SKIP_AUTH_REGEX in
docker-compose.yml). EventSource, used by the frontend log viewer, can't
send a custom Authorization header the way the deploy endpoints require —
but it does send cookies automatically for same-origin requests, which is
why the cookie-based gate (not a bearer token) protects these.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter()

_MANAGER_URL = "http://manager:9000"


@router.get("/status")
async def status() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(base_url=_MANAGER_URL, timeout=30) as client:
            resp = await client.get("/status")
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail=f"manager unreachable: {exc}"
        ) from exc
    data: dict[str, Any] = resp.json()
    return data


async def _stream_logs(name: str) -> AsyncIterator[bytes]:
    try:
        async with (
            # timeout=None is intentional: this is a live `docker logs -f`
            # tail, meant to stay open indefinitely until the client
            # disconnects — a timeout would kill the stream.
            httpx.AsyncClient(base_url=_MANAGER_URL, timeout=None) as client,  # nosec B113
            client.stream("GET", f"/logs/{name}") as resp,
        ):
            if resp.status_code != 200:
                yield f"data: [error] manager returned {resp.status_code}\n\n".encode()
                return
            async for chunk in resp.aiter_bytes():
                yield chunk
    except httpx.RequestError as exc:
        yield f"data: [error] manager unreachable: {exc}\n\n".encode()


@router.get("/logs/{name}")
async def logs(name: str) -> StreamingResponse:
    return StreamingResponse(_stream_logs(name), media_type="text/event-stream")
