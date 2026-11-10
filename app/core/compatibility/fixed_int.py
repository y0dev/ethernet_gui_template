"""Fixed-width integer helpers that reproduce C integer semantics.

Python integers are unbounded; C integers are not.  Every helper here models
"compute the mathematical result, then convert it to type T", which is what a
C assignment ``T x = expr;`` does once the expression has been evaluated.

Rules encoded here (C11 unless noted):

* Unsigned arithmetic wraps modulo 2**N (well defined, 6.2.5p9).
* Conversion *to* a signed type of an out-of-range value is
  implementation-defined (6.3.1.3p3).  Every mainstream compiler (GCC, Clang,
  MSVC, IAR, Keil) uses two's-complement truncation, which :meth:`IntType.wrap`
  reproduces.
* Signed arithmetic overflow is **undefined behaviour**.  The arithmetic
  helpers raise :class:`CUndefinedBehaviorError` for it unless the caller passes
  ``wrap_signed=True`` (the ``-fwrapv`` behaviour).  A port must decide which
  one the original code relied on; the helper will not guess.
* ``/`` truncates toward zero and ``%`` takes the sign of the dividend
  (6.5.5p6) - unlike Python's ``//`` and ``%``.
* Shifting by a negative amount or by >= the type width is undefined.
* ``>>`` on a negative signed value is implementation-defined; arithmetic
  shift (sign-propagating) is used, as on all mainstream compilers.

Integer promotion is NOT modelled implicitly: ``uint8_t a, b; a + b`` is an
``int`` in C until it is stored.  Use the helper of the *destination* type, or
the helper of the promoted type (usually ``I32``) for intermediate values.
"""

from __future__ import annotations

from dataclasses import dataclass


class CIntRangeError(ValueError):
    """A value does not fit the fixed-width type it was checked against."""


class CUndefinedBehaviorError(ArithmeticError):
    """The equivalent C operation would be undefined behaviour."""


