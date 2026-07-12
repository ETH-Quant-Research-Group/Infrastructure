"""Deploy arbitrary strategy images (from the ETH-Quant-Research-Group GHCR
namespace) as containers, on request from the dashboard.

This is a thin forwarder — all the actual docker.sock work happens in the
`manager` container's internal deploy_server.py (reachable only inside the
compose network, never published to the host).

TEMPORARILY UNGATED: the ``require_token`` dependency is disabled below
while oauth2-proxy's auth is also disabled (see docker-compose.yml's
OAUTH2_PROXY_SKIP_AUTH_REGEX). Re-enable both together — passing
``dependencies=[Depends(require_token)]`` to APIRouter() below, matching
the commented-out line.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# from fastapi import Depends
# from webapp.auth import require_token

router = APIRouter()
# router = APIRouter(dependencies=[Depends(require_token)])

_MANAGER_URL = "http://manager:9000"


class DeployRequest(BaseModel):
    name: str
    image: str
    env: dict[str, str] = {}


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


@router.post("/deploy")
async def deploy(req: DeployRequest) -> dict[str, Any]:
    resp = await _forward("POST", "/deploy", json=req.model_dump())
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.json())
    data: dict[str, Any] = resp.json()
    return data


@router.post("/{name}/stop")
async def stop(name: str) -> dict[str, Any]:
    resp = await _forward("POST", f"/stop/{name}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.json())
    data: dict[str, Any] = resp.json()
    return data
