"""Asynchronous TCP client with connect timeout, reconnect and stream framing."""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket

from ..compatibility.framing import Framer, FramingError, RawFramer
from ..models import ConnectionState
from .endpoint import Endpoint, format_peer


class TcpClient(Endpoint):
    def __init__(
        self,
        host: str,
        port: int,
        framer: Framer | None = None,
        timeout_ms: int = 5000,
        auto_reconnect: bool = False,
        reconnect_interval_ms: int = 3000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.host = host
        self.port = port
        self.framer = framer or RawFramer()
        self.auto_reconnect = auto_reconnect
        self._wanted = False  # user asked for the connection to be up

        self._socket = QTcpSocket(self)
        self._socket.connected.connect(self._on_connected)
        self._socket.disconnected.connect(self._on_disconnected)
        self._socket.readyRead.connect(self._on_ready_read)
        self._socket.errorOccurred.connect(self._on_error)

        self._connect_timer = QTimer(self)
        self._connect_timer.setSingleShot(True)
        self._connect_timer.setInterval(max(1, timeout_ms))
        self._connect_timer.timeout.connect(self._on_connect_timeout)

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.setInterval(max(100, reconnect_interval_ms))
        self._reconnect_timer.timeout.connect(self._start_connect)

    # -- interface --------------------------------------------------------
    def open(self) -> None:
        self._wanted = True
        self._start_connect()

    def close(self) -> None:
        self._wanted = False
        self._connect_timer.stop()
        self._reconnect_timer.stop()
        self._socket.abort()
        self.framer.reset()
        self._set_state(ConnectionState.DISCONNECTED)

    def send(self, payload: bytes, peer: str | None = None) -> bool:
        if self._socket.state() != QAbstractSocket.SocketState.ConnectedState:
            self._report_error("send failed: not connected")
            return False
        try:
            frame = self.framer.encode(bytes(payload))
        except FramingError as exc:
            self._report_error(f"send failed: {exc}")
            return False
        written = self._socket.write(frame)
        if written != len(frame):
            self._report_error(f"send failed: wrote {written} of {len(frame)} bytes")
            return False
        self._count_tx(frame, self._peer())
        return True

    def local_address(self) -> str:
        if self._socket.state() == QAbstractSocket.SocketState.ConnectedState:
            return format_peer(self._socket.localAddress(), self._socket.localPort())
        return ""

    def peers(self) -> list[str]:
        return [self._peer()] if self.state is ConnectionState.CONNECTED else []

    # -- internals --------------------------------------------------------
    def _peer(self) -> str:
        return format_peer(self.host, self.port)

    def _start_connect(self) -> None:
        if not self._wanted:
            return
        if self._socket.state() != QAbstractSocket.SocketState.UnconnectedState:
            self._socket.abort()
        self.framer.reset()
        self._set_state(ConnectionState.CONNECTING)
        self._connect_timer.start()
        self._socket.connectToHost(self.host, self.port)

    def _on_connected(self) -> None:
        self._connect_timer.stop()
        self._set_state(ConnectionState.CONNECTED)

    def _on_disconnected(self) -> None:
        self.framer.reset()
        if self._wanted:
            self._set_state(ConnectionState.DISCONNECTED)
            self._schedule_reconnect()

    def _on_connect_timeout(self) -> None:
        self._socket.abort()
        self._report_error(f"connection to {self._peer()} timed out")
        self._set_state(ConnectionState.ERROR)
        self._schedule_reconnect()

    def _on_error(self, _error: QAbstractSocket.SocketError) -> None:
        was_connecting = self._connect_timer.isActive()
        self._connect_timer.stop()
        self._report_error(self._socket.errorString())
        if was_connecting or self._socket.state() == QAbstractSocket.SocketState.UnconnectedState:
            self._set_state(ConnectionState.ERROR)
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._wanted and self.auto_reconnect and not self._reconnect_timer.isActive():
            self._reconnect_timer.start()

    def _on_ready_read(self) -> None:
        data = bytes(self._socket.readAll().data())
        if not data:
            return
        self.rx_bytes += len(data)
        try:
            frames = self.framer.feed(data)
        except FramingError as exc:
            self._report_error(f"framing error: {exc}")
            frames = []
        peer = self._peer()
        for frame in frames:
            self._count_rx(frame, peer)
        self.counters_changed.emit()
