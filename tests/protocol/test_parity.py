"""Byte-for-byte parity between the new protocol lib and the legacy code."""

from __future__ import annotations

import random
from unittest.mock import patch

import pytest

from custom_components.awox_ble.protocol import crypto, packets
from custom_components.awox_ble.protocol.constants import Opcode

from .legacy import packetutils as legacy
from .legacy.legacy_parse import parse_status_result

MAC = "A4:C1:38:12:34:56"
RNG = random.Random(0xA30C)


def _rand(n: int) -> bytes:
    return bytes(RNG.getrandbits(8) for _ in range(n))


KEYS = [_rand(16) for _ in range(8)]
MACS = [MAC, "A4:C1:38:00:00:01", "11:22:33:44:55:66"]
CREDENTIALS = [
    (b"unpaired", b"1234"),
    (b"a" * 16, b"b" * 16),
    (b"Zx8kq2P", b"9f3c0d2e1a4b5c6d"),
]


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("length", [0, 1, 8, 15, 16])
def test_encrypt_parity(key: bytes, length: int) -> None:
    value = _rand(length)
    assert crypto.telink_encrypt(key, value) == bytes(legacy.encrypt(key, value))


@pytest.mark.parametrize(("name", "password"), CREDENTIALS)
def test_pair_packet_and_session_key_parity(name: bytes, password: bytes) -> None:
    session_random = _rand(8)
    response_random = _rand(8)
    assert crypto.make_pair_packet(name, password, session_random) == bytes(
        legacy.make_pair_packet(name, password, session_random)
    )
    assert crypto.make_session_key(name, password, session_random, response_random) == bytes(
        legacy.make_session_key(name, password, session_random, response_random)
    )


COMMANDS = [
    (Opcode.POWER, packets.power_data(True)),
    (Opcode.POWER, packets.power_data(False)),
    (Opcode.COLOR, packets.color_data(255, 10, 128)),
    (Opcode.COLOR_BRIGHTNESS, packets.byte_data(0x40)),
    (Opcode.WHITE_BRIGHTNESS, packets.byte_data(0x7F)),
    (Opcode.WHITE_TEMPERATURE, packets.byte_data(0x00)),
    (Opcode.REQUEST_STATUS, packets.request_status_data()),
    (Opcode.SEQUENCE_FADE_DURATION, packets.duration_data(1500)),
    (Opcode.PRESET, packets.byte_data(3)),
]


@pytest.mark.parametrize("key", KEYS[:3])
@pytest.mark.parametrize("mac", MACS)
@pytest.mark.parametrize("dest", [0, 1, 0x1234, 0xFFFF])
@pytest.mark.parametrize(("opcode", "data"), COMMANDS)
def test_command_packet_parity(key: bytes, mac: str, dest: int, opcode: int, data: bytes) -> None:
    seq = _rand(3)
    with patch.object(legacy, "urandom", return_value=seq):
        expected = bytes(legacy.make_command_packet(key, mac, dest, opcode, data))
    session = packets.MeshSession(key, mac)
    assert session.encode(dest, opcode, data, seq=seq) == expected
    assert len(expected) == 20


def _device_side_packet(key: bytes, mac: str, header5: bytes, payload: bytes) -> bytes:
    """Encrypt a packet the way a light does (independent re-implementation)."""
    nonce = crypto.mac_to_reversed_bytes(mac)[0:3] + header5
    check = legacy.make_checksum(key, nonce, payload)
    return header5 + bytes(check[0:2]) + bytes(legacy.crypt_payload(key, nonce, payload))


def _status_payload(opcode: int, body: bytes) -> bytes:
    # payload bytes = packet[7:20]: opcode, vendor(2), body(10)
    return (bytes([opcode]) + b"\x60\x01" + body).ljust(13, b"\x00")


@pytest.mark.parametrize("key", KEYS[:4])
@pytest.mark.parametrize("mac", MACS)
def test_decrypt_and_parse_parity_status(key: bytes, mac: str) -> None:
    header = _rand(3) + bytes([0x05, 0x00])  # source mesh id 5
    body = bytes([0x03, 0x40, 0x20, 0x50, 0xFF, 0x80, 0x00, 0, 0, 0])
    packet = _device_side_packet(key, mac, header, _status_payload(Opcode.STATUS_REPORT, body))

    old = legacy.decrypt_packet(key, mac, packet)
    new = crypto.decrypt_packet(key, mac, packet)
    assert old is not None
    assert new == bytes(old)

    old_status = parse_status_result(old)
    new_status = packets.parse_status(new)
    assert new_status is not None
    assert old_status["type"] == new_status.source == "status"
    for field in (
        "mesh_id",
        "state",
        "color_mode",
        "red",
        "green",
        "blue",
        "white_temperature",
        "white_brightness",
        "color_brightness",
    ):
        assert old_status[field] == getattr(new_status, field), field
    assert old_status["transition_mode"] == new_status.sequence_mode


@pytest.mark.parametrize("key", KEYS[:4])
def test_decrypt_and_parse_parity_notification(key: bytes) -> None:
    header = _rand(3) + bytes([0x02, 0x00])
    # [10]=mesh id low, [11]=online, [12]=mode, [13..18]=wb temp cb r g b, [19]=mesh id high
    body = bytes([0x07, 0x01, 0x01, 0x7F, 0x10, 0x64, 1, 2, 3, 0x01])
    packet = _device_side_packet(
        key, MAC, header, _status_payload(Opcode.ONLINE_STATUS_REPORT, body)
    )
    old = legacy.decrypt_packet(key, MAC, packet)
    new = crypto.decrypt_packet(key, MAC, packet)
    assert old is not None
    assert new == bytes(old)
    old_status = parse_status_result(old)
    new_status = packets.parse_status(new)
    assert new_status is not None
    assert new_status.mesh_id == old_status["mesh_id"] == 0x0107
    assert new_status.online
    assert new_status.state == old_status["state"]
    assert (new_status.red, new_status.green, new_status.blue) == (1, 2, 3)


def test_bad_checksum_rejected_like_legacy() -> None:
    key = KEYS[0]
    header = _rand(5)
    packet = bytearray(_device_side_packet(key, MAC, header, _status_payload(0xDB, b"\x01" * 10)))
    packet[6] ^= 0xFF
    assert legacy.decrypt_packet(key, MAC, bytes(packet)) is None
    assert crypto.decrypt_packet(key, MAC, bytes(packet)) is None


def test_crc16_parity() -> None:
    data = _rand(64)
    assert crypto.crc16(data) == legacy.crc16(data)
