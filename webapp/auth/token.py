"""Shared-secret auth gate for admin endpoints (e.g. webapp/api/deploy.py).

The webapp has no other auth and is tunneled to a public hostname, so any
endpoint that can mutate infrastructure (deploy/stop a container, etc.) must
go through ``require_token``. Read-only endpoints are intentionally left
open — this only gates the admin surface.

Set ``API_TOKEN`` in the environment to enable. If it's unset, protected
endpoints fail closed (503) rather than silently accepting any request.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException


def require_token(authorization: str | None = Header(default=None)) -> None:
    token = os.environ.get("API_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="API_TOKEN not configured")

    provided = (authorization or "").removeprefix("Bearer ").strip()
    if not provided or not secrets.compare_digest(provided, token):
        raise HTTPException(status_code=401, detail="invalid or missing token")
