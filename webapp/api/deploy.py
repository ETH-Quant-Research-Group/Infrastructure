"""Deploy arbitrary strategy images (from the ETH-Quant-Research-Group GHCR
namespace) as containers, on request from the dashboard.

This is a thin forwarder — all the actual docker.sock work happens in the
`manager` container's internal deploy_server.py (reachable only inside the
compose network, never published to the host).

No auth dependency here — webapp has no published port (see
docker-compose.yml), so the only way to reach this router at all is through
oauth2-proxy, which already requires a GitHub-org-authenticated session for
every path under /api/deploy* (see OAUTH2_PROXY_SKIP_AUTH_REGEX). Local dev
bypasses that same gate via docker-compose.override.yml.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Literal

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter()

_MANAGER_URL = "http://manager:9000"
_GITHUB_API = "https://api.github.com"
_GITHUB_ORG = os.environ.get("GITHUB_ORG", "ETH-Quant-Research-Group")
_GITHUB_PACKAGES_TOKEN = os.environ.get("GITHUB_PACKAGES_TOKEN", "")


class DeployRequest(BaseModel):
    name: str
    image: str
    env: dict[str, str] = {}
    # Label only for now — nothing downstream branches on it yet.
    mode: Literal["backtest", "paper", "live"] = "paper"


async def _forward(method: str, path: str, **kwargs: Any) -> httpx.Response:
    try:
        async with httpx.AsyncClient(base_url=_MANAGER_URL, timeout=120) as client:
            return await client.request(method, path, **kwargs)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail=f"manager unreachable: {exc}"
        ) from exc


@router.get("/deployed")
async def list_deployed() -> dict[str, Any]:
    resp = await _forward("GET", "/deployed")
    data: dict[str, Any] = resp.json()
    return data


def _sse_error(detail: str) -> bytes:
    payload = json.dumps({"event": "error", "detail": detail})
    return f"data: __EVENT__{payload}\n\n".encode()


async def _stream_deploy(req: DeployRequest) -> AsyncIterator[bytes]:
    """Proxies manager's live deploy progress stream (docker pull/run output,
    then a seamless transition into the new container's own startup logs) —
    not just a single JSON response, since a several-hundred-MB pull can
    take a while and the Deploy page wants to show that happening.
    """
    try:
        async with (
            # timeout=None is intentional, same reasoning as ops.py's log
            # streaming: this can legitimately run for a while (a large
            # image pull) and then tails forever until the client closes it.
            httpx.AsyncClient(base_url=_MANAGER_URL, timeout=None) as client,  # nosec B113
            client.stream("POST", "/deploy", json=req.model_dump()) as resp,
        ):
            if resp.status_code >= 400:
                body = await resp.aread()
                yield _sse_error(body.decode(errors="replace"))
                return
            async for chunk in resp.aiter_bytes():
                yield chunk
    except httpx.RequestError as exc:
        yield _sse_error(f"manager unreachable: {exc}")


@router.post("/deploy")
async def deploy(req: DeployRequest) -> StreamingResponse:
    return StreamingResponse(_stream_deploy(req), media_type="text/event-stream")


@router.post("/{name}/stop")
async def stop(name: str) -> dict[str, Any]:
    resp = await _forward("POST", f"/stop/{name}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.json())
    data: dict[str, Any] = resp.json()
    return data


@router.get("/packages")
async def list_packages() -> dict[str, Any]:
    """List container packages + tags under the org's GHCR namespace.

    Needs a GitHub PAT with `read:packages` scope — separate from the
    oauth2-proxy login flow, which only authenticates users, not backend
    API calls. Set GITHUB_PACKAGES_TOKEN in .env to enable this.
    """
    if not _GITHUB_PACKAGES_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="GITHUB_PACKAGES_TOKEN not set — add a GitHub PAT with "
            "read:packages scope to .env to browse GHCR packages",
        )

    headers = {
        "Authorization": f"Bearer {_GITHUB_PACKAGES_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_GITHUB_API}/orgs/{_GITHUB_ORG}/packages",
                params={"package_type": "container", "per_page": 100},
                headers=headers,
            )
            resp.raise_for_status()

            results: list[dict[str, Any]] = []
            for pkg in resp.json():
                name = pkg.get("name", "")
                versions_resp = await client.get(
                    f"{_GITHUB_API}/orgs/{_GITHUB_ORG}/packages/container/{name}/versions",
                    params={"per_page": 10},
                    headers=headers,
                )
                tags: list[str] = []
                if versions_resp.status_code == 200:
                    for v in versions_resp.json():
                        container = v.get("metadata", {}).get("container", {})
                        tags += container.get("tags", [])
                results.append(
                    {
                        "name": name,
                        "image": f"ghcr.io/{_GITHUB_ORG.lower()}/{name}",
                        "tags": tags,
                        "updated_at": pkg.get("updated_at"),
                    }
                )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502, detail=f"GitHub API error: {exc.response.status_code}"
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail=f"GitHub API unreachable: {exc}"
        ) from exc

    return {"packages": results}