@dataclass(frozen=True)
class IntType:
    """A fixed-width C integer type (``uint8_t`` ... ``int64_t``)."""

    name: str
    bits: int
    signed: bool

    @property
    def mask(self) -> int:
        """Bit mask covering every bit of the type, e.g. ``0xFF`` for 8 bits."""
        return (1 << self.bits) - 1

    @property
    def min(self) -> int:
        """Smallest representable value."""
        return -(1 << (self.bits - 1)) if self.signed else 0

    @property
    def max(self) -> int:
        """Largest representable value."""
        return (1 << (self.bits - 1)) - 1 if self.signed else self.mask

    @property
    def size(self) -> int:
        """Size of the type in bytes."""
        return self.bits // 8

    # -- conversion -------------------------------------------------------
    def in_range(self, value: int) -> bool:
        """Report whether a value is representable in this type.

        Args:
            value: Integer to test.

        Returns:
            True if ``min <= value <= max``.
        """
        return self.min <= value <= self.max

    def wrap(self, value: int) -> int:
        """Convert a value to this type the way a C cast does, by truncation.

        Args:
            value: Any integer.

        Returns:
            The value reduced to this type's width, read as two's complement
            for signed types.
        """
        value &= self.mask
        if self.signed and value > self.max:
            value -= 1 << self.bits
        return value

    def check(self, value: int) -> int:
        """Return a value unchanged if it fits this type, otherwise raise.

        Args:
            value: Value to validate.

        Returns:
            ``value``.

        Raises:
            TypeError: If ``value`` is not an int, or is a bool.
            CIntRangeError: If ``value`` is outside the type's range.
        """
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{self.name}: expected int, got {type(value).__name__}")
        if not self.in_range(value):
            raise CIntRangeError(
                f"{value} out of range for {self.name} [{self.min}, {self.max}]"
            )
        return value

    def saturate(self, value: int) -> int:
        """Clamp a value to the representable range.

        Args:
            value: Any integer.

        Returns:
            ``value`` limited to ``[min, max]``.
        """
        return max(self.min, min(self.max, value))

    # -- arithmetic -------------------------------------------------------
    def _checked_result(self, exact: int, op: str, wrap_signed: bool) -> int:
        """Convert an exact arithmetic result to this type, rejecting signed overflow.

        Args:
            exact: Mathematically exact result of the operation.
            op: Operation name, used in the error message.
            wrap_signed: If True, signed overflow wraps (``-fwrapv``) instead of
                raising.

        Returns:
            The result wrapped to this type.

        Raises:
            CUndefinedBehaviorError: If a signed result overflows and
                ``wrap_signed`` is False.
        """
        if self.signed and not wrap_signed and not self.in_range(exact):
            raise CUndefinedBehaviorError(
                f"signed overflow in {self.name} {op} (result {exact}); "
                "pass wrap_signed=True only if the C code was built with wrapping semantics"
            )
        return self.wrap(exact)

    def add(self, a: int, b: int, *, wrap_signed: bool = False) -> int:
        """Compute C ``a + b`` in this type.

        Args:
            a: Left operand; must fit this type.
            b: Right operand; must fit this type.
            wrap_signed: Wrap signed overflow instead of raising.

        Returns:
            The sum converted to this type.

        Raises:
            CUndefinedBehaviorError: On signed overflow without ``wrap_signed``.
        """
        return self._checked_result(self.check(a) + self.check(b), "add", wrap_signed)

    def sub(self, a: int, b: int, *, wrap_signed: bool = False) -> int:
        """Compute C ``a - b`` in this type.

        Args:
            a: Left operand; must fit this type.
            b: Right operand; must fit this type.
            wrap_signed: Wrap signed overflow instead of raising.

        Returns:
            The difference converted to this type.

        Raises:
            CUndefinedBehaviorError: On signed overflow without ``wrap_signed``.
        """
        return self._checked_result(self.check(a) - self.check(b), "sub", wrap_signed)

    def mul(self, a: int, b: int, *, wrap_signed: bool = False) -> int:
        """Compute C ``a * b`` in this type.

        Args:
            a: Left operand; must fit this type.
            b: Right operand; must fit this type.
            wrap_signed: Wrap signed overflow instead of raising.

        Returns:
            The product converted to this type.

        Raises:
            CUndefinedBehaviorError: On signed overflow without ``wrap_signed``.
        """
        return self._checked_result(self.check(a) * self.check(b), "mul", wrap_signed)

    def neg(self, a: int, *, wrap_signed: bool = False) -> int:
        """Compute C ``-a`` in this type.

        Args:
            a: Operand; must fit this type.
            wrap_signed: Wrap signed overflow (``-INT_MIN``) instead of raising.

        Returns:
            The negation converted to this type.

        Raises:
            CUndefinedBehaviorError: On signed overflow without ``wrap_signed``.
        """
        return self._checked_result(-self.check(a), "neg", wrap_signed)

    def div(self, a: int, b: int) -> int:
        """Compute C ``a / b``, truncating toward zero.

        Args:
            a: Dividend; must fit this type.
            b: Divisor; must fit this type.

        Returns:
            The quotient.

        Raises:
            CUndefinedBehaviorError: On division by zero or ``INT_MIN / -1``.
        """
        self.check(a)
        self.check(b)
        if b == 0:
            raise CUndefinedBehaviorError(f"{self.name} division by zero")
        quotient = abs(a) // abs(b)
        if (a < 0) != (b < 0):
            quotient = -quotient
        if not self.in_range(quotient):  # INT_MIN / -1
            raise CUndefinedBehaviorError(f"{self.name} division overflow ({a} / {b})")
        return quotient

    def mod(self, a: int, b: int) -> int:
        """Compute C ``a % b``; the result has the sign of the dividend.

        Args:
            a: Dividend; must fit this type.
            b: Divisor; must fit this type.

        Returns:
            The remainder.

        Raises:
            CUndefinedBehaviorError: On division by zero or overflow.
        """
        return a - self.div(a, b) * b

    def shl(self, a: int, count: int, *, wrap_signed: bool = False) -> int:
        """Compute C ``a << count`` in this type.

        Args:
            a: Value to shift; must fit this type.
            count: Shift amount, ``0 <= count < bits``.
            wrap_signed: Allow shifting negative values and wrap signed overflow.

        Returns:
            The shifted value converted to this type.

        Raises:
            CUndefinedBehaviorError: For an invalid shift count, a negative signed
                operand or signed overflow without ``wrap_signed``.
        """
        self.check(a)
        self._check_shift(count)
        if self.signed and a < 0 and not wrap_signed:
            raise CUndefinedBehaviorError(f"left shift of negative {self.name}")
        return self._checked_result(a << count, "shl", wrap_signed)

    def shr(self, a: int, count: int) -> int:
        """Compute C ``a >> count``, using an arithmetic shift for negative values.

        Args:
            a: Value to shift; must fit this type.
            count: Shift amount, ``0 <= count < bits``.

        Returns:
            The shifted value.

        Raises:
            CUndefinedBehaviorError: If ``count`` is negative or too large.
        """
        self.check(a)
        self._check_shift(count)
        return a >> count

    def _check_shift(self, count: int) -> None:
        """Reject shift counts that are undefined behaviour in C.

        Args:
            count: Proposed shift amount.

        Raises:
            CUndefinedBehaviorError: If ``count`` is negative or at least the
                type width.
        """
        if count < 0 or count >= self.bits:
            raise CUndefinedBehaviorError(
                f"shift count {count} invalid for {self.name} (width {self.bits})"
            )

    def bit_not(self, a: int) -> int:
        """Compute C ``~a`` in this type.

        Args:
            a: Operand; must fit this type.

        Returns:
            The bitwise complement converted to this type.
        """
        return self.wrap(~self.check(a))

    def __str__(self) -> str:
        """Return the C type name, e.g. ``uint16_t``."""
        return self.name


