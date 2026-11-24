"""Command building and report parsing (pure, no I/O)."""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import Literal

from .constants import (
    PAYLOAD_LEN,
    SEQ_LEN,
    VENDOR_CODE,
    Opcode,
)
from .crypto import (
    decrypt_packet,
    encrypt_command,
    make_pair_packet,
    session_key_from_pair_response,
)


def build_payload(dest: int, opcode: int, data: bytes) -> bytes:
    """Clear-text payload: dest(2, LE) + opcode + vendor(2) + data, padded to 15."""
    if not 0 <= dest <= 0xFFFF:
        raise ValueError("dest out of range")
    if len(data) > PAYLOAD_LEN - 5:
        raise ValueError("command data holds at most 10 bytes")
    return (struct.pack("<H", dest) + bytes([opcode]) + VENDOR_CODE + data).ljust(
        PAYLOAD_LEN, b"\x00"
    )


# --- command data helpers -------------------------------------------------


def power_data(on: bool) -> bytes:
    return b"\x01" if on else b"\x00"


def color_data(red: int, green: int, blue: int) -> bytes:
    return struct.pack("BBBB", 0x04, red, green, blue)


def byte_data(value: int) -> bytes:
    return struct.pack("B", value)


def request_status_data() -> bytes:
    return b"\x10"


def duration_data(milliseconds: int) -> bytes:
    return struct.pack("<I", milliseconds)


# --- reports ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LightStatus:
    """State of one mesh node, from a 0xDB or 0xDC report."""

    mesh_id: int
    source: Literal["status", "notification"]
    online: bool
    state: bool
    color_mode: bool
    sequence_mode: bool
    candle_mode: bool
    white_brightness: int
    white_temperature: int
    color_brightness: int
    red: int
    green: int
    blue: int


def _mode_flags(mode: int) -> tuple[bool, bool, bool, bool]:
    return (
        bool(mode & 0x01),
        bool((mode >> 1) & 0x01),
        bool((mode >> 2) & 0x01),
        bool((mode >> 4) & 0x01),
    )


def parse_status(packet: bytes) -> LightStatus | None:
    """Parse a *decrypted* 20 byte packet. Returns None for other reports."""
    if len(packet) < 20:
        return None
    opcode = packet[7]
    if opcode == Opcode.STATUS_REPORT:
        mode = packet[10]
        state, color_mode, seq_mode, candle = _mode_flags(mode)
        return LightStatus(
            mesh_id=packet[4] * 256 + packet[3],
            source="status",
            online=True,
            state=state,
            color_mode=color_mode,
            sequence_mode=seq_mode,
            candle_mode=candle,
            white_brightness=packet[11],
            white_temperature=packet[12],
            color_brightness=packet[13],
            red=packet[14],
            green=packet[15],
            blue=packet[16],
        )
    if opcode == Opcode.ONLINE_STATUS_REPORT:
        mode = packet[12]
        state, color_mode, seq_mode, candle = _mode_flags(mode)
        return LightStatus(
            mesh_id=packet[19] * 256 + packet[10],
            source="notification",
            online=packet[11] != 0,
            state=state,
            color_mode=color_mode,
            sequence_mode=seq_mode,
            candle_mode=candle,
            white_brightness=packet[13],
            white_temperature=packet[14],
            color_brightness=packet[15],
            red=packet[16],
            green=packet[17],
            blue=packet[18],
        )
    return None


def packet_opcode(packet: bytes) -> int | None:
    return packet[7] if len(packet) > 7 else None


def packet_source(packet: bytes) -> int:
    return packet[4] * 256 + packet[3]


# --- session ---------------------------------------------------------------


class LoginHandshake:
    """Client side of the Telink login exchange on the pair characteristic."""

    def __init__(
        self, mesh_name: bytes, mesh_password: bytes, session_random: bytes | None = None
    ) -> None:
        self._name = mesh_name
        self._password = mesh_password
        self.session_random = session_random if session_random is not None else os.urandom(8)

    def request(self) -> bytes:
        return make_pair_packet(self._name, self._password, self.session_random)

    def complete(self, response: bytes) -> bytes:
        """Return the session key or raise MeshAuthError / ProtocolError."""
        return session_key_from_pair_response(
            self._name, self._password, self.session_random, bytes(response)
        )


class MeshSession:
    """Encrypts commands and decrypts notifications for one gateway link.

    ``mac`` is the MAC address of the node we are connected to (the nonce
    uses it, whatever the command destination).
    """

    def __init__(self, session_key: bytes, mac: str, seq_start: int | None = None) -> None:
        self.session_key = session_key
        self.mac = mac
        if seq_start is None:
            seq_start = int.from_bytes(os.urandom(SEQ_LEN), "little")
        self._seq = seq_start & 0xFFFFFF

    def next_seq(self) -> bytes:
        """Monotonic 24 bit counter (random start so mesh nodes never see a replay)."""
        self._seq = (self._seq + 1) & 0xFFFFFF or 1
        return self._seq.to_bytes(SEQ_LEN, "little")

    def encode(self, dest: int, opcode: int, data: bytes, seq: bytes | None = None) -> bytes:
        payload = build_payload(dest, opcode, data)
        return encrypt_command(self.session_key, self.mac, seq or self.next_seq(), payload)

    def decode(self, packet: bytes) -> bytes | None:
        return decrypt_packet(self.session_key, self.mac, bytes(packet))
