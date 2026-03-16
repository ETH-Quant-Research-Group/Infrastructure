from __future__ import annotations

import os

NATS_URL: str = os.environ.get("NATS_URL", "nats://localhost:4222")
DASHBOARD_URL: str = os.environ.get("DASHBOARD_URL", "http://localhost:8000")
