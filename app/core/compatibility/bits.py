"""Bit manipulation utilities mirroring common C macro idioms."""

from __future__ import annotations

from .fixed_int import U32, IntType


def bit(n: int) -> int:
    """Return a value with only bit ``n`` set, like ``BIT(n)`` or ``(1u << n)``.

    Args:
        n: Zero-based bit index.

    Returns:
        ``1 << n``.

    Raises:
        ValueError: If ``n`` is negative.
    """
    if n < 0:
        raise ValueError("bit index must be >= 0")
    return 1 << n


def mask(width: int, shift: int = 0) -> int:
    """Build a mask of ``width`` consecutive one bits, in the style of ``GENMASK``.

    Args:
        width: Number of one bits.
        shift: Position of the lowest one bit.

    Returns:
        ``((1 << width) - 1) << shift``.

    Raises:
        ValueError: If ``width`` or ``shift`` is negative.
    """
    if width < 0 or shift < 0:
        raise ValueError("width and shift must be >= 0")
    return ((1 << width) - 1) << shift


def get_field(value: int, shift: int, width: int) -> int:
    """Extract an unsigned bit field, like ``(value >> shift) & ((1 << width) - 1)``.

    Args:
        value: Integer containing the field.
        shift: Position of the field's lowest bit.
        width: Field width in bits.

    Returns:
        The field value.
    """
    return (value >> shift) & mask(width)


def set_field(value: int, shift: int, width: int, field: int, ctype: IntType = U32) -> int:
    """Return ``value`` with one bit field replaced.

    C code would silently truncate a ``field`` that is too wide. That is
    almost always a bug, so this function raises instead.

    Args:
        value: Original integer; must fit in ``ctype``.
        shift: Position of the field's lowest bit.
        width: Field width in bits.
        field: New field value; must fit in ``width`` bits.
        ctype: C integer type that holds ``value``.

    Returns:
        The updated integer, wrapped to ``ctype``.

    Raises:
        ValueError: If ``field`` does not fit, the field exceeds ``ctype``, or
            ``value`` is out of range for ``ctype``.
    """
    if field < 0 or field > mask(width):
        raise ValueError(f"field value {field} does not fit in {width} bits")
    if shift + width > ctype.bits:
        raise ValueError(f"field [{shift}+{width}] exceeds {ctype.name}")
    cleared = ctype.check(value) & ~mask(width, shift)
    return ctype.wrap(cleared | (field << shift))


def is_bit_set(value: int, n: int) -> bool:
    """Report whether bit ``n`` of ``value`` is set.

    Args:
        value: Integer to test.
        n: Zero-based bit index.

    Returns:
        True if the bit is one.
    """
    return bool(value & bit(n))


def sign_extend(value: int, width: int) -> int:
    """Interpret the low ``width`` bits of ``value`` as a two's-complement number.

    Args:
        value: Raw bits; higher bits are ignored.
        width: Width of the signed field in bits.

    Returns:
        The signed value.

    Raises:
        ValueError: If ``width`` is not positive.
    """
    if width <= 0:
        raise ValueError("width must be > 0")
    value &= mask(width)
    sign = 1 << (width - 1)
    return (value ^ sign) - sign


def rotl(value: int, count: int, ctype: IntType = U32) -> int:
    """Rotate an unsigned value left within its C type.

    Args:
        value: Value to rotate; must fit in ``ctype``.
        count: Number of bit positions, taken modulo the type width.
        ctype: Unsigned C integer type.

    Returns:
        The rotated value.

    Raises:
        ValueError: If ``ctype`` is signed or ``value`` is out of range.
    """
    if ctype.signed:
        raise ValueError("rotation is only defined here for unsigned types")
    ctype.check(value)
    count %= ctype.bits
    return ((value << count) | (value >> (ctype.bits - count))) & ctype.mask


def rotr(value: int, count: int, ctype: IntType = U32) -> int:
    """Rotate an unsigned value right within its C type.

    Args:
        value: Value to rotate; must fit in ``ctype``.
        count: Number of bit positions, taken modulo the type width.
        ctype: Unsigned C integer type.

    Returns:
        The rotated value.

    Raises:
        ValueError: If ``ctype`` is signed or ``value`` is out of range.
    """
    return rotl(value, ctype.bits - (count % ctype.bits), ctype)


def popcount(value: int) -> int:
    """Count the one bits in a non-negative integer.

    Args:
        value: Integer to count.

    Returns:
        The number of set bits.

    Raises:
        ValueError: If ``value`` is negative.
    """
    if value < 0:
        raise ValueError("popcount requires a non-negative value")
    return bin(value).count("1")


def reverse_bits(value: int, width: int) -> int:
    """Reverse the order of the low ``width`` bits, as reflected CRCs do.

    Args:
        value: Integer whose low bits are reversed.
        width: Number of bits to reverse.

    Returns:
        The bit-reversed value.
    """
    result = 0
    for _ in range(width):
        result = (result << 1) | (value & 1)
        value >>= 1
    return result
