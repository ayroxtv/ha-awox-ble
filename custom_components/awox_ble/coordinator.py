"""Push coordinator: notification-driven state, fanned out per mesh id.

There is no ``update_interval``: state only changes when the mesh sends a
notification (or when an entity applies an optimistic update).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .const import UNAVAILABLE_GRACE
from .mesh import MeshManager
from .protocol import DEST_ALL, LightStatus

_LOGGER = logging.getLogger(__name__)


class AwoxMeshCoordinator:
    """Holds the last status of every node and routes updates to entities."""

    def __init__(self, hass: HomeAssistant, label: str) -> None:
        self.hass = hass
        self.label = label
        self.statuses: dict[int, LightStatus] = {}
        # Optimistically available at start: commands sent while the first
        # connection is being made are queued instead of being refused by HA.
        self.available = True
        self._listeners: dict[int, list[Callable[[], None]]] = {}
        self._cancel_grace: CALLBACK_TYPE | None = None
        self._shutdown = False
        self.mesh: MeshManager | None = None

    # -- wiring -------------------------------------------------------------

    @callback
    def async_add_listener(self, mesh_id: int, update: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.setdefault(mesh_id, []).append(update)

        @callback
        def _remove() -> None:
            self._listeners[mesh_id].remove(update)

        return _remove

    @callback
    def _notify(self, mesh_id: int | None = None) -> None:
        targets = (
            self._listeners.get(mesh_id, [])
            if mesh_id is not None
            else [cb for cbs in self._listeners.values() for cb in cbs]
        )
        for update in list(targets):
            update()

    # -- callbacks from MeshManager ----------------------------------------------

    @callback
    def async_handle_status(self, status: LightStatus) -> None:
        if status.mesh_id not in self._listeners:
            _LOGGER.debug("[%s] status for unknown mesh id %d", self.label, status.mesh_id)
        self.statuses[status.mesh_id] = status
        self._notify(status.mesh_id)

    @callback
    def async_handle_connection(self, connected: bool) -> None:
        if self._cancel_grace is not None:
            self._cancel_grace()
            self._cancel_grace = None
        if self._shutdown:
            return
        if connected:
            if not self.available:
                self.available = True
                self._notify()
            return
        # Do not flap entities on a short reconnect.
        self._cancel_grace = async_call_later(self.hass, UNAVAILABLE_GRACE, self._grace_expired)

    @callback
    def _grace_expired(self, _now: Any) -> None:
        self._cancel_grace = None
        if self.mesh is not None and self.mesh.connected:
            return
        _LOGGER.info("[%s] mesh unreachable for %.0f s", self.label, UNAVAILABLE_GRACE)
        self.available = False
        self._notify()

    @callback
    def async_start_grace(self) -> None:
        """Go unavailable if no connection is made within the grace period."""
        if self._cancel_grace is None and not (self.mesh is not None and self.mesh.connected):
            self._cancel_grace = async_call_later(self.hass, UNAVAILABLE_GRACE, self._grace_expired)

    @callback
    def async_shutdown(self) -> None:
        self._shutdown = True
        if self._cancel_grace is not None:
            self._cancel_grace()
            self._cancel_grace = None

    # -- commands ---------------------------------------------------------------

    @callback
    def async_send(self, dest: int, opcode: int, data: bytes) -> None:
        if self.mesh is not None:
            self.mesh.async_send(dest, opcode, data)

    @callback
    def async_request_status(self, dest: int = DEST_ALL) -> None:
        if self.mesh is not None:
            self.mesh.async_request_status(dest)
