"""Telink mesh constants used by AwoX / EGLO connect devices.

Ported from python-awox-mesh-light (c) 2017 Leiaz and home-assistant-awox
(c) 2020 Frans Saris, both MIT licensed.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final

SERVICE_UUID: Final = "00010203-0405-0607-0809-0a0b0c0d1910"
STATUS_CHAR_UUID: Final = "00010203-0405-0607-0809-0a0b0c0d1911"
COMMAND_CHAR_UUID: Final = "00010203-0405-0607-0809-0a0b0c0d1912"
OTA_CHAR_UUID: Final = "00010203-0405-0607-0809-0a0b0c0d1913"
PAIR_CHAR_UUID: Final = "00010203-0405-0607-0809-0a0b0c0d1914"

MODEL_NUMBER_UUID: Final = "00002a24-0000-1000-8000-00805f9b34fb"
FIRMWARE_REV_UUID: Final = "00002a26-0000-1000-8000-00805f9b34fb"
HARDWARE_REV_UUID: Final = "00002a27-0000-1000-8000-00805f9b34fb"

#: Vendor code appended after the opcode in every command (0x0160, little endian).
VENDOR_CODE: Final = b"\x60\x01"

#: Mesh address that targets every node of the mesh.
DEST_ALL: Final = 0xFFFF

#: Written to the status characteristic to switch on Telink notifications.
ENABLE_NOTIFICATIONS: Final = b"\x01"

#: Max length (bytes) of mesh name / password / long term key.
MESH_CREDENTIAL_MAX_LEN: Final = 16

PACKET_LEN: Final = 20
PAYLOAD_LEN: Final = 15
SEQ_LEN: Final = 3


class PairOpcode(IntEnum):
    """First byte of packets exchanged on the pair characteristic."""

    LOGIN_REQUEST = 0x0C
    LOGIN_SUCCESS = 0x0D
    LOGIN_FAILURE = 0x0E
    SET_MESH_NAME = 0x04
    SET_MESH_PASSWORD = 0x05
    SET_MESH_LTK = 0x06
    SET_MESH_SUCCESS = 0x07


class Opcode(IntEnum):
    """Mesh command / report opcodes (byte 7 of a decrypted packet)."""

    POWER = 0xD0
    GROUP_ID_REPORT = 0xD4
    MESH_GROUP = 0xD7
    ADDRESS_REPORT = 0xD8
    REQUEST_STATUS = 0xDA
    STATUS_REPORT = 0xDB
    ONLINE_STATUS_REPORT = 0xDC
    GROUP_ID_QUERY = 0xDD
    MESH_ADDRESS = 0xE0
    ADDRESS_REPORT_QUERY = 0xE1
    COLOR = 0xE2
    MESH_RESET = 0xE3
    TIME = 0xE4
    ALARMS = 0xE5
    DEVICE_INFO_QUERY = 0xEA
    DEVICE_INFO_REPORT = 0xEB
    PRESET = 0xC8
    CANDLE_MODE = 0xC9
    LIGHT_MODE = 0x33
    WHITE_TEMPERATURE = 0xF0
    WHITE_BRIGHTNESS = 0xF1
    COLOR_BRIGHTNESS = 0xF2
    SEQUENCE_COLOR_DURATION = 0xF5
    SEQUENCE_FADE_DURATION = 0xF6


# Device value ranges.
WHITE_BRIGHTNESS_MIN: Final = 0x01
WHITE_BRIGHTNESS_MAX: Final = 0x7F
WHITE_TEMPERATURE_MIN: Final = 0x00  # coolest
WHITE_TEMPERATURE_MAX: Final = 0x7F  # warmest
COLOR_BRIGHTNESS_MIN: Final = 0x0A
COLOR_BRIGHTNESS_MAX: Final = 0x64
KELVIN_MIN: Final = 2700
KELVIN_MAX: Final = 6500
