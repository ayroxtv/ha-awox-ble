"""Persistent connection to an AwoX Telink mesh through the HA Bluetooth stack.

One ``MeshManager`` per mesh keeps a single GATT link to the best reachable
node (any node relays to the whole mesh), logs in, listens to notifications
and drains a command queue. All of it runs in the event loop: no thread, no
subprocess, no blocking call.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakOutOfConnectionSlotsError,
    establish_connection,
)
from homeassistant.components import bluetooth
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .const import (
    COMMAND_MIN_GAP,
    COMMAND_TTL,
    COMMAND_WRITE_TIMEOUT,
    COMMAND_WRITE_WITH_RESPONSE,
    CONNECT_MAX_ATTEMPTS,
    DISCONNECT_TIMEOUT,
    IDLE_WAIT_FOR_ADVERTISEMENT,
    KEEPALIVE_INTERVAL,
    KEEPALIVE_TIMEOUT,
    LOGIN_TIMEOUT,
    NODE_AUTH_FAILURE_COOLDOWN,
    NODE_FAILURE_COOLDOWN,
    RECONNECT_BACKOFF_MAX,
    RECONNECT_BACKOFF_MIN,
)
from .protocol import DEST_ALL, LightStatus, LoginHandshake, MeshAuthError, MeshSession, Opcode
from .protocol.constants import (
    COMMAND_CHAR_UUID,
    ENABLE_NOTIFICATIONS,
    PAIR_CHAR_UUID,
    STATUS_CHAR_UUID,
)
from .protocol.crypto import ProtocolError
from .protocol.packets import packet_opcode, packet_source, parse_status, request_status_data

_LOGGER = logging.getLogger(__name__)

StatusCallback = Callable[[LightStatus], None]
ConnectionCallback = Callable[[bool], None]


@dataclass(slots=True)
class QueuedCommand:
    dest: int
    opcode: int
    data: bytes
    created: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class MeshStats:
    """Counters exposed in diagnostics."""

    connect_attempts: int = 0
    connections: int = 0
    disconnections: int = 0
    auth_failures: int = 0
    slot_exhausted: int = 0
    commands_sent: int = 0
    commands_dropped: int = 0
    commands_coalesced: int = 0
    notifications: int = 0
    bad_packets: int = 0
    last_connect_seconds: float | None = None
    last_reconnect_gap_seconds: float | None = None
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__slots__}


class SessionEnded(Exception):
    """The current GATT session is over (disconnect, keepalive or write failure)."""


class MeshManager:
    """Connection, login, notifications, command queue and reconnection."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        mesh_name: str,
        mesh_password: str,
        node_macs: list[str],
        on_status: StatusCallback,
        on_connection_change: ConnectionCallback,
        label: str = "awox",
    ) -> None:
        self._hass = hass
        self._mesh_name = mesh_name.encode()
        self._mesh_password = mesh_password.encode()
        self._label = label
        self._on_status = on_status
        self._on_connection_change = on_connection_change
        self._known_macs: set[str] = {m.upper() for m in node_macs if m}
        self._seen_macs: set[str] = set()
        # Soft penalty (ranked last) after a failure; hard ban after an auth failure.
        self._node_penalty_until: dict[str, float] = {}
        self._node_banned_until: dict[str, float] = {}

        self._queue: deque[QueuedCommand] = deque()
        self._queue_event = asyncio.Event()
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._unsubs: list[CALLBACK_TYPE] = []

        self._client: BleakClient | None = None
        self._session: MeshSession | None = None
        self.gateway_mac: str | None = None
        self.gateway_source: str | None = None
        self._last_activity = 0.0
        self._disconnected_at: float | None = None
        self.stats = MeshStats()

    # ------------------------------------------------------------------ API

    @property
    def connected(self) -> bool:
        return self._session is not None

    @property
    def known_macs(self) -> set[str]:
        return self._known_macs | self._seen_macs

    @callback
    def async_start(self, create_task: Callable[..., asyncio.Task[None]]) -> None:
        """Register advertisement callbacks and start the connection loop."""
        for mac in self._known_macs:
            self._unsubs.append(
                bluetooth.async_register_callback(
                    self._hass,
                    self._async_on_advertisement,
                    bluetooth.BluetoothCallbackMatcher(address=mac, connectable=True),
                    bluetooth.BluetoothScanningMode.PASSIVE,
                )
            )
        name = self._mesh_name.decode(errors="ignore")
        if name and not any(c in name for c in "*?[]"):
            # Telink nodes advertise the mesh name: lets us use nodes we were not told about.
            self._unsubs.append(
                bluetooth.async_register_callback(
                    self._hass,
                    self._async_on_advertisement,
                    bluetooth.BluetoothCallbackMatcher(local_name=name, connectable=True),
                    bluetooth.BluetoothScanningMode.PASSIVE,
                )
            )
        self._task = create_task(self._async_run(), f"{self._label} mesh connection")

    async def async_stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._async_disconnect()

    @callback
    def async_send(self, dest: int, opcode: int, data: bytes) -> None:
        """Queue a command. Never blocks: returns immediately.

        A newer command with the same destination and opcode replaces a still
        queued one (slider drags do not pile up).
        """
        for queued in self._queue:
            if queued.dest == dest and queued.opcode == opcode:
                queued.data = data
                queued.created = time.monotonic()
                self.stats.commands_coalesced += 1
                break
        else:
            self._queue.append(QueuedCommand(dest, opcode, data))
        self._queue_event.set()
        if not self.connected:
            self._wake.set()

    @callback
    def async_request_status(self, dest: int = DEST_ALL) -> None:
        self.async_send(dest, Opcode.REQUEST_STATUS, request_status_data())

    # ------------------------------------------------------- advertisements

    @callback
    def _async_on_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        mac = service_info.address.upper()
        if mac not in self._known_macs and mac not in self._seen_macs:
            _LOGGER.debug("[%s] new mesh node seen: %s", self._label, mac)
            self._seen_macs.add(mac)
        if not self.connected:
            self._wake.set()

    def _candidates(self) -> list[tuple[str, BLEDevice, int, str]]:
        """Connectable nodes sorted by best RSSI over all scanners/proxies."""
        now = time.monotonic()
        result: list[tuple[str, BLEDevice, int, str]] = []
        for mac in self.known_macs:
            if self._node_banned_until.get(mac, 0.0) > now:
                continue
            info = bluetooth.async_last_service_info(self._hass, mac, connectable=True)
            if info is None:
                continue
            device = bluetooth.async_ble_device_from_address(self._hass, mac, connectable=True)
            if device is None:
                continue
            result.append((mac, device, info.rssi, info.source))
        result.sort(key=lambda c: (self._node_penalty_until.get(c[0], 0.0) > now, -c[2]))
        return result

    # ------------------------------------------------------- connection loop

    async def _async_run(self) -> None:
        backoff = RECONNECT_BACKOFF_MIN
        while not self._stopping.is_set():
            candidates = self._candidates()
            if not candidates:
                _LOGGER.debug("[%s] no connectable mesh node in range, waiting", self._label)
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(IDLE_WAIT_FOR_ADVERTISEMENT):
                        await self._wake.wait()
                continue

            for mac, device, rssi, source in candidates:
                if self._stopping.is_set():
                    return
                started = time.monotonic()
                try:
                    await self._async_session(mac, device, rssi, source)
                except asyncio.CancelledError:
                    raise
                except MeshAuthError:
                    self.stats.auth_failures += 1
                    self.stats.last_error = "mesh credentials refused"
                    _LOGGER.error(  # noqa: TRY400 - no traceback wanted
                        "[%s] node %s refused the mesh credentials (was it re-paired with "
                        "another AwoX account?); skipping it for %.0f s",
                        self._label,
                        mac,
                        NODE_AUTH_FAILURE_COOLDOWN,
                    )
                    self._node_banned_until[mac] = time.monotonic() + NODE_AUTH_FAILURE_COOLDOWN
                    continue
                except BleakOutOfConnectionSlotsError as err:
                    self.stats.slot_exhausted += 1
                    self.stats.last_error = f"no free connection slot: {err}"
                    _LOGGER.warning(
                        "[%s] no free Bluetooth connection slot to reach %s (ESPHome proxies "
                        "usually allow 3 active connections); trying another node",
                        self._label,
                        mac,
                    )
                    self._node_penalty_until[mac] = time.monotonic() + NODE_FAILURE_COOLDOWN
                    continue
                except (BleakError, TimeoutError, ProtocolError, SessionEnded, OSError) as err:
                    self.stats.last_error = f"{type(err).__name__}: {err}"
                    _LOGGER.debug(
                        "[%s] session with %s ended: %s: %s",
                        self._label,
                        mac,
                        type(err).__name__,
                        err,
                    )
                    if not self._session_was_useful(started):
                        self._node_penalty_until[mac] = time.monotonic() + NODE_FAILURE_COOLDOWN
                        continue
                if self._session_was_useful(started):
                    backoff = RECONNECT_BACKOFF_MIN
                break  # after any established session, re-rank candidates

            if self._stopping.is_set():
                return
            _LOGGER.debug("[%s] reconnecting in %.1f s", self._label, backoff)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(backoff):
                    await self._stopping.wait()
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)

    def _session_was_useful(self, started: float) -> bool:
        """A session that logged in counts as success for the backoff."""
        return self._last_activity >= started and self._last_activity > 0

    async def _async_session(self, mac: str, device: BLEDevice, rssi: int, source: str) -> None:
        self.stats.connect_attempts += 1
        loop = asyncio.get_running_loop()
        disconnected = asyncio.Event()

        def _on_disconnect(_client: BleakClient) -> None:
            loop.call_soon_threadsafe(disconnected.set)

        _LOGGER.debug("[%s] connecting to %s via %s (rssi %s dBm)", self._label, mac, source, rssi)
        t0 = time.monotonic()
        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            f"{self._label} {mac}",
            disconnected_callback=_on_disconnect,
            max_attempts=CONNECT_MAX_ATTEMPTS,
            ble_device_callback=lambda: (
                bluetooth.async_ble_device_from_address(self._hass, mac, connectable=True) or device
            ),
        )
        self._client = client
        try:
            async with asyncio.timeout(LOGIN_TIMEOUT):
                session = await self._async_login(client, mac)
                await client.start_notify(STATUS_CHAR_UUID, self._on_notify)
                await client.write_gatt_char(STATUS_CHAR_UUID, ENABLE_NOTIFICATIONS, response=True)
            elapsed = time.monotonic() - t0
            self.stats.connections += 1
            self.stats.last_connect_seconds = round(elapsed, 3)
            if self._disconnected_at is not None:
                self.stats.last_reconnect_gap_seconds = round(
                    time.monotonic() - self._disconnected_at, 3
                )
            self._session = session
            self.gateway_mac = mac
            self.gateway_source = source
            self._last_activity = time.monotonic()
            self._node_penalty_until.pop(mac, None)
            _LOGGER.info(
                "[%s] connected to mesh through %s via %s in %.2f s",
                self._label,
                mac,
                source,
                elapsed,
            )
            self._on_connection_change(True)
            # The only "poll": one status request right after connecting.
            self._queue.appendleft(
                QueuedCommand(DEST_ALL, Opcode.REQUEST_STATUS, request_status_data())
            )
            self._queue_event.set()
            await self._async_serve(client, session, disconnected)
        finally:
            was_connected = self._session is not None
            self._session = None
            self._client = None
            self.gateway_mac = None
            self.gateway_source = None
            if was_connected:
                self.stats.disconnections += 1
                self._disconnected_at = time.monotonic()
                _LOGGER.info("[%s] mesh link through %s lost", self._label, mac)
                self._on_connection_change(False)
            with contextlib.suppress(BleakError, TimeoutError, OSError):
                async with asyncio.timeout(DISCONNECT_TIMEOUT):
                    await client.disconnect()

    async def _async_login(self, client: BleakClient, mac: str) -> MeshSession:
        handshake = LoginHandshake(self._mesh_name, self._mesh_password)
        await client.write_gatt_char(PAIR_CHAR_UUID, handshake.request(), response=True)
        reply = await client.read_gatt_char(PAIR_CHAR_UUID)
        return MeshSession(handshake.complete(bytes(reply)), mac)

    async def _async_serve(
        self, client: BleakClient, session: MeshSession, disconnected: asyncio.Event
    ) -> None:
        tasks = [
            asyncio.create_task(self._async_writer(client, session), name="awox writer"),
            asyncio.create_task(self._async_keepalive(client), name="awox keepalive"),
            asyncio.create_task(disconnected.wait(), name="awox disconnected"),
            asyncio.create_task(self._stopping.wait(), name="awox stopping"),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in done:
            if not task.cancelled() and (exc := task.exception()) is not None:
                raise exc
        if disconnected.is_set():
            raise SessionEnded("device disconnected")

    async def _async_writer(self, client: BleakClient, session: MeshSession) -> None:
        last_write = 0.0
        while True:
            if not self._queue:
                self._queue_event.clear()
                await self._queue_event.wait()
                continue
            cmd = self._queue[0]
            if time.monotonic() - cmd.created > COMMAND_TTL:
                self._queue.popleft()
                self.stats.commands_dropped += 1
                _LOGGER.debug("[%s] dropped stale command 0x%02x", self._label, cmd.opcode)
                continue
            gap = COMMAND_MIN_GAP - (time.monotonic() - last_write)
            if gap > 0:
                await asyncio.sleep(gap)
            packet = session.encode(cmd.dest, cmd.opcode, cmd.data)
            t_write = time.monotonic()
            try:
                async with asyncio.timeout(COMMAND_WRITE_TIMEOUT):
                    await client.write_gatt_char(
                        COMMAND_CHAR_UUID, packet, response=COMMAND_WRITE_WITH_RESPONSE
                    )
            except (BleakError, TimeoutError) as err:
                # Keep the command queued: it is resent after reconnection if still fresh.
                raise SessionEnded(f"command write failed: {err}") from err
            if self._queue and self._queue[0] is cmd:
                self._queue.popleft()
            last_write = self._last_activity = time.monotonic()
            self.stats.commands_sent += 1
            _LOGGER.debug(
                "[%s] sent 0x%02x to %d in %.0f ms (queued for %.0f ms)",
                self._label,
                cmd.opcode,
                cmd.dest,
                (last_write - t_write) * 1000,
                (t_write - cmd.created) * 1000,
            )

    async def _async_keepalive(self, client: BleakClient) -> None:
        """Detect zombie links: a cheap GATT read when the link has been idle."""
        while True:
            idle = time.monotonic() - self._last_activity
            if idle < KEEPALIVE_INTERVAL:
                await asyncio.sleep(KEEPALIVE_INTERVAL - idle)
                continue
            try:
                async with asyncio.timeout(KEEPALIVE_TIMEOUT):
                    await client.read_gatt_char(STATUS_CHAR_UUID)
            except (BleakError, TimeoutError) as err:
                raise SessionEnded(f"keepalive failed: {err}") from err
            self._last_activity = time.monotonic()

    @callback
    def _on_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        session = self._session
        if session is None:
            return
        self._last_activity = time.monotonic()
        clear = session.decode(bytes(data))
        if clear is None:
            self.stats.bad_packets += 1
            _LOGGER.debug("[%s] dropped notification with bad checksum", self._label)
            return
        self.stats.notifications += 1
        status = parse_status(clear)
        if status is None:
            _LOGGER.debug(
                "[%s] report 0x%02x from %d: %s",
                self._label,
                packet_opcode(clear) or 0,
                packet_source(clear),
                clear[7:].hex(),
            )
            return
        self._on_status(status)

    async def _async_disconnect(self) -> None:
        client = self._client
        if client is None:
            return
        with contextlib.suppress(BleakError, TimeoutError, OSError):
            async with asyncio.timeout(DISCONNECT_TIMEOUT):
                await client.disconnect()

    def diagnostics(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "gateway_mac": self.gateway_mac,
            "gateway_source": self.gateway_source,
            "known_macs": sorted(self._known_macs),
            "seen_macs": sorted(self._seen_macs),
            "queued_commands": len(self._queue),
            "nodes_penalized": sorted(
                mac for mac, until in self._node_penalty_until.items() if until > time.monotonic()
            ),
            "nodes_banned": sorted(
                mac for mac, until in self._node_banned_until.items() if until > time.monotonic()
            ),
            "stats": self.stats.as_dict(),
        }


class NodeNotFoundError(Exception):
    """No connectable advertisement for this MAC in any scanner."""


async def async_probe_mesh(
    hass: HomeAssistant, mac: str, mesh_name: str, mesh_password: str, listen: float = 3.0
) -> set[int]:
    """Connect once, log in and collect the mesh ids that answer a status request.

    Used by the config flow (manual setup). Raises NodeNotFoundError,
    MeshAuthError, BleakError or TimeoutError.
    """
    device = bluetooth.async_ble_device_from_address(hass, mac.upper(), connectable=True)
    if device is None:
        raise NodeNotFoundError(mac)
    client = await establish_connection(
        BleakClientWithServiceCache, device, f"awox probe {mac}", max_attempts=CONNECT_MAX_ATTEMPTS
    )
    mesh_ids: set[int] = set()
    try:
        async with asyncio.timeout(LOGIN_TIMEOUT):
            handshake = LoginHandshake(mesh_name.encode(), mesh_password.encode())
            await client.write_gatt_char(PAIR_CHAR_UUID, handshake.request(), response=True)
            reply = await client.read_gatt_char(PAIR_CHAR_UUID)
            session = MeshSession(handshake.complete(bytes(reply)), mac.upper())

            def _collect(_char: BleakGATTCharacteristic, data: bytearray) -> None:
                clear = session.decode(bytes(data))
                if clear is not None and (status := parse_status(clear)) is not None:
                    mesh_ids.add(status.mesh_id)

            await client.start_notify(STATUS_CHAR_UUID, _collect)
            await client.write_gatt_char(STATUS_CHAR_UUID, ENABLE_NOTIFICATIONS, response=True)
            await client.write_gatt_char(
                COMMAND_CHAR_UUID,
                session.encode(DEST_ALL, Opcode.REQUEST_STATUS, request_status_data()),
                response=True,
            )
        await asyncio.sleep(listen)
    finally:
        with contextlib.suppress(BleakError, TimeoutError, OSError):
            async with asyncio.timeout(DISCONNECT_TIMEOUT):
                await client.disconnect()
    return mesh_ids
