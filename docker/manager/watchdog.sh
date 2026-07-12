#!/usr/bin/env bash
# Foreground loop (container's main process): keeps the strategy-* containers
# in sync with /state/active_strategies. Crash-recovery for the always-on
# services (nats/datafeed/consolidator/webapp) is handled natively by
# their `restart: unless-stopped` policy — this loop only has to do the
# thing compose profiles can't: start/stop specific strategy containers on
# demand.
set -euo pipefail
source /usr/local/bin/lib.sh

INTERVAL="${WATCHDOG_INTERVAL:-15}"

trap 'echo "[manager] stopping"; exit 0' TERM INT

while true; do
  reconcile_strategies
  sleep "$INTERVAL"
done
