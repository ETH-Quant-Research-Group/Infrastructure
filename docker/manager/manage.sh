#!/usr/bin/env bash
# Interactive + scriptable control surface for the stack. Run via:
#   docker compose exec manager manage.sh              # interactive menu
#   docker compose exec manager manage.sh status        # one-shot command
#   docker compose exec manager manage.sh enable MomentumStrategy
set -euo pipefail
source /usr/local/bin/lib.sh

usage() {
  cat <<'EOF'
Usage: manage.sh <command> [args]

  status                    docker compose ps
  logs <service> [tail]     follow logs (default tail=200)
  restart <service>         restart one service
  rebuild <service>         rebuild image + recreate one service
  strategies                list strategy classes and active/running state
  enable <ClassName>        start a strategy container, persist as active
  disable <ClassName>       stop a strategy container, persist as inactive
  up                        start core services (nats/datafeed/consolidator/webapp)
  down                      stop the entire stack (including this manager)
  shell <service>           open a shell in a running service's container

With no arguments, opens an interactive menu.
EOF
}

cmd_status() { dc ps; }

cmd_logs() {
  local svc="${1:?service name required}"
  local tail="${2:-200}"
  dc logs -f --tail "$tail" "$svc"
}

cmd_restart() { dc restart "${1:?service name required}"; }

cmd_rebuild() {
  local svc="${1:?service name required}"
  dc build "$svc"
  dc up -d "$svc"
}

cmd_strategies() {
  load_map
  local active
  active="$(active_classes)"
  printf '%-28s %-30s %-8s %s\n' "SERVICE" "CLASS" "ACTIVE" "CONTAINER STATE"
  for i in "${!MAP_SERVICES[@]}"; do
    local svc="${MAP_SERVICES[$i]}" cls="${MAP_CLASSES[$i]}"
    local is_active="no"
    grep -qxF "$cls" <<< "$active" && is_active="yes"
    local state
    state="$(dc ps --format '{{.Service}} {{.State}}' 2>/dev/null | awk -v s="$svc" '$1==s{print $2}')"
    printf '%-28s %-30s %-8s %s\n' "$svc" "$cls" "$is_active" "${state:-not created}"
  done
}

cmd_enable() {
  local cls="${1:?strategy class name required}"
  service_for_class "$cls" >/dev/null || { echo "Unknown strategy class: $cls" >&2; exit 1; }
  if ! grep -qxF "$cls" "$STATE_FILE" 2>/dev/null; then
    echo "$cls" >> "$STATE_FILE"
  fi
  reconcile_strategies
  echo "enabled $cls"
}

cmd_disable() {
  local cls="${1:?strategy class name required}"
  [[ -f "$STATE_FILE" ]] && grep -vxF "$cls" "$STATE_FILE" > "$STATE_FILE.tmp" || true
  mv -f "$STATE_FILE.tmp" "$STATE_FILE" 2>/dev/null || true
  reconcile_strategies
  echo "disabled $cls"
}

cmd_up() { dc up -d; }

cmd_down() { dc down; }

cmd_shell() {
  local svc="${1:?service name required}"
  dc exec "$svc" sh
}

run() {
  case "${1:-}" in
    status) cmd_status ;;
    logs) shift; cmd_logs "$@" ;;
    restart) shift; cmd_restart "$@" ;;
    rebuild) shift; cmd_rebuild "$@" ;;
    strategies) cmd_strategies ;;
    enable) shift; cmd_enable "$@" ;;
    disable) shift; cmd_disable "$@" ;;
    up) cmd_up ;;
    down) cmd_down ;;
    shell) shift; cmd_shell "$@" ;;
    -h|--help|help) usage ;;
    "") menu ;;
    *) echo "Unknown command: $1" >&2; usage; exit 1 ;;
  esac
}

menu() {
  local choice
  while true; do
    echo
    echo "===== Infrastructure Manager ====="
    echo "1) Status"
    echo "2) Strategies (list active/running)"
    echo "3) Enable a strategy"
    echo "4) Disable a strategy"
    echo "5) Restart a service"
    echo "6) Rebuild a service"
    echo "7) Tail logs of a service"
    echo "8) Shell into a service"
    echo "9) Start core stack (up)"
    echo "0) Quit"
    read -rp "> " choice
    case "$choice" in
      1) cmd_status ;;
      2) cmd_strategies ;;
      3) read -rp "Strategy class name: " c; cmd_enable "$c" ;;
      4) read -rp "Strategy class name: " c; cmd_disable "$c" ;;
      5) read -rp "Service name: " s; cmd_restart "$s" ;;
      6) read -rp "Service name: " s; cmd_rebuild "$s" ;;
      7) read -rp "Service name: " s; cmd_logs "$s" ;;
      8) read -rp "Service name: " s; cmd_shell "$s" ;;
      9) cmd_up ;;
      0) exit 0 ;;
      *) echo "invalid choice" ;;
    esac
  done
}

run "$@"
