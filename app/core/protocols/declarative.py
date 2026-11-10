"""Declarative frame decoder driven by a JSON definition.

Lets a user describe a flat frame layout without writing code.  The definition
is data supplied by the user (stored in the ``protocols`` table); the
application ships none.

Definition format::

    {
      "byte_order": "big",                # default for multi-byte fields
      "fields": [
        {"name": "sync",   "type": "uint8_t",  "expect": 170},
        {"name": "length", "type": "uint16_t"},
        {"name": "flags",  "type": "uint8_t",  "format": "hex",
         "bits": [{"name": "ack", "shift": 0, "width": 1}]},
        {"name": "serial", "type": "bytes", "length": 4},
        {"name": "body",   "type": "bytes", "length_from": "length"},
        {"name": "rest",   "type": "bytes"}            # remainder of frame
      ]
    }

Types: ``uint8_t`` ... ``int64_t``, ``float``, ``double``, ``bytes``, ``ascii``.
Per-field ``byte_order`` overrides the default.  ``enum`` maps integer values
to names: ``{"1": "READ", "2": "WRITE"}``.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from typing import Any

from ..compatibility.bits import get_field
from ..compatibility.endian import ByteOrder
from ..compatibility.fixed_int import BY_NAME
from ..hexutil import format_hex, to_ascii
from .decoder import DecodedField, DecodedMessage, ProtocolDecoder

_FLOATS = {"float": ("f", 4), "double": ("d", 8)}


class DefinitionError(ValueError):
    """The decoder definition itself is invalid."""


class DeclarativeDecoder(ProtocolDecoder):
    decoder_type = "declarative"

    def __init__(self, name: str, definition: Mapping[str, Any]) -> None:
        """Create a decoder from a JSON definition, validating it first.

        Args:
            name: Protocol name.
            definition: Layout definition in the format shown in the module
                docstring.

        Raises:
            DefinitionError: If the definition is invalid.
        """
        self.name = name
        self.definition = dict(definition)
        try:
            self._order = ByteOrder(self.definition.get("byte_order", "big"))
        except ValueError as exc:
            raise DefinitionError(str(exc)) from exc
        self._fields = list(self.definition.get("fields", []))
        self._validate()

    def _validate(self) -> None:
        """Check every field specification in the definition.

        Raises:
            DefinitionError: If there are no fields, a name is missing or
                duplicated, a type or byte order is unknown, ``length_from`` does
                not name an earlier field, or an open-ended field is not last.
        """
        if not self._fields:
            raise DefinitionError("definition has no fields")
        seen: set[str] = set()
        for index, spec in enumerate(self._fields):
            name = spec.get("name")
            ftype = spec.get("type")
            if not name or name in seen:
                raise DefinitionError(f"field {index}: missing or duplicate name {name!r}")
            seen.add(name)
            if ftype not in BY_NAME and ftype not in _FLOATS and ftype not in ("bytes", "ascii"):
                raise DefinitionError(f"field {name!r}: unknown type {ftype!r}")
            if "byte_order" in spec:
                try:
                    ByteOrder(spec["byte_order"])
                except ValueError as exc:
                    raise DefinitionError(f"field {name!r}: {exc}") from exc
            ref = spec.get("length_from")
            if ref is not None and ref not in seen - {name}:
                raise DefinitionError(f"field {name!r}: length_from {ref!r} must name an earlier field")
            if ftype in ("bytes", "ascii"):
                open_ended = "length" not in spec and ref is None
                if open_ended and index != len(self._fields) - 1:
                    raise DefinitionError(f"field {name!r}: only the last field may omit its length")

    def decode(self, frame: bytes) -> DecodedMessage:
        """Decode a frame field by field according to the definition.

        Decoding stops at the first field that does not fit. Undescribed
        trailing bytes are reported as an error.

        Args:
            frame: Complete frame.

        Returns:
            The decoded fields, bit sub-fields, a summary of the first four
            fields, and any errors.
        """
        message = DecodedMessage(self.name)
        values: dict[str, Any] = {}
        offset = 0
        for spec in self._fields:
            name = spec["name"]
            ftype = spec["type"]
            order = ByteOrder(spec.get("byte_order", self._order.value))
            if ftype in BY_NAME:
                size = BY_NAME[ftype].size
            elif ftype in _FLOATS:
                size = _FLOATS[ftype][1]
            elif "length" in spec:
                size = int(spec["length"])
            elif "length_from" in spec:
                size = values[spec["length_from"]]
                if not isinstance(size, int) or size < 0:
                    message.errors.append(f"{name}: length field {spec['length_from']!r} is not usable")
                    break
            else:
                size = len(frame) - offset
            if offset + size > len(frame):
                message.errors.append(
                    f"{name}: needs {size} bytes at offset {offset}, frame has {len(frame)}"
                )
                break
            raw = frame[offset:offset + size]
            valid = True
            note = ""
            if ftype in BY_NAME:
                ctype = BY_NAME[ftype]
                value: Any = int.from_bytes(raw, order.value, signed=ctype.signed)
                if spec.get("format") == "hex":
                    display = f"0x{value & ctype.mask:0{ctype.size * 2}X}"
                else:
                    display = str(value)
                enum = spec.get("enum")
                if enum:
                    label = enum.get(str(value))
                    display += f" ({label})" if label else " (unknown)"
                if "expect" in spec and value != spec["expect"]:
                    valid = False
                    note = f"expected {spec['expect']}"
            elif ftype in _FLOATS:
                value = struct.unpack(order.struct_prefix + _FLOATS[ftype][0], raw)[0]
                display = repr(value)
            elif ftype == "ascii":
                value = raw
                display = to_ascii(raw)
            else:
                value = raw
                display = format_hex(raw)
            values[name] = value
            message.fields.append(DecodedField(name, value, offset, size, display, valid, note))
            for bit_spec in spec.get("bits", []) if ftype in BY_NAME else []:
                sub = get_field(value & BY_NAME[ftype].mask, int(bit_spec["shift"]), int(bit_spec["width"]))
                message.fields.append(
                    DecodedField(f"{name}.{bit_spec['name']}", sub, offset, size, str(sub))
                )
            offset += size
        else:
            if offset != len(frame):
                message.errors.append(f"{len(frame) - offset} trailing bytes not described by the definition")
        message.summary = ", ".join(
            f"{f.name}={f.text}" for f in message.fields[:4] if "." not in f.name
        )
        return message


def build_decoder(name: str, decoder_type: str, definition: Mapping[str, Any]) -> ProtocolDecoder:
    """Build a decoder for a row of the ``protocols`` table.

    Args:
        name: Protocol name.
        decoder_type: Decoder kind; only ``"declarative"`` is supported.
        definition: Decoder definition.

    Returns:
        The decoder.

    Raises:
        DefinitionError: If the type is unknown or the definition is invalid.
    """
    if decoder_type == DeclarativeDecoder.decoder_type:
        return DeclarativeDecoder(name, definition)
    raise DefinitionError(f"unknown decoder type {decoder_type!r}")
