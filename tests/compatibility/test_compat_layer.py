"""Compatibility layer behaviour that does not need the C harness."""

from __future__ import annotations

import pytest

from app.core.compatibility import bits, crc
from app.core.compatibility.buffer import (
    BufferOverrunError,
    ByteReader,
    ByteWriter,
    OverflowPolicy,
    RingBuffer,
)
from app.core.compatibility.endian import (
    ByteOrder,
    from_bytes,
    htonl,
    htons,
    swap16,
    swap32,
    swap64,
    to_bytes,
)
from app.core.compatibility.fixed_int import (
    ALL_TYPES,
    I8,
    I16,
    I32,
    U8,
    U16,
    U32,
    U64,
    CIntRangeError,
    CUndefinedBehaviorError,
)
from app.core.compatibility.serialization import Field, StructLayout


# -- fixed width ----------------------------------------------------------
@pytest.mark.parametrize("ctype", ALL_TYPES, ids=str)
def test_limits(ctype):
    """Each type has the C range, wraps at its limits and range-checks values.

    Args:
        ctype: Fixed-width type under test.
    """
    if ctype.signed:
        assert ctype.min == -(2 ** (ctype.bits - 1))
        assert ctype.max == 2 ** (ctype.bits - 1) - 1
    else:
        assert (ctype.min, ctype.max) == (0, 2**ctype.bits - 1)
    assert ctype.wrap(ctype.max + 1) == ctype.min
    assert ctype.wrap(ctype.min - 1) == ctype.max
    assert ctype.check(ctype.max) == ctype.max
    with pytest.raises(CIntRangeError):
        ctype.check(ctype.max + 1)
    with pytest.raises(CIntRangeError):
        ctype.check(ctype.min - 1)


def test_check_rejects_non_int():
    """``check`` rejects floats and bools."""
    with pytest.raises(TypeError):
        U8.check(1.0)
    with pytest.raises(TypeError):
        U8.check(True)


def test_signed_overflow_is_undefined_unless_wrapping_requested():
    """Signed overflow, division by zero and negative left shifts raise unless wrapping is requested."""
    with pytest.raises(CUndefinedBehaviorError):
        I32.add(I32.max, 1)
    with pytest.raises(CUndefinedBehaviorError):
        I8.neg(I8.min)
    with pytest.raises(CUndefinedBehaviorError):
        I32.div(I32.min, -1)
    with pytest.raises(CUndefinedBehaviorError):
        I16.div(1, 0)
    with pytest.raises(CUndefinedBehaviorError):
        I32.shl(-1, 1)
    assert I32.add(I32.max, 1, wrap_signed=True) == I32.min


def test_unsigned_wraps():
    """Unsigned arithmetic wraps modulo 2**N."""
    assert U8.add(255, 1) == 0
    assert U16.sub(0, 1) == 0xFFFF
    assert U64.mul(2**63, 2) == 0
    assert U8.shl(0x81, 1) == 0x02


@pytest.mark.parametrize("count", [-1, 32, 33])
def test_invalid_shift_counts(count):
    """Negative shifts and shifts by at least the width are undefined behaviour.

    Args:
        count: Invalid shift count.
    """
    with pytest.raises(CUndefinedBehaviorError):
        U32.shl(1, count)
    with pytest.raises(CUndefinedBehaviorError):
        U32.shr(1, count)


def test_division_truncates_toward_zero():
    """Division truncates toward zero and modulo takes the dividend's sign, as in C."""
    assert I32.div(-7, 2) == -3       # Python -7 // 2 == -4
    assert I32.mod(-7, 2) == -1       # Python -7 % 2 == 1
    assert I32.div(7, -2) == -3
    assert I32.mod(7, -2) == 1


def test_saturate():
    """``saturate`` clamps to the type's range."""
    assert I8.saturate(1000) == 127
    assert U8.saturate(-5) == 0


