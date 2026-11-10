"""UDP endpoints.  One datagram is one message; no stream framing applies."""

from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QAbstractSocket, QHostAddress, QUdpSocket

from ..models import ConnectionState
from .endpoint import Endpoint, format_peer, parse_peer, resolve_address


class _UdpEndpoint(Endpoint):
    def __init__(self, local_port: int, bind_address: str, parent: QObject | None) -> None:
        super().__init__(parent)
        self.local_port = local_port
        self.bind_address = bind_address
        self._socket = QUdpSocket(self)
        self._socket.readyRead.connect(self._on_ready_read)
        self._socket.errorOccurred.connect(self._on_error)

    def _bind(self) -> bool:
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
        self._socket.close()
        self._set_state(ConnectionState.DISCONNECTED)

    def local_address(self) -> str:
        if self._socket.state() == QAbstractSocket.SocketState.BoundState:
            return format_peer(self._socket.localAddress(), self._socket.localPort())
        return ""

    def bound_port(self) -> int:
        return self._socket.localPort()

    def _write(self, payload: bytes, address: QHostAddress, port: int) -> bool:
        payload = bytes(payload)
        peer = format_peer(address, port)
        written = self._socket.writeDatagram(payload, address, port)
        if written != len(payload):
            self._report_error(f"send to {peer} failed: {self._socket.errorString()}")
            return False
        self._count_tx(payload, peer)
        return True

    def _on_error(self, _error: QAbstractSocket.SocketError) -> None:
        self._report_error(self._socket.errorString())

    def _on_ready_read(self) -> None:
        while self._socket.hasPendingDatagrams():
            datagram = self._socket.receiveDatagram()
            if not datagram.isValid():
                continue
            data = bytes(datagram.data().data())
            peer = format_peer(datagram.senderAddress(), datagram.senderPort())
            self.rx_bytes += len(data)
            self._accept(data, peer)
        self.counters_changed.emit()

    def _accept(self, data: bytes, peer: str) -> None:
        self._count_rx(data, peer)


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
        super().__init__(local_port, bind_address, parent)
        self.host = host
        self.port = port
        self._remote: QHostAddress | None = None

    def open(self) -> None:
        try:
            self._remote = resolve_address(self.host)
        except ValueError as exc:
            self._report_error(str(exc))
            self._set_state(ConnectionState.ERROR)
            return
        if self._bind():
            # UDP is connectionless: "Connected" means the socket is ready.
            self._set_state(ConnectionState.CONNECTED)

    def send(self, payload: bytes, peer: str | None = None) -> bool:
        if self._remote is None or self.state is not ConnectionState.CONNECTED:
            self._report_error("send failed: UDP client is not open")
            return False
        return self._write(payload, self._remote, self.port)

    def peers(self) -> list[str]:
        return [format_peer(self.host, self.port)] if self.state is ConnectionState.CONNECTED else []


class UdpServer(_UdpEndpoint):
    """Listens on a port; replies go to an explicit peer or the last sender."""

    def __init__(self, port: int, bind_address: str = "", parent: QObject | None = None) -> None:
        super().__init__(port, bind_address, parent)
        self._last_peer: str | None = None
        self._known: list[str] = []

    def open(self) -> None:
        if self._bind():
            self._set_state(ConnectionState.LISTENING)

    def close(self) -> None:
        super().close()
        self._known.clear()
        self._last_peer = None

    def send(self, payload: bytes, peer: str | None = None) -> bool:
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
        return self._write(payload, address, port)

    def peers(self) -> list[str]:
        return list(self._known)

    def _accept(self, data: bytes, peer: str) -> None:
        self._last_peer = peer
        if peer not in self._known:
            self._known.append(peer)
            self.peer_changed.emit(peer, True)
        super()._accept(data, peer)
