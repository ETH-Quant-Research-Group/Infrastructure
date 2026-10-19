#!/usr/bin/env bash
# Shared helpers for entrypoint.sh, watchdog.sh, manage.sh.
set -euo pipefail

MAP_FILE="/usr/local/bin/strategies.map"
STATE_FILE="/state/active_strategies"

dc() {
  (cd /workspace && docker compose "$@")
}

# Reads strategies.map into two parallel arrays: MAP_SERVICES / MAP_CLASSES.
load_map() {
  MAP_SERVICES=()
  MAP_CLASSES=()
  while IFS='=' read -r service class; do
    [[ -z "$service" || "$service" == \#* ]] && continue
    MAP_SERVICES+=("$service")
    MAP_CLASSES+=("$class")
  done < "$MAP_FILE"
}

# service_for_class <ClassName> -> echoes compose service name, or nothing.
service_for_class() {
  local want="$1"
  load_map
  for i in "${!MAP_CLASSES[@]}"; do
    if [[ "${MAP_CLASSES[$i]}" == "$want" ]]; then
      echo "${MAP_SERVICES[$i]}"
      return 0
    fi
  done
  return 1
}

# Seed the state file from ACTIVE_STRATEGIES on first boot only. Later
# changes go through manage.sh enable/disable, which edit the state file
# directly — the env var is just the initial default.
seed_state() {
  mkdir -p "$(dirname "$STATE_FILE")"
  if [[ ! -f "$STATE_FILE" ]]; then
    echo "${ACTIVE_STRATEGIES:-}" | tr ',' '\n' | sed '/^\s*$/d' > "$STATE_FILE"
    echo "[manager] seeded $STATE_FILE from ACTIVE_STRATEGIES=${ACTIVE_STRATEGIES:-<empty>}"
  fi
}

active_classes() {
  [[ -f "$STATE_FILE" ]] || touch "$STATE_FILE"
  cat "$STATE_FILE"
}

# Starts every strategy-* service whose class is in the state file, stops
# every other one. Explicit `up -d <service>` bypasses the "strategies"
# profile gate that keeps them out of a bare `docker compose up`.
reconcile_strategies() {
  load_map
  local wanted
  wanted="$(active_classes)"
  for i in "${!MAP_SERVICES[@]}"; do
    local svc="${MAP_SERVICES[$i]}"
    local cls="${MAP_CLASSES[$i]}"
    if grep -qxF "$cls" <<< "$wanted"; then
      dc up -d "$svc" >/dev/null 2>&1 || echo "[manager] failed to start $svc"
    else
      dc stop "$svc" >/dev/null 2>&1 || true
    fi
  done
}
