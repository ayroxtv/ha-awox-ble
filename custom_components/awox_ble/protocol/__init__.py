"""Pure-Python Telink mesh protocol for AwoX / EGLO connect devices.

No Home Assistant import, no I/O: usable from the integration, the CLI and tests.
"""

from .constants import DEST_ALL, Opcode, PairOpcode
from .crypto import MeshAuthError, ProtocolError
from .packets import LightStatus, LoginHandshake, MeshSession, parse_status

__all__ = [
    "DEST_ALL",
    "LightStatus",
    "LoginHandshake",
    "MeshAuthError",
    "MeshSession",
    "Opcode",
    "PairOpcode",
    "ProtocolError",
    "parse_status",
]
