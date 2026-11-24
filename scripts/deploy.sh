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

# COPYFILE_DISABLE: no macOS AppleDouble (._*) files. Writes need root: the add-on
# user (e.g. hassio) is in wheel and has passwordless sudo; root logins skip sudo.
SUDO='sudo -n'
[[ "$HOST" == root@* ]] && SUDO=''
COPYFILE_DISABLE=1 tar -C custom_components --exclude '__pycache__' --exclude '._*' -czf - awox_ble \
  | ssh -p "$PORT" "$HOST" "$SUDO mkdir -p /config/custom_components \
      && $SUDO rm -rf /config/custom_components/awox_ble \
      && $SUDO tar -xzf - -C /config/custom_components --no-same-owner \
      && grep '\"version\"' /config/custom_components/awox_ble/manifest.json"
echo "deployed to $HOST:/config/custom_components/awox_ble"

if [[ "${1:-}" != "--no-restart" ]]; then
  # The `ha` CLI needs the Supervisor token, which non-interactive SSH sessions
  # of the add-on may not have. Fall back to a manual restart in that case.
  if ssh -p "$PORT" "$HOST" "ha core restart"; then
    echo "Home Assistant Core restart requested"
  else
    echo "Could not restart from SSH: restart HA from the UI (or the homeassistant.restart action)." >&2
  fi
fi
