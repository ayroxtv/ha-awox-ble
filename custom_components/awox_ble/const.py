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
LOGIN_STEP_TIMEOUT: Final = 5.0
NOTIFY_SETUP_TIMEOUT: Final = 35.0  # BlueZ ATT timeout is 30 s
# BlueZ path (bluez#2383): cycle the link before BlueZ's 30 s ATT timeout does.
NOTIFY_CYCLE: Final = 27.0
NOTIFY_READY_DELAY: Final = 0.1
RECONNECT_PLANNED_DELAY: Final = 0.05

# What to do on a local (BlueZ) adapter, where notifications cost a link reset
# every ~30 s on these nodes. ESPHome proxies always get stable push updates.
CONF_BLUEZ_MODE: Final = "bluez_mode"
BLUEZ_MODE_STABLE: Final = "stable"  # stable link, no push updates
BLUEZ_MODE_CYCLE: Final = "cycle"  # push updates, link cycled every ~27 s
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
COMMAND_TTL: Final = 30.0  # drop commands that could not be sent in time
COMMAND_MIN_GAP: Final = 0.05  # Telink nodes drop packets when flooded
COMMAND_WRITE_TIMEOUT: Final = 5.0
# Write without response: faster, and never queued behind a pending ATT request
# (the command characteristic supports it on AwoX / EGLO nodes).
COMMAND_WRITE_WITH_RESPONSE: Final[bool | None] = False

REDACT_KEYS: Final = {CONF_MESH_NAME, CONF_MESH_PASSWORD, CONF_MESH_KEY, "unique_id", "title"}


def mesh_unique_id(mesh_name: str) -> str:
    """Stable entry id that does not expose the mesh name."""
    return hashlib.sha256(mesh_name.encode()).hexdigest()[:16]
