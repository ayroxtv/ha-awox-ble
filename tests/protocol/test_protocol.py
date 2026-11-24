"""Unit tests of the pure protocol library."""

from __future__ import annotations

import pytest

from custom_components.awox_ble.protocol import (
    LoginHandshake,
    MeshAuthError,
    MeshSession,
    ProtocolError,
    convert,
    crypto,
    packets,
)
from custom_components.awox_ble.protocol.constants import PairOpcode

MAC = "a4:c1:38:12:34:56"


def test_mac_reversal() -> None:
    assert crypto.mac_to_reversed_bytes(MAC) == bytes.fromhex("56341238c1a4")
    with pytest.raises(ValueError):
        crypto.mac_to_reversed_bytes("a4:c1")


def test_payload_layout() -> None:
    payload = packets.build_payload(0x0102, 0xD0, b"\x01")
    assert payload == bytes.fromhex("0201d0600101") + b"\x00" * 9
    with pytest.raises(ValueError):
        packets.build_payload(0x10000, 0xD0, b"")
    with pytest.raises(ValueError):
        packets.build_payload(1, 0xD0, b"\x00" * 11)


def test_login_handshake_success_and_failure() -> None:
    hs = LoginHandshake(b"mesh", b"pass", session_random=b"\x01" * 8)
    req = hs.request()
    assert req[0] == PairOpcode.LOGIN_REQUEST
    assert len(req) == 17
    key = hs.complete(bytes([PairOpcode.LOGIN_SUCCESS]) + b"\x02" * 8)
    assert key == crypto.make_session_key(b"mesh", b"pass", b"\x01" * 8, b"\x02" * 8)
    with pytest.raises(MeshAuthError):
        hs.complete(bytes([PairOpcode.LOGIN_FAILURE]) + b"\x00" * 8)
    with pytest.raises(ProtocolError):
        hs.complete(b"\x42")
    with pytest.raises(ProtocolError):
        hs.complete(b"")


def test_credentials_too_long() -> None:
    with pytest.raises(ValueError):
        crypto.make_pair_packet(b"x" * 17, b"", b"\x00" * 8)


def test_sequence_is_monotonic_and_wraps() -> None:
    session = MeshSession(b"\x00" * 16, MAC, seq_start=0xFFFFFE)
    assert session.next_seq() == b"\xff\xff\xff"
    assert session.next_seq() == b"\x01\x00\x00"  # skips 0
    assert session.next_seq() == b"\x02\x00\x00"


def test_session_roundtrip_with_own_decrypt() -> None:
    """A command we encrypt decrypts with the device-side nonce derivation."""
    key = bytes(range(16))
    session = MeshSession(key, MAC, seq_start=10)
    packet = session.encode(3, 0xD0, b"\x01")
    nonce = crypto.command_nonce(MAC, packet[0:3])
    clear = crypto.crypt_payload(key, nonce, packet[5:])
    assert clear == packets.build_payload(3, 0xD0, b"\x01")
    assert crypto.make_checksum(key, nonce, clear)[0:2] == packet[3:5]


def test_parse_ignores_other_reports() -> None:
    assert packets.parse_status(b"\x00" * 7 + b"\xd8" + b"\x00" * 12) is None
    assert packets.parse_status(b"\x00" * 10) is None


@pytest.mark.parametrize("ha", [0, 1, 64, 128, 254, 255])
def test_brightness_roundtrip_close(ha: int) -> None:
    assert abs(convert.white_brightness_to_ha(convert.ha_to_white_brightness(ha)) - ha) <= 2
    assert abs(convert.color_brightness_to_ha(convert.ha_to_color_brightness(ha)) - ha) <= 3


def test_kelvin_direction() -> None:
    assert convert.kelvin_to_white_temperature(6500) == 0
    assert convert.kelvin_to_white_temperature(2700) == 0x7F
    assert convert.white_temperature_to_kelvin(0) == 6500
    assert convert.white_temperature_to_kelvin(0x7F) == 2700
