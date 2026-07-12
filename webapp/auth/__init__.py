"""Auth for the webapp — currently just the shared-secret token gate.

Room for this to grow: e.g. a module here to verify the identity header
an oauth2-proxy sits in front and forwards once that's wired up.
"""

from __future__ import annotations

from webapp.auth.token import require_token

__all__ = ["require_token"]
