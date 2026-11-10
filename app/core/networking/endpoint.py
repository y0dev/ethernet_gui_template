"""Common interface for all network endpoints.

Endpoints are QObjects driven by the Qt event loop: no call blocks.  Results
arrive through signals.  The transport knows nothing about protocols; it only
splits the stream with the configured framer and reports raw frames.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QAbstractSocket, QHostAddress

from ..compatibility.framing import Framer, RawFramer
from ..models import ConnectionState

log = logging.getLogger(__name__)


def format_peer(address: QHostAddress | str, port: int) -> str:
    """Format an address and port as ``host:port``.

    IPv4-mapped IPv6 addresses are shown as IPv4, and other IPv6 addresses
    are bracketed as ``[addr]:port``.

    Args:
        address: Qt address or host string.
        port: Port number.

    Returns:
        The peer string.
    """
    if isinstance(address, QHostAddress):
        text = address.toString()
        if (
            address.protocol() == QAbstractSocket.NetworkLayerProtocol.IPv6Protocol
            and text.lower().startswith("::ffff:")
        ):
            ipv4 = address.toIPv4Address()
            # PySide6 returns int or (int, ok) depending on version
            value = ipv4[0] if isinstance(ipv4, tuple) else ipv4
            text = QHostAddress(int(value)).toString()
    else:
        text = address
    return f"[{text}]:{port}" if ":" in text else f"{text}:{port}"


def parse_peer(peer: str) -> tuple[str, int]:
    """Split a peer string produced by :func:`format_peer`.

    Args:
        peer: ``host:port`` or ``[ipv6]:port``.

    Returns:
        The host, without brackets, and the port.

    Raises:
        ValueError: If the string has no host or a non-numeric port.
    """
    host, _, port = peer.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"invalid peer address {peer!r}")
    return host.strip("[]"), int(port)


def resolve_address(host: str) -> QHostAddress:
    """Convert a literal IP address or ``localhost`` to a QHostAddress.

    No DNS lookup is made, so the call never blocks.

    Args:
        host: IP address text or ``localhost``.

    Returns:
        The Qt address.

    Raises:
        ValueError: If ``host`` is not an IP address.
    """
    if host.lower() == "localhost":
        return QHostAddress(QHostAddress.SpecialAddress.LocalHost)
    address = QHostAddress(host)
    if address.isNull():
        raise ValueError(
            f"{host!r} is not an IP address (UDP endpoints do not resolve host names)"
        )
    return address


class Endpoint(QObject):
    """Base class: state tracking, counters and signals."""

    #: ConnectionState value (str)
    state_changed = Signal(str)
    #: (frame: bytes, peer: str) - one complete received frame
    frame_received = Signal(object, str)
    #: (frame: bytes, peer: str) - bytes actually written to the socket
    frame_sent = Signal(object, str)
    #: human-readable error description
    error_occurred = Signal(str)
    #: any counter changed
    counters_changed = Signal()
    #: (peer: str, connected: bool) - server endpoints only
    peer_changed = Signal(str, bool)

    def __init__(self, parent: QObject | None = None) -> None:
        """Initialise the endpoint as disconnected with zeroed counters.

        Args:
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self._state = ConnectionState.DISCONNECTED
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.rx_frames = 0
        self.tx_frames = 0
        self.error_count = 0
        self.last_error = ""

    # -- state ------------------------------------------------------------
    @property
    def state(self) -> ConnectionState:
        """Current connection state."""
        return self._state

    def _set_state(self, state: ConnectionState) -> None:
        """Change the state and emit ``state_changed`` if it differs.

        Args:
            state: New state.
        """
        if state is not self._state:
            self._state = state
            self.state_changed.emit(state.value)

    def _report_error(self, message: str) -> None:
        """Count, log and emit an error.

        Args:
            message: Human-readable error description.
        """
        self.error_count += 1
        self.last_error = message
        log.warning("network error", extra={"context": {"endpoint": self.objectName(), "error": message}})
        self.error_occurred.emit(message)
        self.counters_changed.emit()

    def _emit_received_frame(self, frame: bytes, peer: str) -> None:
        """Count a received frame and emit ``frame_received``.

        Received bytes are counted by the subclass when data arrives.

        Args:
            frame: Complete received frame.
            peer: Sender, ``host:port``.
        """
        self.rx_frames += 1
        self.frame_received.emit(frame, peer)

    def _emit_sent_frame(self, frame: bytes, peer: str) -> None:
        """Count a sent frame and emit ``frame_sent`` and ``counters_changed``.

        Args:
            frame: Bytes written to the socket.
            peer: Destination, ``host:port``.
        """
        self.tx_bytes += len(frame)
        self.tx_frames += 1
        self.frame_sent.emit(frame, peer)
        self.counters_changed.emit()

    def reset_counters(self) -> None:
        """Zero the byte, frame and error counters."""
        self.rx_bytes = self.tx_bytes = self.rx_frames = self.tx_frames = self.error_count = 0
        self.counters_changed.emit()

    # -- interface --------------------------------------------------------
    def open(self) -> None:
        """Start connecting or listening without blocking.

        Raises:
            NotImplementedError: In the base class.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Stop and release the socket. Safe to call in any state.

        Raises:
            NotImplementedError: In the base class.
        """
        raise NotImplementedError

    def send(self, payload: bytes, peer: str | None = None) -> bool:
        """Frame and queue a payload for sending.

        Args:
            payload: Application bytes.
            peer: Destination for server endpoints; None means every peer, or the
                only peer.

        Returns:
            False if the payload could not be queued.

        Raises:
            NotImplementedError: In the base class.
        """
        raise NotImplementedError

    def local_address(self) -> str:
        """Return the address the socket is bound to.

        Returns:
            ``host:port``, or ``""`` when not bound.
        """
        return ""

    def peers(self) -> list[str]:
        """List the currently connected remote peers.

        Returns:
            Peer strings; empty for the base class.
        """
        return []


def default_framer_factory() -> Framer:
    """Create the default framer for endpoints without explicit framing.

    Returns:
        A new raw (pass-through) framer.
    """
    return RawFramer()
