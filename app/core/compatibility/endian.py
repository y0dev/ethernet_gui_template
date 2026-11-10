"""Byte-order utilities.

Byte order is always explicit.  Nothing in this module depends on the byte
order of the machine running Python, so ported code behaves the same on every
host regardless of what the original target was.
"""

from __future__ import annotations

from enum import Enum

from .fixed_int import U16, U32, U64, IntType


class ByteOrder(str, Enum):
    LITTLE = "little"
    BIG = "big"

    @property
    def struct_prefix(self) -> str:
        return "<" if self is ByteOrder.LITTLE else ">"


NETWORK = ByteOrder.BIG


def to_bytes(value: int, ctype: IntType, order: ByteOrder) -> bytes:
    """Serialize ``value`` as ``ctype``; raises if it does not fit."""
    ctype.check(value)
    return value.to_bytes(ctype.size, order.value, signed=ctype.signed)


def from_bytes(data: bytes | bytearray | memoryview, ctype: IntType, order: ByteOrder) -> int:
    """Deserialize exactly ``ctype.size`` bytes."""
    if len(data) != ctype.size:
        raise ValueError(f"{ctype.name} needs {ctype.size} bytes, got {len(data)}")
    return int.from_bytes(data, order.value, signed=ctype.signed)


def swap16(value: int) -> int:
    U16.check(value)
    return ((value & 0xFF) << 8) | (value >> 8)


def swap32(value: int) -> int:
    U32.check(value)
    return int.from_bytes(value.to_bytes(4, "little"), "big")


def swap64(value: int) -> int:
    U64.check(value)
    return int.from_bytes(value.to_bytes(8, "little"), "big")


def htons(value: int, host: ByteOrder) -> int:
    """``htons`` for an explicitly stated host byte order."""
    return value if host is ByteOrder.BIG else swap16(value)


def htonl(value: int, host: ByteOrder) -> int:
    """``htonl`` for an explicitly stated host byte order."""
    return value if host is ByteOrder.BIG else swap32(value)


ntohs = htons
ntohl = htonl
