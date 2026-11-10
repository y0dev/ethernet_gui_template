"""Compatibility layer vs. code compiled by the host C compiler (via ctypes)."""

from __future__ import annotations

import ctypes
import random

import pytest

from app.core.compatibility import crc as crcmod
from app.core.compatibility.endian import ByteOrder
from app.core.compatibility.fixed_int import I8, I16, I32, U8, U16, U32
from app.core.compatibility.serialization import Field, StructLayout

pytestmark = pytest.mark.c_reference

BOUNDARY = [
    0, 1, -1, 127, 128, -128, -129, 255, 256, 32767, 32768, -32768, -32769,
    65535, 65536, 2**31 - 1, 2**31, -(2**31), -(2**31) - 1, 2**32 - 1, 2**32,
    2**63 - 1, -(2**63), 0x123456789ABCDEF,
]


def _samples(seed: int, count: int, lo: int, hi: int) -> list[int]:
    rng = random.Random(seed)
    return [rng.randint(lo, hi) for _ in range(count)]


@pytest.mark.parametrize(
    "ctype, fn",
    [(U8, "ref_to_u8"), (U16, "ref_to_u16"), (U32, "ref_to_u32"),
     (I8, "ref_to_i8"), (I16, "ref_to_i16"), (I32, "ref_to_i32")],
)
def test_cast_matches_c(c_reference, ctype, fn):
    c_fn = getattr(c_reference, fn)
    for value in BOUNDARY + _samples(1, 2000, -(2**63), 2**63 - 1):
        assert ctype.wrap(value) == c_fn(value), f"({ctype.name}){value}"


def test_signed_division_and_modulo_match_c(c_reference):
    values = [-(2**31), -7, -1, 0, 1, 7, 2**31 - 1] + _samples(2, 500, -(2**31), 2**31 - 1)
    divisors = [-7, -3, -2, -1, 1, 2, 3, 7, 2**31 - 1, -(2**31)] + _samples(3, 50, -(2**31), 2**31 - 1)
    for a in values:
        for b in divisors:
            if b == 0 or (a == -(2**31) and b == -1):
                continue  # undefined in C; covered by the UB tests
            assert I32.div(a, b) == c_reference.ref_div_i32(a, b), (a, b)
            assert I32.mod(a, b) == c_reference.ref_mod_i32(a, b), (a, b)


def test_wrapping_signed_arithmetic_matches_fwrapv(c_reference):
    pairs = list(zip(_samples(4, 2000, -(2**31), 2**31 - 1), _samples(5, 2000, -(2**31), 2**31 - 1), strict=True))
    pairs += [(2**31 - 1, 1), (-(2**31), -1), (2**31 - 1, 2**31 - 1)]
    for a, b in pairs:
        assert I32.add(a, b, wrap_signed=True) == c_reference.ref_add_i32_wrap(a, b)
        assert I32.mul(a, b, wrap_signed=True) == c_reference.ref_mul_i32_wrap(a, b)


def test_unsigned_arithmetic_matches_c(c_reference):
    pairs = list(zip(_samples(6, 2000, 0, 2**32 - 1), _samples(7, 2000, 0, 2**32 - 1), strict=True))
    pairs += [(0, 1), (2**32 - 1, 1), (2**32 - 1, 2**32 - 1)]
    for a, b in pairs:
        assert U32.add(a, b) == c_reference.ref_add_u32(a, b)
        assert U32.sub(a, b) == c_reference.ref_sub_u32(a, b)
        assert U32.mul(a, b) == c_reference.ref_mul_u32(a, b)
        assert U32.bit_not(a) == c_reference.ref_not_u32(a)
    for a, b in [(65535, 1), (40000, 40000), (0, 0)]:
        assert U16.add(a, b) == c_reference.ref_add_u16(a, b)


def test_arithmetic_right_shift_matches_c(c_reference):
    for a in [-(2**31), -1, -2, -3, -1000, 0, 1, 2**31 - 1] + _samples(8, 200, -(2**31), 2**31 - 1):
        for n in range(32):
            assert I32.shr(a, n) == c_reference.ref_shr_i32(a, n), (a, n)


@pytest.mark.parametrize(
    "params, fn",
    [
        (crcmod.CRC32, "ref_crc32"),
        (crcmod.CRC16_CCITT_FALSE, "ref_crc16_ccitt_false"),
        (crcmod.CRC16_MODBUS, "ref_crc16_modbus"),
        (crcmod.CRC8, "ref_crc8_smbus"),
    ],
)
def test_crc_matches_bitwise_c(c_reference, params, fn):
    engine = crcmod.Crc(params)
    c_fn = getattr(c_reference, fn)
    rng = random.Random(9)
    buffers = [b"", b"\x00", b"\xff", b"123456789", bytes(range(256))]
    buffers += [bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 300))) for _ in range(200)]
    for data in buffers:
        assert engine.compute(data) == c_fn(data, len(data)), (params.name, data.hex())


def _layout(packed: bool, order: ByteOrder) -> StructLayout:
    return StructLayout(
        "ref",
        [Field("a", "uint8_t"), Field("b", "uint32_t"), Field("c", "uint16_t"),
         Field("d", "bytes", 3), Field("e", "uint64_t"), Field("f", "int8_t")],
        order, packed=packed,
    )


VALUES = {"a": 0x11, "b": 0x22334455, "c": 0x6677, "d": b"\x01\x02\x03",
          "e": 0x0102030405060708, "f": -2}


def test_natural_layout_matches_compiler(c_reference):
    order = ByteOrder.LITTLE if c_reference.ref_is_little_endian() else ByteOrder.BIG
    layout = _layout(False, order)
    assert layout.size == c_reference.ref_natural_size()
    for index, name in enumerate("abcdef"):
        assert layout.offsets[name] == c_reference.ref_natural_offset(index), name
    buffer = ctypes.create_string_buffer(layout.size)
    c_reference.ref_fill_natural(buffer)
    assert layout.pack(VALUES) == buffer.raw  # byte-for-byte, padding included
    assert layout.unpack(buffer.raw) == {**VALUES, "d": b"\x01\x02\x03"}


def test_packed_layout_matches_compiler(c_reference):
    order = ByteOrder.LITTLE if c_reference.ref_is_little_endian() else ByteOrder.BIG
    layout = _layout(True, order)
    assert layout.size == c_reference.ref_packed_size()
    buffer = ctypes.create_string_buffer(layout.size)
    c_reference.ref_fill_packed(buffer)
    assert layout.pack(VALUES) == buffer.raw