# -- endian ---------------------------------------------------------------
def test_endian_round_trip():
    """Integers serialize in either byte order and reject out-of-range values or wrong lengths."""
    assert to_bytes(0x1234, U16, ByteOrder.BIG) == b"\x12\x34"
    assert to_bytes(0x1234, U16, ByteOrder.LITTLE) == b"\x34\x12"
    assert to_bytes(-2, I16, ByteOrder.BIG) == b"\xff\xfe"
    assert from_bytes(b"\xff\xfe", I16, ByteOrder.BIG) == -2
    assert from_bytes(b"\xff\xfe", U16, ByteOrder.BIG) == 0xFFFE
    with pytest.raises(CIntRangeError):
        to_bytes(70000, U16, ByteOrder.BIG)
    with pytest.raises(ValueError):
        from_bytes(b"\x00", U16, ByteOrder.BIG)


def test_swaps():
    """Byte swaps and host-to-network conversions follow the stated host order."""
    assert swap16(0x1234) == 0x3412
    assert swap32(0x12345678) == 0x78563412
    assert swap64(0x0102030405060708) == 0x0807060504030201
    assert htons(0x1234, ByteOrder.LITTLE) == 0x3412
    assert htons(0x1234, ByteOrder.BIG) == 0x1234
    assert htonl(0x12345678, ByteOrder.LITTLE) == 0x78563412


# -- bits -----------------------------------------------------------------
def test_bit_helpers():
    """Bit, mask, field, sign-extension, rotation and counting helpers give C results."""
    assert bits.bit(7) == 0x80
    assert bits.mask(4, 4) == 0xF0
    assert bits.get_field(0xABCD, 4, 8) == 0xBC
    assert bits.set_field(0x0000, 4, 4, 0xA) == 0x00A0
    assert bits.set_field(0xFFFF, 4, 4, 0x0, U16) == 0xFF0F
    with pytest.raises(ValueError):
        bits.set_field(0, 0, 4, 0x10)
    with pytest.raises(ValueError):
        bits.set_field(0, 30, 4, 1, U32)
    assert bits.is_bit_set(0b100, 2)
    assert bits.sign_extend(0xFFF, 12) == -1
    assert bits.sign_extend(0x7FF, 12) == 2047
    assert bits.rotl(0x80000001, 1) == 0x00000003
    assert bits.rotr(0x00000003, 1) == 0x80000001
    assert bits.rotl(0x12, 8, U8) == 0x12
    assert bits.popcount(0xFF) == 8
    assert bits.reverse_bits(0b0001, 4) == 0b1000


# -- CRC ------------------------------------------------------------------
@pytest.mark.parametrize("params", list(crc.CATALOGUE.values()), ids=lambda p: p.name)
def test_crc_catalogue_check_values(params):
    """Every catalogue CRC produces its published check value for ``123456789``.

    Args:
        params: Catalogue CRC parameters.
    """
    assert crc.Crc(params).compute(b"123456789") == params.check


def test_crc_incremental_equals_one_shot():
    """Feeding a CRC in chunks gives the same result as one call."""
    engine = crc.Crc(crc.CRC32)
    data = bytes(range(200))
    state = engine.start()
    for i in range(0, len(data), 7):
        state = engine.update(state, data[i:i + 7])
    assert engine.finish(state) == engine.compute(data)


def test_crc_accepts_memoryview():
    """The CRC engine accepts a memoryview."""
    engine = crc.Crc(crc.CRC16_MODBUS)
    assert engine.compute(memoryview(b"123456789")) == 0x4B37


def test_crc_rejects_bad_width():
    """CRC widths below 8 bits are rejected."""
    with pytest.raises(ValueError):
        crc.Crc(crc.CrcParams("bad", 4, 0x3, 0, False, False, 0))


