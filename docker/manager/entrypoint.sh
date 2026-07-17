#!/usr/bin/env bash
set -euo pipefail
source /usr/local/bin/lib.sh

echo "[manager] starting — waiting for docker socket..."
until docker info >/dev/null 2>&1; do
  sleep 1
done

# GHCR packages default to private — log in so `docker pull` on deploy
# actually works, instead of relying on the image being public. Reuses
# GITHUB_PACKAGES_TOKEN (already read:packages scoped for the package
# browser); this just extends the same credential to the actual pull.
if [[ -n "${GITHUB_PACKAGES_TOKEN:-}" && -n "${GITHUB_USERNAME:-}" ]]; then
  echo "[manager] logging in to ghcr.io as $GITHUB_USERNAME..."
  echo "$GITHUB_PACKAGES_TOKEN" | docker login ghcr.io -u "$GITHUB_USERNAME" --password-stdin
else
  echo "[manager] GITHUB_PACKAGES_TOKEN/GITHUB_USERNAME not set — GHCR pulls will fail unless the package is public"
fi

seed_state
echo "[manager] active strategies: $(active_classes | tr '\n' ',' | sed 's/,$//')"
reconcile_strategies

/usr/local/bin/watchdog.sh &
WATCHDOG_PID=$!
trap 'kill "$WATCHDOG_PID" 2>/dev/null || true' TERM INT

exec python3 /usr/local/bin/deploy_server.py
