"""Telink mesh cryptography (pure functions, no I/O).

Straight port of ``packetutils.py`` from python-awox-mesh-light
(c) 2017 Leiaz, MIT licensed. The algorithms are unchanged; only the AES
backend moved from pycryptodome to ``cryptography`` (shipped with Home
Assistant core) and the code is typed.

Telink "AES" is plain AES-128-ECB applied to *byte-reversed* key and data,
with the output reversed again.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .constants import MESH_CREDENTIAL_MAX_LEN, PACKET_LEN, PairOpcode

BLOCK = 16


class ProtocolError(Exception):
    """Base error of the protocol library."""


class MeshAuthError(ProtocolError):
    """The device refused the mesh credentials."""


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b, strict=False))


def telink_encrypt(key: bytes, value: bytes) -> bytes:
    """Encrypt one block the Telink way (reverse, AES-ECB, reverse)."""
    if len(key) != BLOCK:
        raise ValueError("key must be 16 bytes")
    if len(value) > BLOCK:
        raise ValueError("value must be at most 16 bytes")
    block = value.ljust(BLOCK, b"\x00")[::-1]
    encryptor = Cipher(algorithms.AES(key[::-1]), modes.ECB()).encryptor()  # noqa: S305
    return (encryptor.update(block) + encryptor.finalize())[::-1]


def make_checksum(key: bytes, nonce: bytes, payload: bytes) -> bytes:
    """CBC-MAC like checksum used to authenticate a packet payload."""
    base = (nonce + bytes([len(payload)])).ljust(BLOCK, b"\x00")
    check = telink_encrypt(key, base)
    for i in range(0, len(payload), BLOCK):
        chunk = payload[i : i + BLOCK].ljust(BLOCK, b"\x00")
        check = telink_encrypt(key, _xor(check, chunk))
    return check


def crypt_payload(key: bytes, nonce: bytes, payload: bytes) -> bytes:
    """CTR-like stream cipher; the same function encrypts and decrypts."""
    base = bytearray((b"\x00" + nonce).ljust(BLOCK, b"\x00"))
    out = bytearray()
    for i in range(0, len(payload), BLOCK):
        stream = telink_encrypt(key, bytes(base))
        out += _xor(stream, payload[i : i + BLOCK])
        base[0] = (base[0] + 1) & 0xFF
    return bytes(out)


def mac_to_reversed_bytes(mac: str) -> bytes:
    """``AA:BB:CC:DD:EE:FF`` -> ``b'\\xff\\xee\\xdd\\xcc\\xbb\\xaa'``."""
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError(f"invalid MAC address: {mac!r}")
    return raw[::-1]


def command_nonce(mac: str, seq: bytes) -> bytes:
    """Nonce of an outgoing command: reversed MAC[0:4] + 0x01 + sequence."""
    return mac_to_reversed_bytes(mac)[0:4] + b"\x01" + seq


def notification_nonce(mac: str, packet: bytes) -> bytes:
    """Nonce of an incoming packet: reversed MAC[0:3] + packet[0:5]."""
    return mac_to_reversed_bytes(mac)[0:3] + packet[0:5]


def encrypt_command(key: bytes, mac: str, seq: bytes, payload: bytes) -> bytes:
    """Build the 20 byte encrypted packet: seq(3) + checksum(2) + payload(15)."""
    nonce = command_nonce(mac, seq)
    check = make_checksum(key, nonce, payload)
    return seq + check[0:2] + crypt_payload(key, nonce, payload)


def decrypt_packet(key: bytes, mac: str, packet: bytes) -> bytes | None:
    """Decrypt a notification. Returns ``None`` when the checksum mismatches."""
    if len(packet) != PACKET_LEN:
        return None
    nonce = notification_nonce(mac, packet)
    payload = crypt_payload(key, nonce, packet[7:])
    check = make_checksum(key, nonce, payload)
    if check[0:2] != packet[5:7]:
        return None
    return packet[0:7] + payload


def _name_xor_password(mesh_name: bytes, mesh_password: bytes) -> bytes:
    if len(mesh_name) > MESH_CREDENTIAL_MAX_LEN or len(mesh_password) > MESH_CREDENTIAL_MAX_LEN:
        raise ValueError("mesh name and password hold at most 16 bytes")
    return _xor(mesh_name.ljust(BLOCK, b"\x00"), mesh_password.ljust(BLOCK, b"\x00"))


def make_pair_packet(mesh_name: bytes, mesh_password: bytes, session_random: bytes) -> bytes:
    """Login request written to the pair characteristic (17 bytes)."""
    if len(session_random) != 8:
        raise ValueError("session_random must be 8 bytes")
    enc = telink_encrypt(
        session_random.ljust(BLOCK, b"\x00"), _name_xor_password(mesh_name, mesh_password)
    )
    return bytes([PairOpcode.LOGIN_REQUEST]) + session_random + enc[0:8]


def make_session_key(
    mesh_name: bytes, mesh_password: bytes, session_random: bytes, response_random: bytes
) -> bytes:
    """Derive the 16 byte session key from both randoms."""
    return telink_encrypt(
        _name_xor_password(mesh_name, mesh_password), session_random + response_random
    )


def session_key_from_pair_response(
    mesh_name: bytes, mesh_password: bytes, session_random: bytes, response: bytes
) -> bytes:
    """Validate the pair characteristic reply and derive the session key."""
    if not response:
        raise ProtocolError("empty pair response")
    if response[0] == PairOpcode.LOGIN_FAILURE:
        raise MeshAuthError("mesh credentials refused by the device")
    if response[0] != PairOpcode.LOGIN_SUCCESS or len(response) < 9:
        raise ProtocolError(f"unexpected pair response opcode 0x{response[0]:02x}")
    return make_session_key(mesh_name, mesh_password, session_random, response[1:9])


def make_set_mesh_packets(
    session_key: bytes, new_name: bytes, new_password: bytes, new_ltk: bytes
) -> tuple[bytes, bytes, bytes]:
    """Packets that move a device to a new mesh (pairing a factory device)."""
    for value in (new_name, new_password, new_ltk):
        if len(value) > MESH_CREDENTIAL_MAX_LEN:
            raise ValueError("mesh values hold at most 16 bytes")
    return (
        bytes([PairOpcode.SET_MESH_NAME]) + telink_encrypt(session_key, new_name),
        bytes([PairOpcode.SET_MESH_PASSWORD]) + telink_encrypt(session_key, new_password),
        bytes([PairOpcode.SET_MESH_LTK]) + telink_encrypt(session_key, new_ltk),
    )


def crc16(data: bytes) -> int:
    """CRC16 used by the OTA firmware transfer."""
    poly = (0x0, 0xA001)
    crc = 0xFFFF
    for byte in data:
        val = byte
        for _ in range(8):
            crc = (crc >> 1) ^ poly[(crc ^ val) & 0x1]
            val >>= 1
    return crc