U8 = IntType("uint8_t", 8, False)
U16 = IntType("uint16_t", 16, False)
U32 = IntType("uint32_t", 32, False)
U64 = IntType("uint64_t", 64, False)
I8 = IntType("int8_t", 8, True)
I16 = IntType("int16_t", 16, True)
I32 = IntType("int32_t", 32, True)
I64 = IntType("int64_t", 64, True)

ALL_TYPES: tuple[IntType, ...] = (U8, U16, U32, U64, I8, I16, I32, I64)
BY_NAME: dict[str, IntType] = {t.name: t for t in ALL_TYPES}


def u8(value: int) -> int:
    """Convert like the C cast ``(uint8_t)value``.

    Args:
        value: Any integer.

    Returns:
        The value truncated to 8 unsigned bits.
    """
    return U8.wrap(value)


def u16(value: int) -> int:
    """Convert like the C cast ``(uint16_t)value``.

    Args:
        value: Any integer.

    Returns:
        The value truncated to 16 unsigned bits.
    """
    return U16.wrap(value)


def u32(value: int) -> int:
    """Convert like the C cast ``(uint32_t)value``.

    Args:
        value: Any integer.

    Returns:
        The value truncated to 32 unsigned bits.
    """
    return U32.wrap(value)


def u64(value: int) -> int:
    """Convert like the C cast ``(uint64_t)value``.

    Args:
        value: Any integer.

    Returns:
        The value truncated to 64 unsigned bits.
    """
    return U64.wrap(value)


def i8(value: int) -> int:
    """Convert like the C cast ``(int8_t)value``.

    Args:
        value: Any integer.

    Returns:
        The value truncated to 8 bits as two's complement.
    """
    return I8.wrap(value)


def i16(value: int) -> int:
    """Convert like the C cast ``(int16_t)value``.

    Args:
        value: Any integer.

    Returns:
        The value truncated to 16 bits as two's complement.
    """
    return I16.wrap(value)


def i32(value: int) -> int:
    """Convert like the C cast ``(int32_t)value``.

    Args:
        value: Any integer.

    Returns:
        The value truncated to 32 bits as two's complement.
    """
    return I32.wrap(value)


def i64(value: int) -> int:
    """Convert like the C cast ``(int64_t)value``.

    Args:
        value: Any integer.

    Returns:
        The value truncated to 64 bits as two's complement.
    """
    return I64.wrap(value)
