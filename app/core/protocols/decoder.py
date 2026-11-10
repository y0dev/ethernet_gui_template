"""Protocol decoder interface and registry.

The packet monitor never knows a protocol.  It asks the registry for the
decoder named by the connection and displays whatever fields come back.

No embedded protocol is defined in this package.  Decoders are added either
as declarative definitions (see :mod:`declarative`) or, for a ported C
protocol module, as a :class:`ProtocolDecoder` subclass living next to the
ported code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DecodedField:
    name: str
    value: Any
    offset: int
    length: int
    display: str = ""       # formatted value; falls back to str(value)
    valid: bool = True      # False marks a failed validation (shown in red)
    note: str = ""

    @property
    def text(self) -> str:
        """Display text: ``display`` if set, otherwise ``str(value)``."""
        return self.display or str(self.value)


@dataclass
class DecodedMessage:
    protocol: str
    fields: list[DecodedField] = field(default_factory=list)
    summary: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        """True when there are no errors and every field passed validation."""
        return not self.errors and all(f.valid for f in self.fields)


class ProtocolDecoder(ABC):
    """Decodes one complete frame (as delivered by the connection's framer)."""

    name: str = ""

    @abstractmethod
    def decode(self, frame: bytes) -> DecodedMessage:
        """Decode one complete frame.

        This method must never raise for malformed input. Problems are reported
        in ``DecodedMessage.errors`` instead.

        Args:
            frame: Frame as delivered by the connection's framer.

        Returns:
            The decoded message.
        """


class DecoderRegistry:
    """Name -> decoder lookup shared by the application."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._decoders: dict[str, ProtocolDecoder] = {}

    def register(self, decoder: ProtocolDecoder) -> None:
        """Register a decoder under its name, replacing any previous one.

        Args:
            decoder: Decoder to register.

        Raises:
            ValueError: If the decoder has no name.
        """
        if not decoder.name:
            raise ValueError("decoder must have a name")
        self._decoders[decoder.name] = decoder

    def unregister(self, name: str) -> None:
        """Remove a decoder. Unknown names are ignored.

        Args:
            name: Decoder name.
        """
        self._decoders.pop(name, None)

    def get(self, name: str) -> ProtocolDecoder | None:
        """Look up a decoder.

        Args:
            name: Decoder name.

        Returns:
            The decoder, or None if none is registered under that name.
        """
        return self._decoders.get(name)

    def names(self) -> list[str]:
        """List the registered decoder names.

        Returns:
            The names, sorted.
        """
        return sorted(self._decoders)

    def decode(self, name: str, frame: bytes) -> DecodedMessage | None:
        """Decode a frame with the named decoder.

        An exception raised by the decoder is turned into an error message, so
        a decoder bug cannot take down the monitor.

        Args:
            name: Decoder name.
            frame: Complete frame.

        Returns:
            The decoded message, or None if no decoder has that name.
        """
        decoder = self._decoders.get(name)
        if decoder is None:
            return None
        try:
            return decoder.decode(frame)
        except Exception as exc:  # a decoder bug must not take the monitor down
            return DecodedMessage(name, errors=[f"decoder raised {type(exc).__name__}: {exc}"])
