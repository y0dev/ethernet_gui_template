"""Plain data models shared by services, repositories and the UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Transport(str, Enum):
    TCP_CLIENT = "tcp_client"
    TCP_SERVER = "tcp_server"
    UDP_CLIENT = "udp_client"
    UDP_SERVER = "udp_server"

    @property
    def label(self) -> str:
        """Human-readable transport name, e.g. ``"TCP Client"``."""
        return {
            "tcp_client": "TCP Client",
            "tcp_server": "TCP Server",
            "udp_client": "UDP Client",
            "udp_server": "UDP Server",
        }[self.value]


class Direction(str, Enum):
    RX = "RX"
    TX = "TX"


class ConnectionState(str, Enum):
    DISCONNECTED = "Disconnected"
    CONNECTING = "Connecting"
    CONNECTED = "Connected"
    LISTENING = "Listening"
    ERROR = "Error"

    @property
    def is_active(self) -> bool:
        """True when the connection is connected or listening."""
        return self in (ConnectionState.CONNECTED, ConnectionState.LISTENING)


class PortingStatus(str, Enum):
    NOT_ANALYZED = "Not Analyzed"
    ANALYZED = "Analyzed"
    READY_TO_PORT = "Ready to Port"
    IN_PROGRESS = "In Progress"
    PORTED = "Ported"
    VERIFICATION_REQUIRED = "Verification Required"
    VERIFIED = "Verified"
    BLOCKED = "Blocked"


class PayloadFormat(str, Enum):
    HEX = "hex"
    ASCII = "ascii"


class MatchMode(str, Enum):
    """How a device response is judged for an operation."""

    NONE = "none"          # no response expected; sending is the whole operation
    ANY = "any"            # any response within the timeout passes
    EXACT = "exact"        # response frame equals the expected bytes
    PREFIX = "prefix"      # response frame starts with the expected bytes
    CONTAINS = "contains"  # expected bytes appear anywhere in the response

    @property
    def label(self) -> str:
        """Human-readable description of the match rule."""
        return {
            "none": "No response expected", "any": "Any response",
            "exact": "Exactly equals", "prefix": "Starts with", "contains": "Contains",
        }[self.value]


class NoteCategory(str, Enum):
    NOTE = "Note"
    COMPATIBILITY_ISSUE = "Compatibility Issue"
    BEHAVIORAL_DIFFERENCE = "Behavioral Difference"


class ReferenceKind(str, Enum):
    """What a verification run was compared against."""

    C_HARNESS = "C harness"            # original C compiled and executed
    GOLDEN_VECTORS = "Golden vectors"  # recorded outputs of the original C
    NONE = "None"                      # Python-only tests; cannot verify


@dataclass
class Device:
    name: str
    host: str = ""
    port: int = 0
    transport: Transport = Transport.TCP_CLIENT
    description: str = ""
    protocol_name: str = ""
    notes: str = ""
    id: int | None = None


@dataclass
class ConnectionConfig:
    name: str
    transport: Transport
    host: str = ""
    port: int = 0
    local_port: int = 0
    framing: dict[str, Any] = field(default_factory=lambda: {"type": "raw"})
    protocol_name: str = ""
    timeout_ms: int = 5000
    auto_reconnect: bool = False
    reconnect_interval_ms: int = 3000
    device_id: int | None = None
    id: int | None = None


@dataclass
class Packet:
    timestamp: float            # seconds since the Unix epoch (UTC)
    direction: Direction
    source: str
    destination: str
    data: bytes
    protocol: str = ""
    connection_id: int | None = None
    session_id: int | None = None
    id: int | None = None

    @property
    def length(self) -> int:
        """Number of payload bytes in the packet."""
        return len(self.data)


@dataclass
class DeviceOperation:
    """A menu entry: bytes to send to a device and how to judge the reply.

    Operations are user data (stored in ``device_operations``); none are
    built into the application.
    """

    name: str
    payload_text: str
    payload_format: PayloadFormat = PayloadFormat.HEX
    match_mode: MatchMode = MatchMode.ANY
    expected_text: str = ""
    timeout_ms: int = 2000
    category: str = "General"
    description: str = ""
    sort_order: int = 0
    id: int | None = None

    def _parse_payload_text(self, text: str) -> bytes:
        """Parse operation text using this operation's payload format.

        Args:
            text: Hex digits, or ASCII with escapes.

        Returns:
            The decoded bytes.

        Raises:
            ValueError: If the text is not valid for the payload format.
        """
        from ..hexutil import parse_ascii, parse_hex

        return parse_hex(text) if self.payload_format is PayloadFormat.HEX else parse_ascii(text)

    @property
    def payload(self) -> bytes:
        """Bytes sent to the device, decoded from ``payload_text``."""
        return self._parse_payload_text(self.payload_text)

    @property
    def expected(self) -> bytes | None:
        """Bytes the response is matched against, or None for ``NONE``/``ANY``."""
        if self.match_mode in (MatchMode.NONE, MatchMode.ANY):
            return None
        return self._parse_payload_text(self.expected_text)

    def validate(self) -> None:
        """Check that the operation can be sent and judged.

        Raises:
            ValueError: If the name or payload is empty, a match mode needs an
                expected response that is missing, or the timeout is not positive.
        """
        if not self.name.strip():
            raise ValueError("operation name is required")
        if not self.payload:
            raise ValueError("operation payload is empty")
        if self.match_mode in (MatchMode.EXACT, MatchMode.PREFIX, MatchMode.CONTAINS) and not self.expected:
            raise ValueError(f"match mode '{self.match_mode.label}' needs an expected response")
        if self.timeout_ms <= 0:
            raise ValueError("timeout must be positive")

    def judge(self, response: bytes) -> bool:
        """Decide whether a device response satisfies this operation's match rule.

        Args:
            response: Response frame received from the device.

        Returns:
            True if the response passes. ``NONE`` operations never pass on a
            response.
        """
        expected = self.expected
        if self.match_mode is MatchMode.ANY:
            return True
        if self.match_mode is MatchMode.NONE or expected is None:
            return False
        if self.match_mode is MatchMode.EXACT:
            return response == expected
        if self.match_mode is MatchMode.PREFIX:
            return response.startswith(expected)
        return expected in response

    def to_dict(self) -> dict[str, Any]:
        """Serialize the operation for JSON export.

        Returns:
            A dict in the import/export format read by :meth:`from_dict`.
        """
        return {
            "name": self.name, "category": self.category, "description": self.description,
            "payload_format": self.payload_format.value, "payload": self.payload_text,
            "match": self.match_mode.value, "expected": self.expected_text,
            "timeout_ms": self.timeout_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], sort_order: int = 0) -> DeviceOperation:
        """Build and validate an operation from its JSON export form.

        Args:
            data: Mapping with at least ``name`` and ``payload``.
            sort_order: Position of the operation in its menu.

        Returns:
            The validated operation.

        Raises:
            KeyError: If ``name`` or ``payload`` is missing.
            ValueError: If a value is invalid.
        """
        op = cls(
            name=str(data["name"]), payload_text=str(data["payload"]),
            payload_format=PayloadFormat(data.get("payload_format", "hex")),
            match_mode=MatchMode(data.get("match", "any")),
            expected_text=str(data.get("expected", "")), timeout_ms=int(data.get("timeout_ms", 2000)),
            category=str(data.get("category", "General")), description=str(data.get("description", "")),
            sort_order=sort_order,
        )
        op.validate()
        return op
