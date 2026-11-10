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
        return self.display or str(self.value)


@dataclass
class DecodedMessage:
    protocol: str
    fields: list[DecodedField] = field(default_factory=list)
    summary: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors and all(f.valid for f in self.fields)


class ProtocolDecoder(ABC):
    """Decodes one complete frame (as delivered by the connection's framer)."""

    name: str = ""

    @abstractmethod
    def decode(self, frame: bytes) -> DecodedMessage:
        """Never raises for malformed input; report problems in ``errors``."""


class DecoderRegistry:
    """Name -> decoder lookup shared by the application."""

    def __init__(self) -> None:
        self._decoders: dict[str, ProtocolDecoder] = {}

    def register(self, decoder: ProtocolDecoder) -> None:
        if not decoder.name:
            raise ValueError("decoder must have a name")
        self._decoders[decoder.name] = decoder

    def unregister(self, name: str) -> None:
        self._decoders.pop(name, None)

    def get(self, name: str) -> ProtocolDecoder | None:
        return self._decoders.get(name)

    def names(self) -> list[str]:
        return sorted(self._decoders)

    def decode(self, name: str, frame: bytes) -> DecodedMessage | None:
        """Decode with the named decoder; ``None`` when no decoder is registered."""
        decoder = self._decoders.get(name)
        if decoder is None:
            return None
        try:
            return decoder.decode(frame)
        except Exception as exc:  # a decoder bug must not take the monitor down
            return DecodedMessage(name, errors=[f"decoder raised {type(exc).__name__}: {exc}"])
