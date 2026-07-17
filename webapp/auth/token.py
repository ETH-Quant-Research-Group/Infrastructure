"""Shared-secret auth gate for admin endpoints (e.g. webapp/api/deploy.py).

The webapp has no other auth of its own and is tunneled to a public
hostname, so any endpoint that can mutate infrastructure (deploy/stop a
container, etc.) must go through ``require_token``. Read-only endpoints are
intentionally left open — this only gates the admin surface.

Set ``API_TOKEN`` in the environment to enable. If it's unset, protected
endpoints fail closed (503) rather than silently accepting any request.

``DEV_MODE=1`` bypasses this entirely — set by docker-compose.override.yml
for local dev, never in the base docker-compose.yml. Production runs
without the override file (`docker compose -f docker-compose.yml up`), so
this stays enforced there.
"""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import Header, HTTPException

log = logging.getLogger(__name__)

_warned_dev_mode = False


def _dev_mode() -> bool:
    return os.environ.get("DEV_MODE", "0").strip().lower() in ("1", "true", "yes")


def require_token(authorization: str | None = Header(default=None)) -> None:
    global _warned_dev_mode
    if _dev_mode():
        if not _warned_dev_mode:
            log.warning("DEV_MODE is on — admin endpoints are unauthenticated")
            _warned_dev_mode = True
        return

    token = os.environ.get("API_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="API_TOKEN not configured")

    provided = (authorization or "").removeprefix("Bearer ").strip()
    if not provided or not secrets.compare_digest(provided, token):
        raise HTTPException(status_code=401, detail="invalid or missing token")
