"""UDP endpoints.  One datagram is one message; no stream framing applies."""

from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QAbstractSocket, QHostAddress, QUdpSocket

from ..models import ConnectionState
from .endpoint import Endpoint, format_peer, parse_peer, resolve_address


class _UdpEndpoint(Endpoint):
    def __init__(self, local_port: int, bind_address: str, parent: QObject | None) -> None:
        """Create an unbound UDP socket and connect its signals.

        Args:
            local_port: Local port to bind; 0 picks a free port.
            bind_address: Local IP address, or ``""`` for any IPv4 interface.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self.local_port = local_port
        self.bind_address = bind_address
        self._socket = QUdpSocket(self)
        self._socket.readyRead.connect(self._on_ready_read)
        self._socket.errorOccurred.connect(self._on_error)

    def _bind(self) -> bool:
        """Bind the socket to the configured local address and port.

        A failure is reported and sets the state to ``ERROR``.

        Returns:
            True if the socket was bound.
        """
        address = (
            QHostAddress(self.bind_address)
            if self.bind_address
            else QHostAddress(QHostAddress.SpecialAddress.AnyIPv4)
        )
        if self._socket.bind(address, self.local_port):
            return True
        self._report_error(f"bind to UDP port {self.local_port} failed: {self._socket.errorString()}")
        self._set_state(ConnectionState.ERROR)
        return False

    def close(self) -> None:
        """Close the socket and mark the endpoint disconnected."""
        self._socket.close()
        self._set_state(ConnectionState.DISCONNECTED)

    def local_address(self) -> str:
        """Return the address the socket is bound to.

        Returns:
            ``host:port``, or ``""`` when not bound.
        """
        if self._socket.state() == QAbstractSocket.SocketState.BoundState:
            return format_peer(self._socket.localAddress(), self._socket.localPort())
        return ""

    def bound_port(self) -> int:
        """Return the local port the socket is bound to.

        Returns:
            The port number, or 0 when not bound.
        """
        return self._socket.localPort()

    def _write_datagram(self, payload: bytes, address: QHostAddress, port: int) -> bool:
        """Send one datagram and count it.

        Args:
            payload: Datagram contents.
            address: Destination address.
            port: Destination port.

        Returns:
            True if the whole datagram was written.
        """
        payload = bytes(payload)
        peer = format_peer(address, port)
        written = self._socket.writeDatagram(payload, address, port)
        if written != len(payload):
            self._report_error(f"send to {peer} failed: {self._socket.errorString()}")
            return False
        self._emit_sent_frame(payload, peer)
        return True

    def _on_error(self, _error: QAbstractSocket.SocketError) -> None:
        """Report a socket error.

        Args:
            _error: Qt error code; the socket's error string is reported instead.
        """
        self._report_error(self._socket.errorString())

    def _on_ready_read(self) -> None:
        """Receive every pending datagram and pass each one on."""
        while self._socket.hasPendingDatagrams():
            datagram = self._socket.receiveDatagram()
            if not datagram.isValid():
                continue
            data = bytes(datagram.data().data())
            peer = format_peer(datagram.senderAddress(), datagram.senderPort())
            self.rx_bytes += len(data)
            self._handle_datagram(data, peer)
        self.counters_changed.emit()

    def _handle_datagram(self, data: bytes, peer: str) -> None:
        """Handle one received datagram by emitting it as a frame.

        Args:
            data: Datagram contents.
            peer: Sender, ``host:port``.
        """
        self._emit_received_frame(data, peer)


class UdpClient(_UdpEndpoint):
    """Sends datagrams to one remote endpoint and receives its replies."""

    def __init__(
        self,
        host: str,
        port: int,
        local_port: int = 0,
        bind_address: str = "",
        parent: QObject | None = None,
    ) -> None:
        """Create a client for one remote endpoint; call :meth:`open` to bind.

        Args:
            host: Remote IP address or ``localhost``. Names are not resolved.
            port: Remote UDP port.
            local_port: Local port to bind; 0 picks a free port.
            bind_address: Local IP address, or ``""`` for any interface.
            parent: Optional Qt parent.
        """
        super().__init__(local_port, bind_address, parent)
        self.host = host
        self.port = port
        self._remote: QHostAddress | None = None

    def open(self) -> None:
        """Resolve the remote address and bind the socket.

        UDP has no connection, so ``CONNECTED`` only means the socket is ready.
        """
        try:
            self._remote = resolve_address(self.host)
        except ValueError as exc:
            self._report_error(str(exc))
            self._set_state(ConnectionState.ERROR)
            return
        if self._bind_socket():
            # UDP is connectionless: "Connected" means the socket is ready.
            self._set_state(ConnectionState.CONNECTED)

    def send(self, payload: bytes, peer: str | None = None) -> bool:
        """Send a datagram to the remote endpoint.

        Args:
            payload: Datagram contents.
            peer: Ignored; the client always sends to its configured remote.

        Returns:
            False if the client is not open or the write failed.
        """
        if self._remote is None or self.state is not ConnectionState.CONNECTED:
            self._report_error("send failed: UDP client is not open")
            return False
        return self._write_datagram(payload, self._remote, self.port)

    def peers(self) -> list[str]:
        """List the remote endpoint while the client is open.

        Returns:
            The single remote peer, or an empty list.
        """
        return [format_peer(self.host, self.port)] if self.state is ConnectionState.CONNECTED else []


class UdpServer(_UdpEndpoint):
    """Listens on a port; replies go to an explicit peer or the last sender."""

    def __init__(self, port: int, bind_address: str = "", parent: QObject | None = None) -> None:
        """Create a server; call :meth:`open` to bind.

        Args:
            port: Local UDP port to listen on.
            bind_address: Local IP address, or ``""`` for any interface.
            parent: Optional Qt parent.
        """
        super().__init__(port, bind_address, parent)
        self._last_peer: str | None = None
        self._known: list[str] = []

    def open(self) -> None:
        """Bind the socket and mark the server as listening."""
        if self._bind_socket():
            self._set_state(ConnectionState.LISTENING)

    def close(self) -> None:
        """Close the socket and forget every known peer."""
        super().close()
        self._known.clear()
        self._last_peer = None

    def send(self, payload: bytes, peer: str | None = None) -> bool:
        """Send a datagram to a peer, or to the last sender when none is given.

        Args:
            payload: Datagram contents.
            peer: Destination ``host:port``, or None for the last sender.

        Returns:
            False if the server is not listening, no target is known, the peer
            is invalid or the write failed.
        """
        target = peer or self._last_peer
        if self.state is not ConnectionState.LISTENING:
            self._report_error("send failed: UDP server is not listening")
            return False
        if target is None:
            self._report_error("send failed: no peer has sent a datagram yet")
            return False
        try:
            host, port = parse_peer(target)
            address = resolve_address(host)
        except ValueError as exc:
            self._report_error(f"send failed: {exc}")
            return False
        return self._write_datagram(payload, address, port)

    def peers(self) -> list[str]:
        """List every peer that has sent a datagram.

        Returns:
            Peer strings in order of first contact.
        """
        return list(self._known)

    def _handle_datagram(self, data: bytes, peer: str) -> None:
        """Remember the sender, announce new peers and emit the datagram.

        Args:
            data: Datagram contents.
            peer: Sender, ``host:port``.
        """
        self._last_peer = peer
        if peer not in self._known:
            self._known.append(peer)
            self.peer_changed.emit(peer, True)
        super()._handle_datagram(data, peer)
