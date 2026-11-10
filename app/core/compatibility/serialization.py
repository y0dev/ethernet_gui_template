"""Declarative binary layouts for C structs that cross a wire or file boundary.

A :class:`StructLayout` is built from the C struct definition, field by field.
Padding is never guessed silently:

* ``packed=True`` reproduces ``__attribute__((packed))`` / ``#pragma pack(1)``.
* ``packed=False`` applies natural alignment (each scalar aligned to its own
  size, struct padded to its largest member).  That matches the common ABIs
  for the fixed-width types supported here, but the target's real layout must
  still be confirmed against the C build (``sizeof`` / ``offsetof``) before a
  module is marked verified.

Bit fields are deliberately not supported: their layout is
implementation-defined.  Model the containing integer as one field and use
:mod:`bits` with masks taken from the C source.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .endian import ByteOrder
from .fixed_int import BY_NAME, IntType

_INT_CODES = {
    ("uint8_t"): "B", ("int8_t"): "b",
    ("uint16_t"): "H", ("int16_t"): "h",
    ("uint32_t"): "I", ("int32_t"): "i",
    ("uint64_t"): "Q", ("int64_t"): "q",
}
_FLOAT_CODES = {"float": ("f", 4), "double": ("d", 8)}


@dataclass(frozen=True)
class Field:
    """One struct member.

    ``ctype`` is a fixed-width integer name (``uint16_t``), ``float``,
    ``double`` or ``bytes``.  ``count`` > 1 declares an array; for ``bytes`` it
    is the array length (``uint8_t name[count]`` kept as a bytes object).
    """

    name: str
    ctype: str
    count: int = 1

    @property
    def element_size(self) -> int:
        if self.ctype == "bytes":
            return 1
        if self.ctype in _FLOAT_CODES:
            return _FLOAT_CODES[self.ctype][1]
        return BY_NAME[self.ctype].size

    @property
    def size(self) -> int:
        return self.element_size * self.count

    @property
    def int_type(self) -> IntType | None:
        return BY_NAME.get(self.ctype)


class StructLayout:
    """Pack/unpack a C struct with explicit byte order and padding rules."""

    def __init__(
        self,
        name: str,
        fields: list[Field],
        order: ByteOrder,
        packed: bool = True,
    ) -> None:
        names = [f.name for f in fields]
        if len(set(names)) != len(names):
            raise ValueError(f"{name}: duplicate field names")
        for f in fields:
            if f.count < 1:
                raise ValueError(f"{name}.{f.name}: count must be >= 1")
            if f.ctype != "bytes" and f.ctype not in _INT_CODES and f.ctype not in _FLOAT_CODES:
                raise ValueError(f"{name}.{f.name}: unsupported C type {f.ctype!r}")
        self.name = name
        self.fields = list(fields)
        self.order = order
        self.packed = packed
        self.offsets: dict[str, int] = {}
        fmt = order.struct_prefix
        offset = 0
        max_align = 1
        for f in self.fields:
            align = 1 if packed else f.element_size
            max_align = max(max_align, align)
            pad = (-offset) % align
            if pad:
                fmt += f"{pad}x"
                offset += pad
            self.offsets[f.name] = offset
            if f.ctype == "bytes":
                fmt += f"{f.count}s"
            else:
                code = _INT_CODES.get(f.ctype) or _FLOAT_CODES[f.ctype][0]
                fmt += f"{f.count}{code}" if f.count > 1 else code
            offset += f.size
        tail = (-offset) % max_align
        if tail:
            fmt += f"{tail}x"
        self._struct = struct.Struct(fmt)
        self.alignment = max_align

    @property
    def size(self) -> int:
        """``sizeof`` the struct."""
        return self._struct.size

    @property
    def format(self) -> str:
        return self._struct.format

    def pack(self, values: Mapping[str, Any]) -> bytes:
        """Serialize; every field is required and range-checked."""
        missing = [f.name for f in self.fields if f.name not in values]
        extra = [k for k in values if k not in self.offsets]
        if missing or extra:
            raise KeyError(f"{self.name}: missing fields {missing}, unknown fields {extra}")
        flat: list[Any] = []
        for f in self.fields:
            value = values[f.name]
            if f.ctype == "bytes":
                raw = bytes(value)
                if len(raw) != f.count:
                    raise ValueError(
                        f"{self.name}.{f.name}: expected {f.count} bytes, got {len(raw)}"
                    )
                flat.append(raw)
                continue
            items = list(value) if f.count > 1 else [value]
            if len(items) != f.count:
                raise ValueError(f"{self.name}.{f.name}: expected {f.count} elements")
            if f.int_type is not None:
                for item in items:
                    f.int_type.check(item)
            flat.extend(items)
        return self._struct.pack(*flat)

    def unpack(self, data: bytes | bytearray | memoryview, offset: int = 0) -> dict[str, Any]:
        """Deserialize ``size`` bytes starting at ``offset``."""
        if offset < 0 or len(data) - offset < self.size:
            raise ValueError(
                f"{self.name}: need {self.size} bytes at offset {offset}, have {len(data) - offset}"
            )
        flat = iter(self._struct.unpack_from(data, offset))
        result: dict[str, Any] = {}
        for f in self.fields:
            if f.ctype == "bytes" or f.count == 1:
                result[f.name] = next(flat)
            else:
                result[f.name] = [next(flat) for _ in range(f.count)]
        return result
