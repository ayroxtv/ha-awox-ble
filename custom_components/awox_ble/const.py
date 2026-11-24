"""Constants for the AwoX BLE integration."""

from __future__ import annotations

import hashlib
from typing import Final

DOMAIN: Final = "awox_ble"
MANUFACTURER: Final = "AwoX"

CONF_MESH_NAME: Final = "mesh_name"
CONF_MESH_PASSWORD: Final = "mesh_password"  # noqa: S105
CONF_MESH_KEY: Final = "mesh_key"
CONF_DEVICES: Final = "devices"
CONF_GATEWAY_MACS: Final = "gateway_macs"
CONF_MESH_ID: Final = "mesh_id"

# Connection management (seconds).
RECONNECT_BACKOFF_MIN: Final = 0.5
RECONNECT_BACKOFF_MAX: Final = 60.0
CONNECT_MAX_ATTEMPTS: Final = 2
LOGIN_TIMEOUT: Final = 10.0
KEEPALIVE_INTERVAL: Final = 60.0
KEEPALIVE_TIMEOUT: Final = 10.0
DISCONNECT_TIMEOUT: Final = 5.0
NODE_FAILURE_COOLDOWN: Final = 30.0
NODE_AUTH_FAILURE_COOLDOWN: Final = 600.0
IDLE_WAIT_FOR_ADVERTISEMENT: Final = 30.0
# Grace period before entities go unavailable after the gateway link drops,
# so a quick reconnect does not make every light flap.
UNAVAILABLE_GRACE: Final = 30.0

# Command queue.
COMMAND_TTL: Final = 10.0  # drop commands that could not be sent in time
COMMAND_MIN_GAP: Final = 0.05  # Telink nodes drop packets when flooded
COMMAND_WRITE_TIMEOUT: Final = 5.0
COMMAND_WRITE_WITH_RESPONSE: Final = True

REDACT_KEYS: Final = {CONF_MESH_NAME, CONF_MESH_PASSWORD, CONF_MESH_KEY, "unique_id", "title"}


def mesh_unique_id(mesh_name: str) -> str:
    """Stable entry id that does not expose the mesh name."""
    return hashlib.sha256(mesh_name.encode()).hexdigest()[:16]
