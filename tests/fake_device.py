"""In-memory Telink mesh node that speaks the real (encrypted) protocol."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from custom_components.awox_ble.protocol import crypto
from custom_components.awox_ble.protocol.constants import (
    COMMAND_CHAR_UUID,
    PAIR_CHAR_UUID,
    STATUS_CHAR_UUID,
    Opcode,
    PairOpcode,
)

MESH_NAME = "testmesh"
MESH_PASSWORD = "secret123"


class FakeLight:
    """State of one mesh node."""

    def __init__(self, mesh_id: int) -> None:
        self.mesh_id = mesh_id
        self.on = False
        self.color_mode = False
        self.white_brightness = 0x40
        self.white_temperature = 0x20
        self.color_brightness = 0x30
        self.rgb = (1, 2, 3)

    def status_body(self) -> bytes:
        mode = int(self.on) | (int(self.color_mode) << 1)
        return bytes(
            [
                self.mesh_id & 0xFF,
                0x01,
                mode,
                self.white_brightness,
                self.white_temperature,
                self.color_brightness,
                *self.rgb,
                self.mesh_id >> 8,
            ]
        )


class FakeMeshClient:
    """Mimics the subset of BleakClient used by MeshManager."""

    def __init__(
        self,
        mac: str,
        lights: dict[int, FakeLight],
        *,
        mesh_name: str = MESH_NAME,
        mesh_password: str = MESH_PASSWORD,
        disconnected_callback: Callable[[Any], None] | None = None,
        notify_delay: float = 0.0,
    ) -> None:
        self.mac = mac
        self.lights = lights
        self._name = mesh_name.encode()
        self._password = mesh_password.encode()
        self._disconnected_callback = disconnected_callback
        self._notify_cb: Callable[[Any, bytearray], None] | None = None
        self._session_random = b""
        self._response_random = os.urandom(8)
        self.session_key: bytes | None = None
        self.notifications_enabled = False
        self.connected = True
        self.written: list[tuple[str, bytes]] = []
        self.commands: list[bytes] = []  # decrypted payloads
        self.fail_writes = False
        self.notify_delay = notify_delay
        self._seq = 0

    # -- BleakClient API -------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def write_gatt_char(self, char: str, data: bytes, response: bool | None = None) -> None:
        if not self.connected or self.fail_writes:
            raise BleakError("not connected")
        data = bytes(data)
        self.written.append((char, data))
        if char == PAIR_CHAR_UUID and data[0] == PairOpcode.LOGIN_REQUEST:
            self._session_random = data[1:9]
            expected = crypto.make_pair_packet(self._name, self._password, self._session_random)
            if expected != data:
                self.session_key = None
                return
            self.session_key = crypto.make_session_key(
                self._name, self._password, self._session_random, self._response_random
            )
        elif char == STATUS_CHAR_UUID and data == b"\x01":
            self.notifications_enabled = True
        elif char == COMMAND_CHAR_UUID:
            self._handle_command(data)

    async def read_gatt_char(self, char: str, **_: Any) -> bytearray:
        if not self.connected:
            raise BleakError("not connected")
        if char == PAIR_CHAR_UUID:
            if self.session_key is None:
                return bytearray(bytes([PairOpcode.LOGIN_FAILURE]) + b"\x00" * 16)
            return bytearray(
                bytes([PairOpcode.LOGIN_SUCCESS]) + self._response_random + b"\x00" * 8
            )
        return bytearray(20)

    async def start_notify(self, char: str, cb: Callable[[Any, bytearray], None]) -> None:
        assert char == STATUS_CHAR_UUID
        self._notify_cb = cb

    async def disconnect(self) -> bool:
        self.connected = False
        return True

    # -- simulation ---------------------------------------------------------

    def drop(self) -> None:
        """Simulate the link going away."""
        self.connected = False
        if self._disconnected_callback:
            self._disconnected_callback(self)

    def _handle_command(self, packet: bytes) -> None:
        assert self.session_key is not None
        nonce = crypto.command_nonce(self.mac, packet[0:3])
        payload = crypto.crypt_payload(self.session_key, nonce, packet[5:])
        assert crypto.make_checksum(self.session_key, nonce, payload)[0:2] == packet[3:5]
        self.commands.append(payload)
        dest = payload[0] | payload[1] << 8
        opcode, data = payload[2], payload[5:]
        targets = list(self.lights.values()) if dest == 0xFFFF else [self.lights[dest]]
        for light in targets:
            if opcode == Opcode.POWER:
                light.on = bool(data[0])
            elif opcode == Opcode.COLOR:
                light.color_mode, light.rgb, light.on = True, (data[1], data[2], data[3]), True
            elif opcode == Opcode.WHITE_BRIGHTNESS:
                light.white_brightness, light.color_mode, light.on = data[0], False, True
            elif opcode == Opcode.COLOR_BRIGHTNESS:
                light.color_brightness, light.on = data[0], True
            elif opcode == Opcode.WHITE_TEMPERATURE:
                light.white_temperature, light.color_mode, light.on = data[0], False, True
            if opcode == Opcode.REQUEST_STATUS:
                self.emit_status_report(light)
            else:
                self.emit_online_status(light)

    def _emit(self, src: int, opcode: int, body: bytes) -> None:
        if not self.notifications_enabled or self._notify_cb is None or self.session_key is None:
            return
        self._seq += 1
        header = self._seq.to_bytes(3, "little") + src.to_bytes(2, "little")
        nonce = crypto.mac_to_reversed_bytes(self.mac)[0:3] + header
        payload = (bytes([opcode]) + b"\x60\x01" + body).ljust(13, b"\x00")
        check = crypto.make_checksum(self.session_key, nonce, payload)
        packet = header + check[0:2] + crypto.crypt_payload(self.session_key, nonce, payload)
        cb = self._notify_cb
        loop = asyncio.get_running_loop()
        if self.notify_delay:
            loop.call_later(self.notify_delay, cb, None, bytearray(packet))
        else:
            loop.call_soon(cb, None, bytearray(packet))

    def emit_online_status(self, light: FakeLight) -> None:
        self._emit(0, Opcode.ONLINE_STATUS_REPORT, light.status_body())

    def emit_status_report(self, light: FakeLight) -> None:
        body = light.status_body()
        # 0xDB layout: [10]=mode, [11..16]=wb temp cb r g b
        self._emit(light.mesh_id, Opcode.STATUS_REPORT, bytes([body[2], *body[3:9]]))


def ble_device(mac: str) -> BLEDevice:
    return BLEDevice(mac, "EPanel", {"path": "/org/bluez/hci0"})
