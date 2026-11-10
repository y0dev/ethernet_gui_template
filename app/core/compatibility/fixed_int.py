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
        return (1 << self.bits) - 1

    @property
    def min(self) -> int:
        return -(1 << (self.bits - 1)) if self.signed else 0

    @property
    def max(self) -> int:
        return (1 << (self.bits - 1)) - 1 if self.signed else self.mask

    @property
    def size(self) -> int:
        """Size in bytes."""
        return self.bits // 8

    # -- conversion -------------------------------------------------------
    def in_range(self, value: int) -> bool:
        return self.min <= value <= self.max

    def wrap(self, value: int) -> int:
        """Convert ``value`` to this type the way a C cast does (truncation)."""
        value &= self.mask
        if self.signed and value > self.max:
            value -= 1 << self.bits
        return value

    def check(self, value: int) -> int:
        """Return ``value`` unchanged, or raise if it does not fit."""
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{self.name}: expected int, got {type(value).__name__}")
        if not self.in_range(value):
            raise CIntRangeError(
                f"{value} out of range for {self.name} [{self.min}, {self.max}]"
            )
        return value

    def saturate(self, value: int) -> int:
        """Clamp ``value`` to the representable range."""
        return max(self.min, min(self.max, value))

    # -- arithmetic -------------------------------------------------------
    def _result(self, exact: int, op: str, wrap_signed: bool) -> int:
        if self.signed and not wrap_signed and not self.in_range(exact):
            raise CUndefinedBehaviorError(
                f"signed overflow in {self.name} {op} (result {exact}); "
                "pass wrap_signed=True only if the C code was built with wrapping semantics"
            )
        return self.wrap(exact)

    def add(self, a: int, b: int, *, wrap_signed: bool = False) -> int:
        return self._result(self.check(a) + self.check(b), "add", wrap_signed)

    def sub(self, a: int, b: int, *, wrap_signed: bool = False) -> int:
        return self._result(self.check(a) - self.check(b), "sub", wrap_signed)

    def mul(self, a: int, b: int, *, wrap_signed: bool = False) -> int:
        return self._result(self.check(a) * self.check(b), "mul", wrap_signed)

    def neg(self, a: int, *, wrap_signed: bool = False) -> int:
        return self._result(-self.check(a), "neg", wrap_signed)

    def div(self, a: int, b: int) -> int:
        """C ``a / b``: truncates toward zero."""
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
        """C ``a % b``: result has the sign of the dividend."""
        return a - self.div(a, b) * b

    def shl(self, a: int, count: int, *, wrap_signed: bool = False) -> int:
        self.check(a)
        self._check_shift(count)
        if self.signed and a < 0 and not wrap_signed:
            raise CUndefinedBehaviorError(f"left shift of negative {self.name}")
        return self._result(a << count, "shl", wrap_signed)

    def shr(self, a: int, count: int) -> int:
        """C ``a >> count`` (arithmetic shift for negative signed values)."""
        self.check(a)
        self._check_shift(count)
        return a >> count

    def _check_shift(self, count: int) -> None:
        if count < 0 or count >= self.bits:
            raise CUndefinedBehaviorError(
                f"shift count {count} invalid for {self.name} (width {self.bits})"
            )

    def bit_not(self, a: int) -> int:
        return self.wrap(~self.check(a))

    def __str__(self) -> str:
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
    """``(uint8_t)value``"""
    return U8.wrap(value)


def u16(value: int) -> int:
    """``(uint16_t)value``"""
    return U16.wrap(value)


def u32(value: int) -> int:
    """``(uint32_t)value``"""
    return U32.wrap(value)


def u64(value: int) -> int:
    """``(uint64_t)value``"""
    return U64.wrap(value)


def i8(value: int) -> int:
    """``(int8_t)value``"""
    return I8.wrap(value)


def i16(value: int) -> int:
    """``(int16_t)value``"""
    return I16.wrap(value)


def i32(value: int) -> int:
    """``(int32_t)value``"""
    return I32.wrap(value)


def i64(value: int) -> int:
    """``(int64_t)value``"""
    return I64.wrap(value)
