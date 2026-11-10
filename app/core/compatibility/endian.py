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
        """The ``struct`` module prefix for this byte order: ``<`` or ``>``."""
        return "<" if self is ByteOrder.LITTLE else ">"


NETWORK = ByteOrder.BIG


def to_bytes(value: int, ctype: IntType, order: ByteOrder) -> bytes:
    """Serialize an integer as a C type in the given byte order.

    Args:
        value: Integer to encode.
        ctype: C integer type that sets the size and signedness.
        order: Byte order of the output.

    Returns:
        ``ctype.size`` bytes.

    Raises:
        ValueError: If ``value`` does not fit in ``ctype``.
    """
    ctype.check(value)
    return value.to_bytes(ctype.size, order.value, signed=ctype.signed)


def from_bytes(data: bytes | bytearray | memoryview, ctype: IntType, order: ByteOrder) -> int:
    """Deserialize exactly ``ctype.size`` bytes into an integer.

    Args:
        data: Bytes to decode.
        ctype: C integer type that sets the size and signedness.
        order: Byte order of ``data``.

    Returns:
        The decoded integer.

    Raises:
        ValueError: If ``data`` is not exactly ``ctype.size`` bytes long.
    """
    if len(data) != ctype.size:
        raise ValueError(f"{ctype.name} needs {ctype.size} bytes, got {len(data)}")
    return int.from_bytes(data, order.value, signed=ctype.signed)


def swap16(value: int) -> int:
    """Swap the two bytes of a 16-bit unsigned value.

    Args:
        value: Value in the uint16 range.

    Returns:
        The byte-swapped value.

    Raises:
        ValueError: If ``value`` is out of range.
    """
    U16.check(value)
    return ((value & 0xFF) << 8) | (value >> 8)


def swap32(value: int) -> int:
    """Reverse the four bytes of a 32-bit unsigned value.

    Args:
        value: Value in the uint32 range.

    Returns:
        The byte-swapped value.

    Raises:
        ValueError: If ``value`` is out of range.
    """
    U32.check(value)
    return int.from_bytes(value.to_bytes(4, "little"), "big")


def swap64(value: int) -> int:
    """Reverse the eight bytes of a 64-bit unsigned value.

    Args:
        value: Value in the uint64 range.

    Returns:
        The byte-swapped value.

    Raises:
        ValueError: If ``value`` is out of range.
    """
    U64.check(value)
    return int.from_bytes(value.to_bytes(8, "little"), "big")


def htons(value: int, host: ByteOrder) -> int:
    """Convert a 16-bit value from host to network byte order, like ``htons``.

    The host byte order is passed in, so results never depend on the machine
    running Python. The conversion is its own inverse and is also exported
    as ``ntohs``.

    Args:
        value: Value in the uint16 range.
        host: Byte order of the original C target.

    Returns:
        The value in network (big-endian) order.
    """
    return value if host is ByteOrder.BIG else swap16(value)


def htonl(value: int, host: ByteOrder) -> int:
    """Convert a 32-bit value from host to network byte order, like ``htonl``.

    The host byte order is passed in, so results never depend on the machine
    running Python. The conversion is its own inverse and is also exported
    as ``ntohl``.

    Args:
        value: Value in the uint32 range.
        host: Byte order of the original C target.

    Returns:
        The value in network (big-endian) order.
    """
    return value if host is ByteOrder.BIG else swap32(value)


ntohs = htons
ntohl = htonl
