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
        """Create a stopped server; call :meth:`open` to listen.

        Args:
            port: TCP port to listen on; 0 picks a free port.
            bind_address: Local IP address to bind, or ``""`` for all interfaces.
            framer_factory: Creates one framer per client; defaults to raw.
            parent: Optional Qt parent.
        """
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
        """Start listening. Does nothing if already listening.

        A failure is reported through ``error_occurred`` and sets the state to
        ``ERROR``.
        """
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
        """Stop listening and abort every client connection."""
        self._server.close()
        for peer, (socket, _framer) in list(self._clients.items()):
            socket.abort()
            self._remove_client(peer)
        self._set_state(ConnectionState.DISCONNECTED)

    def send(self, payload: bytes, peer: str | None = None) -> bool:
        """Frame a payload and send it to one client or to every client.

        Args:
            payload: Application bytes.
            peer: Client to send to, or None for every connected client.

        Returns:
            True only if every target was written successfully.
        """
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
            self._emit_sent_frame(frame, target)
        return ok

    def local_address(self) -> str:
        """Return the address the server is listening on.

        Returns:
            ``host:port``, or ``""`` when not listening.
        """
        if self._server.isListening():
            return format_peer(self._server.serverAddress(), self._server.serverPort())
        return ""

    def disconnect_peer(self, peer: str) -> bool:
        """Ask one client connection to close gracefully.

        Args:
            peer: Client as ``host:port``.

        Returns:
            False if that client is not connected.
        """
        entry = self._clients.get(peer)
        if entry is None:
            return False
        entry[0].disconnectFromHost()
        return True

    def listening_port(self) -> int:
        """Return the port the server is listening on.

        Returns:
            The bound port, or 0 when not listening.
        """
        return self._server.serverPort() if self._server.isListening() else 0

    def peers(self) -> list[str]:
        """List the connected clients.

        Returns:
            Client peer strings in connection order.
        """
        return list(self._clients)

    # -- internals --------------------------------------------------------
    def _on_new_connection(self) -> None:
        """Accept every pending client, giving each its own framer."""
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            peer = format_peer(socket.peerAddress(), socket.peerPort())
            self._clients[peer] = (socket, self._framer_factory())
            socket.readyRead.connect(lambda p=peer: self._on_ready_read(p))
            socket.disconnected.connect(lambda p=peer: self._on_client_disconnected(p))
            socket.errorOccurred.connect(lambda err, p=peer: self._on_client_error(p, err))
            self.peer_changed.emit(peer, True)

    def _on_ready_read(self, peer: str) -> None:
        """Read a client's available bytes, split them into frames and emit each one.

        Args:
            peer: Client whose socket has data.
        """
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
            self._emit_received_frame(frame, peer)
        self.counters_changed.emit()

    def _on_client_error(self, peer: str, error: QAbstractSocket.SocketError) -> None:
        """Report a client socket error, ignoring a normal remote close.

        Args:
            peer: Client whose socket failed.
            error: Qt socket error code.
        """
        if error == QAbstractSocket.SocketError.RemoteHostClosedError:
            return  # normal disconnect, handled by disconnected()
        entry = self._clients.get(peer)
        if entry is not None:
            self._report_error(f"{peer}: {entry[0].errorString()}")

    def _on_client_disconnected(self, peer: str) -> None:
        """Forget a client after its socket disconnects.

        Args:
            peer: Client that disconnected.
        """
        self._remove_client(peer)

    def _remove_client(self, peer: str) -> None:
        """Remove a client, schedule its socket for deletion and emit ``peer_changed``.

        Args:
            peer: Client to remove; unknown peers are ignored.
        """
        entry = self._clients.pop(peer, None)
        if entry is None:
            return
        entry[0].deleteLater()
        self.peer_changed.emit(peer, False)
