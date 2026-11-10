"""Bit manipulation utilities mirroring common C macro idioms."""

from __future__ import annotations

from .fixed_int import U32, IntType


def bit(n: int) -> int:
    """``BIT(n)`` / ``(1u << n)``"""
    if n < 0:
        raise ValueError("bit index must be >= 0")
    return 1 << n


def mask(width: int, shift: int = 0) -> int:
    """Mask of ``width`` ones starting at bit ``shift`` (``GENMASK`` style)."""
    if width < 0 or shift < 0:
        raise ValueError("width and shift must be >= 0")
    return ((1 << width) - 1) << shift


def get_field(value: int, shift: int, width: int) -> int:
    """Extract an unsigned bit field: ``(value >> shift) & ((1 << width) - 1)``."""
    return (value >> shift) & mask(width)


def set_field(value: int, shift: int, width: int, field: int, ctype: IntType = U32) -> int:
    """Return ``value`` with the bit field replaced.

    ``field`` must fit in ``width`` bits; the C idiom would silently truncate,
    which is almost always a bug, so this raises instead.
    """
    if field < 0 or field > mask(width):
        raise ValueError(f"field value {field} does not fit in {width} bits")
    if shift + width > ctype.bits:
        raise ValueError(f"field [{shift}+{width}] exceeds {ctype.name}")
    cleared = ctype.check(value) & ~mask(width, shift)
    return ctype.wrap(cleared | (field << shift))


def is_bit_set(value: int, n: int) -> bool:
    return bool(value & bit(n))


def sign_extend(value: int, width: int) -> int:
    """Interpret the low ``width`` bits of ``value`` as two's complement."""
    if width <= 0:
        raise ValueError("width must be > 0")
    value &= mask(width)
    sign = 1 << (width - 1)
    return (value ^ sign) - sign


def rotl(value: int, count: int, ctype: IntType = U32) -> int:
    """Rotate left within an unsigned type; ``count`` is reduced modulo width."""
    if ctype.signed:
        raise ValueError("rotation is only defined here for unsigned types")
    ctype.check(value)
    count %= ctype.bits
    return ((value << count) | (value >> (ctype.bits - count))) & ctype.mask


def rotr(value: int, count: int, ctype: IntType = U32) -> int:
    """Rotate right within an unsigned type."""
    return rotl(value, ctype.bits - (count % ctype.bits), ctype)


def popcount(value: int) -> int:
    if value < 0:
        raise ValueError("popcount requires a non-negative value")
    return bin(value).count("1")


def reverse_bits(value: int, width: int) -> int:
    """Reverse the low ``width`` bits (used by reflected CRCs)."""
    result = 0
    for _ in range(width):
        result = (result << 1) | (value & 1)
        value >>= 1
    return result
