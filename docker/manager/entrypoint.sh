#!/usr/bin/env bash
set -euo pipefail
source /usr/local/bin/lib.sh

echo "[manager] starting — waiting for docker socket..."
until docker info >/dev/null 2>&1; do
  sleep 1
done

seed_state
echo "[manager] active strategies: $(active_classes | tr '\n' ',' | sed 's/,$//')"
reconcile_strategies

/usr/local/bin/watchdog.sh &
WATCHDOG_PID=$!
trap 'kill "$WATCHDOG_PID" 2>/dev/null || true' TERM INT

exec python3 /usr/local/bin/deploy_server.py
