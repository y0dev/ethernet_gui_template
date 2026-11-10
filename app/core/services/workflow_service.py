"""Session workflow: server → database → devices → operations → report.

This service only orchestrates existing pieces:

* the server is a runtime connection in :class:`ConnectionManager` (not stored
  in the database, so it survives a database switch in step 2),
* recording uses :class:`CaptureService` sessions,
* operations run through :class:`OperationRunner`,
* the report is built from what was stored in the session's database.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from ..config import AppConfig
from ..database import Database, OperationRepository, SessionDeviceRepository, current_version, migrate
from ..models import ConnectionConfig, ConnectionState, DeviceOperation, Direction, Packet, Transport
from ..networking import TcpServer
from .capture_service import CaptureService
from .connection_manager import ConnectionManager
from .device_service import DeviceService
from .operation_runner import OperationRunner
from .protocol_service import ProtocolService

log = logging.getLogger(__name__)

SERVER_NAME = "Workflow server"


class WorkflowError(RuntimeError):
    """A workflow action is not possible in the current state."""


@dataclass
class ConnectedDevice:
    peer: str
    name: str
    connected: bool
    connected_at: float
    disconnected_at: float | None = None
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_frames: int = 0
    tx_frames: int = 0
    row_id: int | None = None          # session_devices row for the current session

    @property
    def label(self) -> str:
        """Display name: the assigned name, or the peer address if unnamed."""
        return self.name or self.peer

    @property
    def host(self) -> str:
        """Host part of the peer address, without IPv6 brackets."""
        return self.peer.rpartition(":")[0].strip("[]")


class WorkflowService(QObject):
    server_state_changed = Signal(str)
    devices_changed = Signal()
    session_changed = Signal(object)      # session id or None
    database_changed = Signal(str)        # new database path
    error = Signal(str)

    def __init__(
        self,
        db: Database,
        config: AppConfig,
        connections: ConnectionManager,
        capture: CaptureService,
        runner: OperationRunner,
        devices: DeviceService,
        protocols: ProtocolService,
        save_config: Callable[[], None],
        parent: QObject | None = None,
    ) -> None:
        """Wire the workflow to the shared services.

        Args:
            db: Shared database; it can be switched to another file in step 2.
            config: Runtime configuration that remembers server and database
                choices.
            connections: Hosts the runtime server connection.
            capture: Records session packets.
            runner: Runs device operations.
            devices: Device inventory used to name peers.
            protocols: Reloaded when the database changes.
            save_config: Persists ``config``.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self.db = db
        self.config = config
        self.connections = connections
        self.capture = capture
        self.runner = runner
        self.device_service = devices
        self.protocols = protocols
        self._save_config = save_config
        self.operations = OperationRepository(db)
        self.session_devices = SessionDeviceRepository(db)
        self.server_id: int | None = None
        self.devices: dict[str, ConnectedDevice] = {}
        self.database_confirmed = False
        connections.packet.connect(self._on_packet)
        connections.state_changed.connect(self._on_state_changed)

    # ------------------------------------------------------------------
    # Step 1: server
    # ------------------------------------------------------------------
    @property
    def server_state(self) -> ConnectionState:
        """State of the workflow server; ``DISCONNECTED`` when none is running."""
        return self.connections.state(self.server_id) if self.server_id is not None else ConnectionState.DISCONNECTED

    @property
    def server_running(self) -> bool:
        """True while the workflow server is listening."""
        return self.server_state is ConnectionState.LISTENING

    def server_config(self) -> ConnectionConfig | None:
        """Return the configuration of the running workflow server.

        Returns:
            The server's runtime configuration, or None when stopped.
        """
        return self.connections.config(self.server_id) if self.server_id is not None else None

    def server_address(self) -> str:
        """Return the address the workflow server is bound to.

        Returns:
            ``host:port``, or ``""`` when stopped.
        """
        endpoint = self.connections.endpoint(self.server_id) if self.server_id is not None else None
        return endpoint.local_address() if endpoint else ""

    def start_server(self, host: str, port: int, transport: Transport, framing: dict[str, Any]) -> None:
        """Start, or restart, the workflow server and remember its settings.

        Args:
            host: Bind address; ``""`` or ``0.0.0.0`` listens on every interface.
            port: Port to listen on.
            transport: ``TCP_SERVER`` or ``UDP_SERVER``.
            framing: Stream framing options; UDP always uses raw framing.

        Raises:
            WorkflowError: If the transport or port is invalid, or the server
                could not start listening.
        """
        if transport not in (Transport.TCP_SERVER, Transport.UDP_SERVER):
            raise WorkflowError("the workflow server must be a TCP or UDP server")
        if not 1 <= port <= 65535:
            raise WorkflowError("port must be between 1 and 65535")
        self.stop_server()
        bind = "" if host in ("", "0.0.0.0") else host
        config = ConnectionConfig(
            SERVER_NAME, transport, host=bind, local_port=port,
            framing=framing if transport is Transport.TCP_SERVER else {"type": "raw"},
        )
        connection_id = self.connections.open_runtime(config)
        endpoint = self.connections.endpoint(connection_id)
        assert endpoint is not None
        if endpoint.state is not ConnectionState.LISTENING:
            message = endpoint.last_error or "server did not start"
            self.connections.close_runtime(connection_id)
            raise WorkflowError(message)
        self.server_id = connection_id
        endpoint.peer_changed.connect(self._on_peer_changed)
        self.config.workflow_server_host = host or "0.0.0.0"
        self.config.workflow_server_port = port
        self.config.workflow_server_transport = transport.value
        self.config.workflow_server_framing = dict(config.framing)
        self._persist_config()
        log.info("workflow server listening", extra={"context": {"address": self.server_address(), "transport": transport.value}})
        self.server_state_changed.emit(ConnectionState.LISTENING.value)

    def stop_server(self) -> None:
        """Stop the workflow server, cancel its operations and mark devices disconnected."""
        if self.server_id is None:
            return
        self.runner.cancel(self.server_id)
        for device in self.devices.values():
            if device.connected:
                self._mark_disconnected(device)
        self.connections.close_runtime(self.server_id)
        self.server_id = None
        self.devices_changed.emit()
        self.server_state_changed.emit(ConnectionState.DISCONNECTED.value)

    def _on_state_changed(self, connection_id: int, state: str) -> None:
        """Re-emit state changes of the workflow server connection.

        Args:
            connection_id: Connection whose state changed.
            state: New ``ConnectionState`` value.
        """
        if connection_id == self.server_id:
            self.server_state_changed.emit(state)

    # ------------------------------------------------------------------
    # Step 2: database
    # ------------------------------------------------------------------
    def database_info(self) -> dict[str, Any]:
        """Summarise the current database.

        Returns:
            A dict with ``path``, ``exists``, ``schema_version``, ``sessions``,
            ``operations`` and ``devices``.
        """
        counts = self.db.run_one("stats.database_summary")
        return {
            "path": str(self.db.path),
            "exists": self.db.path.exists(),
            "schema_version": current_version(self.db),
            "sessions": counts["sessions"] if counts else 0,
            "operations": counts["operations"] if counts else 0,
            "devices": counts["devices"] if counts else 0,
        }

    def use_current_database(self) -> None:
        """Confirm the already-open database for this workflow and remember it."""
        self.database_confirmed = True
        self.config.remember_database(str(self.db.path))
        self._persist_config()
        self.database_changed.emit(str(self.db.path))

    def open_database(self, path: str | Path, make_default: bool = False) -> None:
        """Switch every service to another database file.

        The file is created and migrated if it is new. If migration fails, the
        previous database is reopened.

        Args:
            path: Database file ending in ``.db``, ``.sqlite`` or ``.sqlite3``.
            make_default: Also make it the database opened at start-up.

        Raises:
            WorkflowError: If a session is being recorded, the extension is
                wrong, or the file cannot be migrated.
        """
        if self.capture.session_id is not None:
            raise WorkflowError("finish the current session before switching databases")
        path = Path(path).expanduser().resolve()
        if path.suffix.lower() not in (".db", ".sqlite", ".sqlite3"):
            raise WorkflowError("database files must end in .db, .sqlite or .sqlite3")
        previous = self.db.path
        self.capture.flush()
        self.db.reopen(path)
        try:
            migrate(self.db)
        except Exception as exc:
            self.db.reopen(previous)
            raise WorkflowError(f"cannot use {path}: {exc}") from exc
        for problem in self.protocols.load():
            log.warning("protocol not loaded: %s", problem)
        self.database_confirmed = True
        self.config.remember_database(str(path))
        if make_default:
            self.config.database_path = str(path)
        self._persist_config()
        log.info("database opened", extra={"context": {"path": str(path)}})
        self.connections.configs_changed.emit()  # pages listing stored connections reload
        self.database_changed.emit(str(path))

    # ------------------------------------------------------------------
    # Step 3: devices and the session
    # ------------------------------------------------------------------
    @property
    def session_id(self) -> int | None:
        """Id of the session being recorded, or None."""
        return self.capture.session_id

    def start_session(self, name: str) -> int:
        """Start recording a session and register the devices already connected.

        Args:
            name: Session name; defaults to ``"Device session"`` when empty.

        Returns:
            The new session id.

        Raises:
            WorkflowError: If the server is not running or no database has been
                confirmed.
        """
        if not self.server_running:
            raise WorkflowError("start the server first")
        if not self.database_confirmed:
            raise WorkflowError("open a database first")
        server = self.server_config()
        config = {
            "server_address": self.server_address(),
            "transport": server.transport.value if server else "",
            "framing": server.framing if server else {},
            "database": str(self.db.path),
        }
        session_id = self.capture.start_recording(name or "Device session", config)
        now = time.time()
        for device in self.devices.values():
            if device.connected:
                device.row_id = self.session_devices.record_connected(session_id, device.peer, device.name, now)
        log.info("workflow session started", extra={"context": {"session_id": session_id}})
        self.session_changed.emit(session_id)
        return session_id

    def end_session(self) -> int | None:
        """Cancel pending operations, finalise device rows and stop recording.

        Returns:
            The id of the session that ended, or None if none was running.
        """
        session_id = self.capture.session_id
        if session_id is None:
            return None
        if self.server_id is not None:
            self.runner.cancel(self.server_id)
        for device in self.devices.values():
            self._update_device_row(device, final=True)
            device.row_id = None
        self.capture.stop_recording()
        log.info("workflow session ended", extra={"context": {"session_id": session_id}})
        self.session_changed.emit(None)
        return session_id

    def connected_devices(self) -> list[ConnectedDevice]:
        """List the devices that are currently connected.

        Returns:
            The connected devices.
        """
        return [d for d in self.devices.values() if d.connected]

    def rename_device(self, peer: str, name: str, save_to_inventory: bool = False) -> None:
        """Rename a device for the session and optionally save it to the inventory.

        Args:
            peer: Device address.
            name: New name; surrounding whitespace is removed.
            save_to_inventory: Also create or update a device of that name in
                the device inventory.

        Raises:
            WorkflowError: If the peer is unknown.
        """
        device = self.devices.get(peer)
        if device is None:
            raise WorkflowError(f"{peer} is not known")
        device.name = name.strip()
        if device.row_id is not None:
            self.session_devices.update_row(device.row_id, device_name=device.name)
        if save_to_inventory and device.name:
            from ..models import Device

            existing = next((d for d in self.device_service.list() if d.name == device.name), None)
            inventory = existing or Device(device.name)
            inventory.host = device.host
            server = self.server_config()
            if server:
                inventory.transport = Transport.TCP_CLIENT if server.transport is Transport.TCP_SERVER else Transport.UDP_CLIENT
            self.device_service.save(inventory)
        self.devices_changed.emit()

    def disconnect_device(self, peer: str) -> None:
        """Close one TCP device connection.

        Args:
            peer: Device address.

        Raises:
            WorkflowError: If the server is not TCP or the device is not connected.
        """
        endpoint = self.connections.endpoint(self.server_id) if self.server_id is not None else None
        if not isinstance(endpoint, TcpServer):
            raise WorkflowError("only TCP devices can be disconnected (UDP has no connection)")
        if not endpoint.disconnect_peer(peer):
            raise WorkflowError(f"{peer} is not connected")

    def forget_disconnected(self) -> None:
        """Remove every disconnected device from the device list."""
        for peer in [p for p, d in self.devices.items() if not d.connected]:
            del self.devices[peer]
        self.devices_changed.emit()

    def _lookup_inventory_name(self, peer: str) -> str:
        """Find a device name in the inventory by the peer's host address.

        Args:
            peer: Device address.

        Returns:
            The name if exactly one inventory device has that host, otherwise
            ``""``.
        """
        host = peer.rpartition(":")[0].strip("[]")
        matches = [d for d in self.device_service.list() if d.host == host]
        return matches[0].name if len(matches) == 1 else ""

    def _on_peer_changed(self, peer: str, connected: bool) -> None:
        """Track a device connecting to or disconnecting from the server.

        Args:
            peer: Device address.
            connected: True on connect, False on disconnect.
        """
        now = time.time()
        device = self.devices.get(peer)
        if connected:
            if device is None:
                device = ConnectedDevice(peer, self._lookup_inventory_name(peer), True, now)
                self.devices[peer] = device
            else:
                device.connected, device.connected_at, device.disconnected_at = True, now, None
            if self.session_id is not None:
                device.row_id = self.session_devices.record_connected(self.session_id, peer, device.name, now)
            log.info("device connected", extra={"context": {"peer": peer, "name": device.name}})
        elif device is not None:
            self._mark_disconnected(device)
            if self.server_id is not None:
                self.runner.peer_disconnected(self.server_id, peer)
            log.info("device disconnected", extra={"context": {"peer": peer}})
        self.devices_changed.emit()

    def _mark_disconnected(self, device: ConnectedDevice) -> None:
        """Mark a device disconnected now and finalise its session row.

        Args:
            device: Device that disconnected.
        """
        device.connected = False
        device.disconnected_at = time.time()
        self._update_device_row(device, final=True)

    def _update_device_row(self, device: ConnectedDevice, final: bool = False) -> None:
        """Write a device's traffic counters to its session row.

        Args:
            device: Device to store; ignored if it has no session row.
            final: Also set ``disconnected_at``, using now if it is unknown.
        """
        if device.row_id is None:
            return
        values: dict[str, Any] = {
            "rx_bytes": device.rx_bytes, "tx_bytes": device.tx_bytes,
            "rx_frames": device.rx_frames, "tx_frames": device.tx_frames,
        }
        if final and device.disconnected_at is None:
            values["disconnected_at"] = time.time()
        elif device.disconnected_at is not None:
            values["disconnected_at"] = device.disconnected_at
        self.session_devices.update_row(device.row_id, **values)

    def _on_packet(self, packet: Packet) -> None:
        """Add a server packet to its device's traffic counters.

        Args:
            packet: Packet from the connection manager.
        """
        if packet.connection_id is None or packet.connection_id != self.server_id:
            return
        peer = packet.source if packet.direction is Direction.RX else packet.destination
        device = self.devices.get(peer)
        if device is None:
            # UDP: the first datagram announces the peer (peer_changed also fires)
            return
        if packet.direction is Direction.RX:
            device.rx_bytes += packet.length
            device.rx_frames += 1
        else:
            device.tx_bytes += packet.length
            device.tx_frames += 1

    # ------------------------------------------------------------------
    # Step 4: operations
    # ------------------------------------------------------------------
    def list_operations(self) -> list[DeviceOperation]:
        """List every stored device operation.

        Returns:
            Operations ordered by category, sort order and name.
        """
        return self.operations.list()

    def save_operation(self, op: DeviceOperation) -> DeviceOperation:
        """Validate and save an operation, keeping names unique.

        Args:
            op: Operation to save.

        Returns:
            The saved operation with ``id`` set.

        Raises:
            ValueError: If the operation is invalid.
            WorkflowError: If another operation already has that name.
        """
        op.validate()
        existing = self.operations.find(op.name)
        if existing is not None and existing.id != op.id:
            raise WorkflowError(f"an operation named '{op.name}' already exists")
        return self.operations.save(op)

    def delete_operation(self, op_id: int) -> None:
        """Delete an operation.

        Args:
            op_id: Operation id.
        """
        self.operations.delete(op_id)

    def import_operations(self, items: list[dict[str, Any]], replace: bool = False) -> int:
        """Import a command set, updating operations that have the same name.

        Args:
            items: Operations in the format read by ``DeviceOperation.from_dict``.
            replace: Delete every existing operation first.

        Returns:
            The number of operations imported.

        Raises:
            KeyError: If an item lacks ``name`` or ``payload``.
            ValueError: If an item is invalid; nothing is imported.
        """
        ops = [DeviceOperation.from_dict(item, i) for i, item in enumerate(items)]
        with self.db.transaction():
            if replace:
                self.db.execute("DELETE FROM device_operations")
            for op in ops:
                existing = self.operations.find(op.name)
                if existing is not None:
                    op.id = existing.id
                self.operations.save(op)
        return len(ops)

    def export_operations(self) -> list[dict[str, Any]]:
        """Export every operation as a command set.

        Returns:
            Dicts in the format read by ``DeviceOperation.from_dict``.
        """
        return [op.to_dict() for op in self.operations.list()]

    def run_operations(self, peers: list[str], operations: list[DeviceOperation]) -> int:
        """Queue operations, in order, on each selected device.

        Args:
            peers: Addresses of connected devices.
            operations: Operations to run on each device.

        Returns:
            The number of jobs queued.

        Raises:
            WorkflowError: If the server or session is not running, the
                selection is empty, or a device is not connected.
        """
        if self.server_id is None or not self.server_running:
            raise WorkflowError("the server is not running")
        if self.session_id is None:
            raise WorkflowError("start a session first (step 3) so results are recorded")
        if not peers:
            raise WorkflowError("select at least one connected device")
        if not operations:
            raise WorkflowError("select at least one operation")
        queued = 0
        for peer in peers:
            device = self.devices.get(peer)
            if device is None or not device.connected:
                raise WorkflowError(f"{peer} is not connected")
            self.runner.enqueue(self.server_id, peer, operations, self.session_id, device.label)
            queued += len(operations)
        return queued

    # ------------------------------------------------------------------
    def _persist_config(self) -> None:
        """Save the configuration, logging rather than raising on failure."""
        try:
            self._save_config()
        except Exception as exc:  # config is a convenience; never block the workflow
            log.warning("could not save configuration", extra={"context": {"error": str(exc)}})

    def shutdown(self) -> None:
        """End the session and stop the server."""
        self.end_session()
        self.stop_server()
