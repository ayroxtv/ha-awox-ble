#!/usr/bin/env bash
# Copy custom_components/awox_ble to Home Assistant through the SSH add-on, then restart Core.
#
#   HA_SSH=root@homeassistant.local HA_SSH_PORT=22 scripts/deploy.sh [--no-restart]
#
# Uses tar over ssh (works even when rsync is missing on the add-on).
set -euo pipefail
HOST="${HA_SSH:?set HA_SSH, e.g. root@homeassistant.local}"
PORT="${HA_SSH_PORT:-22}"
cd "$(dirname "$0")/.."

tar -C custom_components --exclude '__pycache__' -czf - awox_ble \
  | ssh -p "$PORT" "$HOST" 'mkdir -p /config/custom_components \
      && rm -rf /config/custom_components/awox_ble \
      && tar -xzf - -C /config/custom_components \
      && echo "deployed: $(grep "\"version\"" /config/custom_components/awox_ble/manifest.json)"'

if [[ "${1:-}" != "--no-restart" ]]; then
  ssh -p "$PORT" "$HOST" 'ha core restart'
  echo "Home Assistant Core restart requested"
fi
