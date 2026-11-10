"""Asynchronous TCP server; each client gets its own framer instance."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QAbstractSocket, QHostAddress, QTcpServer, QTcpSocket

from ..compatibility.framing import Framer, FramingError
from ..models import ConnectionState
from .endpoint import Endpoint, default_framer_factory, format_peer


class TcpServer(Endpoint):
    def __init__(
        self,
        port: int,
        bind_address: str = "",
        framer_factory: Callable[[], Framer] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.port = port
        self.bind_address = bind_address
        self._framer_factory = framer_factory or default_framer_factory
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        self._server.acceptError.connect(
            lambda _err: self._report_error(self._server.errorString())
        )
        self._clients: dict[str, tuple[QTcpSocket, Framer]] = {}

    # -- interface --------------------------------------------------------
    def open(self) -> None:
        if self._server.isListening():
            return
        address = (
            QHostAddress(self.bind_address)
            if self.bind_address
            else QHostAddress(QHostAddress.SpecialAddress.Any)
        )
        if self._server.listen(address, self.port):
            self._set_state(ConnectionState.LISTENING)
        else:
            self._report_error(f"listen on port {self.port} failed: {self._server.errorString()}")
            self._set_state(ConnectionState.ERROR)

    def close(self) -> None:
        self._server.close()
        for peer, (socket, _framer) in list(self._clients.items()):
            socket.abort()
            self._drop(peer)
        self._set_state(ConnectionState.DISCONNECTED)

    def send(self, payload: bytes, peer: str | None = None) -> bool:
        """Send to ``peer``, or to every connected client when ``peer`` is None."""
        targets = [peer] if peer else list(self._clients)
        if not targets:
            self._report_error("send failed: no clients connected")
            return False
        ok = True
        for target in targets:
            entry = self._clients.get(target)
            if entry is None:
                self._report_error(f"send failed: {target} is not connected")
                ok = False
                continue
            socket, framer = entry
            try:
                frame = framer.encode(bytes(payload))
            except FramingError as exc:
                self._report_error(f"send failed: {exc}")
                ok = False
                continue
            if socket.write(frame) != len(frame):
                self._report_error(f"send to {target} failed: {socket.errorString()}")
                ok = False
                continue
            self._count_tx(frame, target)
        return ok

    def local_address(self) -> str:
        if self._server.isListening():
            return format_peer(self._server.serverAddress(), self._server.serverPort())
        return ""

    def disconnect_peer(self, peer: str) -> bool:
        """Close one client connection; ``False`` if it is not connected."""
        entry = self._clients.get(peer)
        if entry is None:
            return False
        entry[0].disconnectFromHost()
        return True

    def listening_port(self) -> int:
        return self._server.serverPort() if self._server.isListening() else 0

    def peers(self) -> list[str]:
        return list(self._clients)

    # -- internals --------------------------------------------------------
    def _on_new_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            peer = format_peer(socket.peerAddress(), socket.peerPort())
            self._clients[peer] = (socket, self._framer_factory())
            socket.readyRead.connect(lambda p=peer: self._on_ready_read(p))
            socket.disconnected.connect(lambda p=peer: self._on_client_disconnected(p))
            socket.errorOccurred.connect(lambda err, p=peer: self._on_client_error(p, err))
            self.peer_changed.emit(peer, True)

    def _on_ready_read(self, peer: str) -> None:
        entry = self._clients.get(peer)
        if entry is None:
            return
        socket, framer = entry
        data = bytes(socket.readAll().data())
        if not data:
            return
        self.rx_bytes += len(data)
        try:
            frames = framer.feed(data)
        except FramingError as exc:
            self._report_error(f"framing error from {peer}: {exc}")
            frames = []
        for frame in frames:
            self._count_rx(frame, peer)
        self.counters_changed.emit()

    def _on_client_error(self, peer: str, error: QAbstractSocket.SocketError) -> None:
        if error == QAbstractSocket.SocketError.RemoteHostClosedError:
            return  # normal disconnect, handled by disconnected()
        entry = self._clients.get(peer)
        if entry is not None:
            self._report_error(f"{peer}: {entry[0].errorString()}")

    def _on_client_disconnected(self, peer: str) -> None:
        self._drop(peer)

    def _drop(self, peer: str) -> None:
        entry = self._clients.pop(peer, None)
        if entry is None:
            return
        entry[0].deleteLater()
        self.peer_changed.emit(peer, False)
