"""Stream framing: turning a TCP byte stream into application messages.

A receive event never equals one message.  Every framer buffers partial data
and returns zero or more complete frames per :meth:`Framer.feed` call.

Frames are returned as the raw bytes that were on the wire (header and
delimiter included) so the packet monitor shows exactly what was transferred.

These are generic, configurable mechanisms.  They do not define any protocol;
the parameters come from the connection configuration and, for a ported
protocol, from the C source.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from .endian import ByteOrder
from .fixed_int import U8, U16, U32, IntType


class FramingError(ValueError):
    """The stream violates the configured framing (the buffer is reset)."""


class Framer(ABC):
    """Stateful splitter for one direction of one stream."""

    @abstractmethod
    def feed(self, data: bytes) -> list[bytes]:
        """Add received bytes; return all frames that are now complete."""

    @abstractmethod
    def encode(self, payload: bytes) -> bytes:
        """Wrap an application payload for transmission."""

    def unwrap(self, frame: bytes) -> bytes:
        """Application payload of a complete frame (framing bytes removed).

        The inverse of :meth:`encode` for frames produced by :meth:`feed`.
        Stateless, so any instance with the same settings can be used.
        """
        return bytes(frame)

    @abstractmethod
    def reset(self) -> None:
        """Drop buffered partial data (call on disconnect)."""

    @property
    @abstractmethod
    def pending(self) -> int:
        """Number of buffered bytes not yet part of a complete frame."""


class RawFramer(Framer):
    """No framing: every chunk is passed through as received."""

    def feed(self, data: bytes) -> list[bytes]:
        return [bytes(data)] if data else []

    def encode(self, payload: bytes) -> bytes:
        return bytes(payload)

    def reset(self) -> None:
        pass

    @property
    def pending(self) -> int:
        return 0


class _BufferedFramer(Framer):
    def __init__(self, max_frame_length: int) -> None:
        if max_frame_length <= 0:
            raise ValueError("max_frame_length must be > 0")
        self._buf = bytearray()
        self.max_frame_length = max_frame_length

    def reset(self) -> None:
        self._buf.clear()

    @property
    def pending(self) -> int:
        return len(self._buf)

    def _fail(self, message: str) -> None:
        self._buf.clear()
        raise FramingError(message)


class DelimiterFramer(_BufferedFramer):
    """Frames terminated by a byte sequence (e.g. ``\\r\\n``)."""

    def __init__(self, delimiter: bytes, max_frame_length: int = 65536) -> None:
        super().__init__(max_frame_length)
        if not delimiter:
            raise ValueError("delimiter must not be empty")
        self.delimiter = bytes(delimiter)

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        frames: list[bytes] = []
        while True:
            index = self._buf.find(self.delimiter)
            if index < 0:
                break
            end = index + len(self.delimiter)
            if end > self.max_frame_length:
                self._fail(f"frame of {end} bytes exceeds maximum {self.max_frame_length}")
            frames.append(bytes(self._buf[:end]))
            del self._buf[:end]
        if len(self._buf) > self.max_frame_length:
            self._fail(f"no delimiter within {self.max_frame_length} bytes")
        return frames

    def encode(self, payload: bytes) -> bytes:
        return bytes(payload) + self.delimiter

    def unwrap(self, frame: bytes) -> bytes:
        frame = bytes(frame)
        return frame[: -len(self.delimiter)] if frame.endswith(self.delimiter) else frame


class FixedLengthFramer(_BufferedFramer):
    """Frames of a constant size."""

    def __init__(self, length: int) -> None:
        super().__init__(length)
        self.length = length

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        frames = []
        while len(self._buf) >= self.length:
            frames.append(bytes(self._buf[:self.length]))
            del self._buf[:self.length]
        return frames

    def encode(self, payload: bytes) -> bytes:
        if len(payload) != self.length:
            raise FramingError(f"payload must be exactly {self.length} bytes, got {len(payload)}")
        return bytes(payload)


class LengthPrefixedFramer(_BufferedFramer):
    """Frames carrying a length field.

    ``length_offset`` bytes precede the length field.  The total frame size is
    ``header_size + value + length_adjust`` where ``header_size`` is
    ``length_offset + length_size`` and ``value`` is the field content.

    * Field counts only the bytes after it: ``length_adjust = 0`` (default).
    * Field counts the whole frame: ``length_adjust = -header_size``.
    * A trailer (e.g. a 2-byte CRC) not counted by the field:
      ``length_adjust = 2``.
    """

    _FIELD_TYPES: dict[int, IntType] = {1: U8, 2: U16, 4: U32}

    def __init__(
        self,
        length_size: int = 2,
        order: ByteOrder = ByteOrder.BIG,
        length_offset: int = 0,
        length_adjust: int = 0,
        max_frame_length: int = 65536,
    ) -> None:
        super().__init__(max_frame_length)
        if length_size not in self._FIELD_TYPES:
            raise ValueError("length_size must be 1, 2 or 4")
        if length_offset < 0:
            raise ValueError("length_offset must be >= 0")
        self._ctype = self._FIELD_TYPES[length_size]
        self.length_size = length_size
        self.order = order
        self.length_offset = length_offset
        self.length_adjust = length_adjust
        self.header_size = length_offset + length_size

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        frames: list[bytes] = []
        while len(self._buf) >= self.header_size:
            field = self._buf[self.length_offset:self.header_size]
            value = int.from_bytes(field, self.order.value)
            total = self.header_size + value + self.length_adjust
            if total < self.header_size:
                self._fail(f"length field {value} gives a frame shorter than its header")
            if total > self.max_frame_length:
                self._fail(f"frame of {total} bytes exceeds maximum {self.max_frame_length}")
            if len(self._buf) < total:
                break
            frames.append(bytes(self._buf[:total]))
            del self._buf[:total]
        return frames

    def unwrap(self, frame: bytes) -> bytes:
        """Bytes after the header; an uncounted trailer (adjust > 0) is kept
        out of the payload."""
        body = bytes(frame)[self.header_size:]
        return body[: len(body) - self.length_adjust] if self.length_adjust > 0 else body

    def encode(self, payload: bytes) -> bytes:
        """Prefix ``payload`` with its length.

        Only available when the length field is the first header field; a
        header with fields before the length must be built by the protocol
        encoder, which then sends the finished frame through a raw connection
        send.
        """
        if self.length_offset:
            raise FramingError("encode() needs length_offset == 0; build the header in the protocol layer")
        value = len(payload) - self.length_adjust
        if not self._ctype.in_range(value):
            raise FramingError(f"payload length {len(payload)} does not fit the length field")
        frame = value.to_bytes(self.length_size, self.order.value) + bytes(payload)
        if len(frame) > self.max_frame_length:
            raise FramingError(f"frame of {len(frame)} bytes exceeds maximum {self.max_frame_length}")
        return frame


def create_framer(config: Mapping[str, Any] | None) -> Framer:
    """Build a framer from a JSON-style configuration mapping.

    ``{"type": "raw"}``
    ``{"type": "delimiter", "delimiter_hex": "0d0a", "max_frame_length": 4096}``
    ``{"type": "fixed", "length": 16}``
    ``{"type": "length_prefixed", "length_size": 2, "byte_order": "big",
       "length_offset": 0, "length_adjust": 0, "max_frame_length": 65536}``
    """
    config = dict(config or {"type": "raw"})
    kind = config.get("type", "raw")
    try:
        if kind == "raw":
            return RawFramer()
        if kind == "delimiter":
            return DelimiterFramer(
                bytes.fromhex(config["delimiter_hex"]),
                int(config.get("max_frame_length", 65536)),
            )
        if kind == "fixed":
            return FixedLengthFramer(int(config["length"]))
        if kind == "length_prefixed":
            return LengthPrefixedFramer(
                length_size=int(config.get("length_size", 2)),
                order=ByteOrder(config.get("byte_order", "big")),
                length_offset=int(config.get("length_offset", 0)),
                length_adjust=int(config.get("length_adjust", 0)),
                max_frame_length=int(config.get("max_frame_length", 65536)),
            )
    except KeyError as exc:
        raise ValueError(f"framing type {kind!r} requires option {exc.args[0]!r}") from exc
    raise ValueError(f"unknown framing type {kind!r}")
