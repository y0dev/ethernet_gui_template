"""Owns connection configurations and their live endpoints.

Every frame sent or received on any connection is re-emitted as a
:class:`Packet` through :attr:`ConnectionManager.packet`, which is what the
capture service and packet monitor consume.
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, Signal

from ..compatibility.framing import create_framer
from ..database import ConnectionRepository, Database
from ..models import ConnectionConfig, ConnectionState, Direction, Packet, Transport
from ..networking import Endpoint, create_endpoint

log = logging.getLogger(__name__)


class ConnectionManager(QObject):
    configs_changed = Signal()
    state_changed = Signal(int, str)       # connection id, ConnectionState value
    counters_changed = Signal(int)
    error_occurred = Signal(int, str)
    peers_changed = Signal(int)
    packet = Signal(object)                # Packet

    def __init__(self, db: Database, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.repository = ConnectionRepository(db)
        self._endpoints: dict[int, Endpoint] = {}
        self._runtime: dict[int, ConnectionConfig] = {}  # not stored in the database
        self._next_runtime_id = -1

    # -- configuration ----------------------------------------------------
    @staticmethod
    def validate(config: ConnectionConfig) -> None:
        if not config.name.strip():
            raise ValueError("connection name is required")
        for label, port in (("port", config.port), ("local port", config.local_port)):
            if not 0 <= port <= 65535:
                raise ValueError(f"{label} must be between 0 and 65535")
        if config.transport in (Transport.TCP_CLIENT, Transport.UDP_CLIENT):
            if not config.host or not config.port:
                raise ValueError("client connections need a remote host and port")
        if config.timeout_ms <= 0 or config.reconnect_interval_ms <= 0:
            raise ValueError("timeouts must be positive")
        create_framer(config.framing)  # raises ValueError on bad framing options

    def configs(self) -> list[ConnectionConfig]:
        return self.repository.list()

    def config(self, connection_id: int) -> ConnectionConfig | None:
        if connection_id in self._runtime:
            return self._runtime[connection_id]
        return self.repository.get(connection_id)

    def open_runtime(self, config: ConnectionConfig) -> int:
        """Open a connection that is not stored in the database.

        Used by the session workflow so its server keeps running when the user
        switches to another database file.  Runtime ids are negative.
        """
        self.validate(config)
        connection_id = self._next_runtime_id
        self._next_runtime_id -= 1
        config.id = connection_id
        self._runtime[connection_id] = config
        try:
            self.open(connection_id)
        except Exception:
            self._runtime.pop(connection_id, None)
            raise
        return connection_id

    def runtime_configs(self) -> list[ConnectionConfig]:
        return list(self._runtime.values())

    def close_runtime(self, connection_id: int) -> None:
        self.close(connection_id)
        self._runtime.pop(connection_id, None)

    def save_config(self, config: ConnectionConfig) -> ConnectionConfig:
        self.validate(config)
        if config.id is None:
            self.repository.add(config)
        else:
            if config.id in self._endpoints:
                self.close(config.id)
            self.repository.update(config)
        self.configs_changed.emit()
        return config

    def delete_config(self, connection_id: int) -> None:
        self.close(connection_id)
        self.repository.delete(connection_id)
        self.configs_changed.emit()

    # -- runtime ----------------------------------------------------------
    def endpoint(self, connection_id: int) -> Endpoint | None:
        return self._endpoints.get(connection_id)

    def state(self, connection_id: int) -> ConnectionState:
        endpoint = self._endpoints.get(connection_id)
        return endpoint.state if endpoint else ConnectionState.DISCONNECTED

    def open(self, connection_id: int) -> Endpoint:
        config = self.config(connection_id)
        if config is None:
            raise KeyError(f"connection {connection_id} not found")
        endpoint = self._endpoints.get(connection_id)
        if endpoint is None:
            endpoint = create_endpoint(config)
            endpoint.setParent(self)
            endpoint.setObjectName(config.name)
            self._wire(connection_id, config, endpoint)
            self._endpoints[connection_id] = endpoint
        log.info("opening connection", extra={"context": {"id": connection_id, "name": config.name}})
        endpoint.open()
        return endpoint

    def close(self, connection_id: int) -> None:
        endpoint = self._endpoints.pop(connection_id, None)
        if endpoint is None:
            return
        endpoint.close()
        endpoint.deleteLater()
        self.state_changed.emit(connection_id, ConnectionState.DISCONNECTED.value)
        log.info("connection closed", extra={"context": {"id": connection_id}})

    def close_all(self) -> None:
        for connection_id in list(self._endpoints):
            self.close(connection_id)

    def send(self, connection_id: int, payload: bytes, peer: str | None = None) -> bool:
        endpoint = self._endpoints.get(connection_id)
        if endpoint is None:
            self.error_occurred.emit(connection_id, "send failed: connection is not open")
            return False
        return endpoint.send(payload, peer)

    def _wire(self, connection_id: int, config: ConnectionConfig, endpoint: Endpoint) -> None:
        protocol = config.protocol_name

        def emit_packet(direction: Direction, frame: bytes, peer: str) -> None:
            local = endpoint.local_address() or "local"
            source, destination = (peer, local) if direction is Direction.RX else (local, peer)
            self.packet.emit(
                Packet(
                    timestamp=time.time(), direction=direction, source=source,
                    destination=destination, data=bytes(frame), protocol=protocol,
                    connection_id=connection_id,
                )
            )

        endpoint.frame_received.connect(lambda f, p: emit_packet(Direction.RX, f, p))
        endpoint.frame_sent.connect(lambda f, p: emit_packet(Direction.TX, f, p))
        endpoint.state_changed.connect(lambda s: self.state_changed.emit(connection_id, s))
        endpoint.counters_changed.connect(lambda: self.counters_changed.emit(connection_id))
        endpoint.error_occurred.connect(lambda m: self.error_occurred.emit(connection_id, m))
        endpoint.peer_changed.connect(lambda _p, _c: self.peers_changed.emit(connection_id))