def test_checksums():
    """The simple checksums match published example values."""
    assert crc.sum8(b"\xff\x02") == 0x01
    assert (crc.sum8(b"\x10\x20") + crc.sum8_twos_complement(b"\x10\x20")) & 0xFF == 0
    assert crc.xor8(b"\x01\x02\x04") == 0x07
    assert crc.fletcher16(b"abcde") == 0xC8F0   # published example value
    # RFC 1071 example: words 0x0001 0xf203 0xf4f5 0xf6f7 sum to 0xddf2
    assert crc.internet_checksum(bytes.fromhex("0001f203f4f5f6f7")) == (~0xDDF2) & 0xFFFF


# -- buffers --------------------------------------------------------------
def test_reader_bounds():
    """The reader decodes in either byte order and a failed read does not advance."""
    reader = ByteReader(b"\x01\x02\x03\x04\x05", ByteOrder.BIG)
    assert reader.read_int(U16) == 0x0102
    assert reader.read_int(U16, ByteOrder.LITTLE) == 0x0403
    assert reader.remaining == 1
    with pytest.raises(BufferOverrunError):
        reader.read_int(U16)
    assert reader.position == 4  # failed read does not advance
    assert reader.read_rest() == b"\x05"


def test_writer_capacity_and_patch():
    """The writer enforces its capacity and only patches already-written bytes."""
    writer = ByteWriter(capacity=4, order=ByteOrder.BIG)
    writer.write_int(0, U16)
    writer.write_bytes(b"\xAA\xBB")
    with pytest.raises(BufferOverrunError):
        writer.write_bytes(b"\x00")
    writer.patch(0, b"\x00\x02")
    assert writer.to_bytes() == b"\x00\x02\xAA\xBB"
    with pytest.raises(BufferOverrunError):
        writer.patch(3, b"\x00\x00")


def test_ring_buffer_reject_policy():
    """The reject policy stores what fits, wraps around and counts dropped bytes."""
    ring = RingBuffer(4)
    assert ring.write(b"abc") == 3
    assert ring.read(2) == b"ab"
    assert ring.write(b"defg") == 3   # wraps; one byte rejected
    assert ring.dropped == 1
    assert ring.read() == b"cdef"
    assert len(ring) == 0


def test_ring_buffer_overwrite_policy():
    """The overwrite policy drops the oldest bytes and counts them."""
    ring = RingBuffer(4, OverflowPolicy.OVERWRITE_OLDEST)
    ring.write(b"abc")
    ring.write(b"de")
    assert ring.peek() == b"bcde"
    ring.write(b"0123456")
    assert ring.read() == b"3456"
    assert ring.dropped == 1 + 3 + 4


# -- serialization --------------------------------------------------------
def test_struct_layout_validation():
    """Struct layouts pack and unpack arrays and reject bad values, fields and buffers."""
    layout = StructLayout("hdr", [Field("id", "uint16_t"), Field("arr", "int16_t", 2)], ByteOrder.BIG)
    assert layout.size == 6
    data = layout.pack({"id": 1, "arr": [-1, 2]})
    assert data == b"\x00\x01\xff\xff\x00\x02"
    assert layout.unpack(data) == {"id": 1, "arr": [-1, 2]}
    with pytest.raises(CIntRangeError):
        layout.pack({"id": 70000, "arr": [0, 0]})
    with pytest.raises(KeyError):
        layout.pack({"id": 1})
    with pytest.raises(ValueError):
        layout.unpack(b"\x00")
    with pytest.raises(ValueError):
        StructLayout("bad", [Field("x", "long")], ByteOrder.BIG)
    with pytest.raises(ValueError):
        StructLayout("dup", [Field("x", "uint8_t"), Field("x", "uint8_t")], ByteOrder.BIG)


def test_float_fields():
    """``float`` and ``double`` fields round-trip."""
    layout = StructLayout("f", [Field("x", "float"), Field("y", "double")], ByteOrder.LITTLE)
    out = layout.unpack(layout.pack({"x": 1.5, "y": -2.25}))
    assert out == {"x": 1.5, "y": -2.25}
