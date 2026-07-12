#!/usr/bin/env bash
# Runs the full local backend stack for manual testing: NATS, data feed,
# a strategy worker, the consolidator, and the webapp API/WebSocket.
# Ctrl+C stops everything cleanly.
#
# Run `npm run dev` in webapp/frontend separately for the frontend
# with hot reload (it proxies /api and /ws to this webapp).
#
# Override the strategy or port if needed:
#   STRATEGY_NAME=MomentumStrategy WEBAPP_PORT=8001 ./dev.sh

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

STRATEGY_NAME="${STRATEGY_NAME:-ExampleStrategy}"
WEBAPP_PORT="${WEBAPP_PORT:-8000}"

pids=()
cleanup() {
  echo
  echo "==> Stopping..."
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "==> Starting NATS on :4222"
nats-server &
pids+=($!)

echo "==> Waiting for NATS..."
for _ in $(seq 1 20); do
  nc -z localhost 4222 2>/dev/null && break
  sleep 0.5
done

echo "==> Starting data feed"
uv run python -m workers.datafeed_server &
pids+=($!)

echo "==> Starting strategy worker ($STRATEGY_NAME)"
STRATEGY_NAME="$STRATEGY_NAME" uv run python -m workers.strategy_worker &
pids+=($!)

echo "==> Starting consolidator"
uv run python -m workers.consolidator_worker &
pids+=($!)

echo "==> Starting webapp on :$WEBAPP_PORT"
uv run uvicorn workers.webapp:app --port "$WEBAPP_PORT" &
pids+=($!)

echo
echo "All services started. Webapp: http://localhost:$WEBAPP_PORT"
echo "Press Ctrl+C to stop everything."
wait
