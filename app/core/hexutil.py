"""HEX/ASCII conversion helpers for user input and display."""

from __future__ import annotations

import re

_HEX_NOISE = re.compile(r"(0[xX])|[\s,:;\-]")


def parse_hex(text: str) -> bytes:
    """Parse user-entered hex: ``"01 02 0A"``, ``"0x01,0x02"``, ``"01020a"``."""
    cleaned = _HEX_NOISE.sub("", text)
    if len(cleaned) % 2:
        raise ValueError("hex input has an odd number of digits")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError(f"invalid hex input: {exc}") from exc


def parse_ascii(text: str) -> bytes:
    """Encode text as bytes, honouring ``\\r \\n \\t \\0 \\\\ \\xNN`` escapes."""
    out = bytearray()
    i = 0
    simple = {"n": 0x0A, "r": 0x0D, "t": 0x09, "0": 0x00, "\\": 0x5C}
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            if nxt == "x":
                digits = text[i + 2:i + 4]
                if len(digits) != 2:
                    raise ValueError("\\x escape needs two hex digits")
                try:
                    out.append(int(digits, 16))
                except ValueError as exc:
                    raise ValueError(f"invalid \\x escape: {digits!r}") from exc
                i += 4
                continue
        try:
            out += ch.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise ValueError(f"character {ch!r} is not a single byte; use HEX mode") from exc
        i += 1
    return bytes(out)


def format_hex(data: bytes, separator: str = " ") -> str:
    return data.hex(separator).upper() if separator else data.hex().upper()


def to_ascii(data: bytes, placeholder: str = ".") -> str:
    """Printable ASCII rendering; everything else becomes ``placeholder``."""
    return "".join(chr(b) if 0x20 <= b < 0x7F else placeholder for b in data)


def hexdump(data: bytes, width: int = 16) -> str:
    """Classic offset / hex / ASCII dump."""
    lines = []
    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        if width == 16 and len(chunk) > 8:
            hex_part = hex_part[:23] + " " + hex_part[23:]
        pad = width * 3 + (1 if width == 16 else 0) - 1
        lines.append(f"{offset:08X}  {hex_part:<{pad}}  |{to_ascii(chunk)}|")
    return "\n".join(lines)
