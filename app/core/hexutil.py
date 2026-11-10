"""HEX/ASCII conversion helpers for user input and display."""

from __future__ import annotations

import re

_HEX_NOISE = re.compile(r"(0[xX])|[\s,:;\-]")


def parse_hex(text: str) -> bytes:
    """Parse hex typed by a user into bytes.

    Accepts forms such as ``"01 02 0A"``, ``"0x01,0x02"`` and ``"01020a"``.
    Whitespace, ``0x`` prefixes and ``, : ; -`` separators are ignored.

    Args:
        text: User-entered hex string.

    Returns:
        The decoded bytes.

    Raises:
        ValueError: If the digit count is odd or a character is not hex.
    """
    cleaned = _HEX_NOISE.sub("", text)
    if len(cleaned) % 2:
        raise ValueError("hex input has an odd number of digits")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError(f"invalid hex input: {exc}") from exc


def parse_ascii(text: str) -> bytes:
    """Encode text as bytes, honouring C-style escapes.

    Supports ``\\r``, ``\\n``, ``\\t``, ``\\0``, ``\\\\`` and ``\\xNN``. Other
    characters must be single Latin-1 bytes.

    Args:
        text: User-entered text.

    Returns:
        The encoded bytes.

    Raises:
        ValueError: If a ``\\x`` escape is malformed or a character does not fit
            in one byte.
    """
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
    """Render bytes as uppercase hex.

    Args:
        data: Bytes to render.
        separator: Text placed between bytes; empty for none.

    Returns:
        The hex string, e.g. ``"01 0A FF"``.
    """
    return data.hex(separator).upper() if separator else data.hex().upper()


def to_ascii(data: bytes, placeholder: str = ".") -> str:
    """Render bytes as printable ASCII.

    Args:
        data: Bytes to render.
        placeholder: Character shown for non-printable bytes.

    Returns:
        The rendered text, one character per byte.
    """
    return "".join(chr(b) if 0x20 <= b < 0x7F else placeholder for b in data)


def hexdump(data: bytes, width: int = 16) -> str:
    """Format bytes as a classic offset, hex and ASCII dump.

    Args:
        data: Bytes to dump.
        width: Bytes per line. A width of 16 adds an extra gap after the
            eighth byte.

    Returns:
        The dump, one line per ``width`` bytes.
    """
    lines = []
    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        if width == 16 and len(chunk) > 8:
            hex_part = hex_part[:23] + " " + hex_part[23:]
        pad = width * 3 + (1 if width == 16 else 0) - 1
        lines.append(f"{offset:08X}  {hex_part:<{pad}}  |{to_ascii(chunk)}|")
    return "\n".join(lines)
