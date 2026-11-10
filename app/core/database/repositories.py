"""Repositories: the only place that contains SQL for application data.

Services depend on these classes, never on SQL.  All repositories share one
:class:`Database`; multi-statement operations use ``db.transaction()``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Sequence
from typing import Any

from ..models import (
    ConnectionConfig,
    Device,
    DeviceOperation,
    Direction,
    MatchMode,
    NoteCategory,
    Packet,
    PayloadFormat,
    PortingStatus,
    ReferenceKind,
    Transport,
)
from .connection import Database

Row = dict[str, Any]


class Repository:
    """Generic CRUD over one table with a fixed column whitelist."""

    table: str = ""
    columns: tuple[str, ...] = ()

    def __init__(self, db: Database) -> None:
        """Bind the repository to a database.

        Args:
            db: Shared database.
        """
        self.db = db

    def _check_columns(self, names: Iterable[str]) -> None:
        """Reject column names that are not in the whitelist.

        Args:
            names: Column names to check.

        Raises:
            KeyError: If any name is not in ``columns``.
        """
        unknown = [n for n in names if n not in self.columns]
        if unknown:
            raise KeyError(f"{self.table}: unknown columns {unknown}")

    def insert_row(self, **values: Any) -> int:
        """Insert one row into the table.

        Args:
            **values: Column values; every name must be whitelisted.

        Returns:
            The new row id.

        Raises:
            KeyError: If a column is not whitelisted.
        """
        self._check_columns(values)
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cursor = self.db.execute(
            f"INSERT INTO {self.table} ({names}) VALUES ({marks})", tuple(values.values())
        )
        return self.db.last_id(cursor)

    def update_row(self, row_id: int, **values: Any) -> bool:
        """Update columns of one row.

        Args:
            row_id: Id of the row to change.
            **values: Column values; every name must be whitelisted.

        Returns:
            True if a row was updated, False if none matched or no values were
            given.

        Raises:
            KeyError: If a column is not whitelisted.
        """
        self._check_columns(values)
        if not values:
            return False
        assignments = ", ".join(f"{name} = ?" for name in values)
        cursor = self.db.execute(
            f"UPDATE {self.table} SET {assignments} WHERE id = ?", (*values.values(), row_id)
        )
        return cursor.rowcount > 0

    def delete(self, row_id: int) -> bool:
        """Delete one row by id.

        Args:
            row_id: Id of the row to delete.

        Returns:
            True if a row was deleted.
        """
        cursor = self.db.execute(f"DELETE FROM {self.table} WHERE id = ?", (row_id,))
        return cursor.rowcount > 0

    def get_row(self, row_id: int) -> Row | None:
        """Fetch one row by id.

        Args:
            row_id: Id of the row.

        Returns:
            The row as a dict, or None if it does not exist.
        """
        row = self.db.query_one(f"SELECT * FROM {self.table} WHERE id = ?", (row_id,))
        return dict(row) if row else None

    def list_rows(
        self,
        where: str = "",
        params: Sequence[Any] = (),
        order_by: str = "id",
        limit: int | None = None,
    ) -> list[Row]:
        """Fetch rows with optional filtering, ordering and limit.

        Args:
            where: SQL condition without ``WHERE``; trusted code only.
            params: Placeholder values for ``where``.
            order_by: SQL ``ORDER BY`` expression; trusted code only.
            limit: Maximum number of rows, or None for all.

        Returns:
            The rows as dicts.
        """
        sql = f"SELECT * FROM {self.table}"
        if where:
            sql += f" WHERE {where}"
        sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [dict(row) for row in self.db.query(sql, params)]

    def count(self, where: str = "", params: Sequence[Any] = ()) -> int:
        """Count rows, optionally filtered.

        Args:
            where: SQL condition without ``WHERE``; trusted code only.
            params: Placeholder values for ``where``.

        Returns:
            The number of matching rows.
        """
        sql = f"SELECT COUNT(*) AS n FROM {self.table}"
        if where:
            sql += f" WHERE {where}"
        return int(self.db.scalar(sql, params))


# -- devices / connections ----------------------------------------------------
class DeviceRepository(Repository):
    table = "devices"
    columns = ("name", "description", "host", "port", "transport", "protocol_name", "notes", "updated_at")

    @staticmethod
    def _to_model(row: Row) -> Device:
        """Convert a ``devices`` row to a model.

        Args:
            row: Database row.

        Returns:
            The device.
        """
        return Device(
            id=row["id"], name=row["name"], description=row["description"],
            host=row["host"], port=row["port"], transport=Transport(row["transport"]),
            protocol_name=row["protocol_name"], notes=row["notes"],
        )

    @staticmethod
    def _to_columns(device: Device) -> Row:
        """Map a device to its column values, excluding ``id``.

        Args:
            device: Device to store.

        Returns:
            Column name to value.
        """
        return {
            "name": device.name, "description": device.description, "host": device.host,
            "port": device.port, "transport": device.transport.value,
            "protocol_name": device.protocol_name, "notes": device.notes,
        }

    def add(self, device: Device) -> Device:
        """Insert a device and assign its new id.

        Args:
            device: Device without an id.

        Returns:
            The same device with ``id`` set.
        """
        device.id = self.insert_row(**self._to_columns(device))
        return device

    def update(self, device: Device) -> None:
        """Write a device's fields and refresh ``updated_at``.

        Args:
            device: Device with an id.

        Raises:
            ValueError: If the device has no id.
        """
        if device.id is None:
            raise ValueError("device has no id")
        self.db.execute(
            "UPDATE devices SET name=?, description=?, host=?, port=?, transport=?, "
            "protocol_name=?, notes=?, updated_at=datetime('now') WHERE id=?",
            (*self._to_columns(device).values(), device.id),
        )

    def get(self, device_id: int) -> Device | None:
        """Fetch one device.

        Args:
            device_id: Device id.

        Returns:
            The device, or None if it does not exist.
        """
        row = self.get_row(device_id)
        return self._to_model(row) if row else None

    def list(self) -> list[Device]:
        """List every device.

        Returns:
            The devices ordered by name.
        """
        return [self._to_model(r) for r in self.list_rows(order_by="name")]


class ConnectionRepository(Repository):
    table = "connections"
    columns = (
        "name", "device_id", "transport", "host", "port", "local_port", "framing_json",
        "protocol_name", "timeout_ms", "auto_reconnect", "reconnect_interval_ms",
    )

    @staticmethod
    def _to_model(row: Row) -> ConnectionConfig:
        """Convert a ``connections`` row to a model, decoding the framing JSON.

        Args:
            row: Database row.

        Returns:
            The connection configuration.
        """
        return ConnectionConfig(
            id=row["id"], name=row["name"], device_id=row["device_id"],
            transport=Transport(row["transport"]), host=row["host"], port=row["port"],
            local_port=row["local_port"], framing=json.loads(row["framing_json"]),
            protocol_name=row["protocol_name"], timeout_ms=row["timeout_ms"],
            auto_reconnect=bool(row["auto_reconnect"]),
            reconnect_interval_ms=row["reconnect_interval_ms"],
        )

    @staticmethod
    def _to_columns(config: ConnectionConfig) -> Row:
        """Map a connection configuration to its column values, excluding ``id``.

        Args:
            config: Configuration to store.

        Returns:
            Column name to value.
        """
        return {
            "name": config.name, "device_id": config.device_id,
            "transport": config.transport.value, "host": config.host, "port": config.port,
            "local_port": config.local_port, "framing_json": json.dumps(config.framing),
            "protocol_name": config.protocol_name, "timeout_ms": config.timeout_ms,
            "auto_reconnect": int(config.auto_reconnect),
            "reconnect_interval_ms": config.reconnect_interval_ms,
        }

    def add(self, config: ConnectionConfig) -> ConnectionConfig:
        """Insert a connection configuration and assign its new id.

        Args:
            config: Configuration without an id.

        Returns:
            The same configuration with ``id`` set.
        """
        config.id = self.insert_row(**self._to_columns(config))
        return config

    def update(self, config: ConnectionConfig) -> None:
        """Write a connection configuration's fields.

        Args:
            config: Configuration with an id.

        Raises:
            ValueError: If the configuration has no id.
        """
        if config.id is None:
            raise ValueError("connection has no id")
        self.update_row(config.id, **self._to_columns(config))

    def get(self, connection_id: int) -> ConnectionConfig | None:
        """Fetch one connection configuration.

        Args:
            connection_id: Connection id.

        Returns:
            The configuration, or None if it does not exist.
        """
        row = self.get_row(connection_id)
        return self._to_model(row) if row else None

    def list(self) -> list[ConnectionConfig]:
        """List every connection configuration.

        Returns:
            The configurations ordered by name.
        """
        return [self._to_model(r) for r in self.list_rows(order_by="name")]


# -- sessions / packets -------------------------------------------------------
class SessionRepository(Repository):
    table = "sessions"
    columns = ("name", "started_at", "ended_at", "notes", "config_json")

    def start(self, name: str, started_at: float | None = None, config: dict[str, Any] | None = None) -> int:
        """Create a session row.

        Args:
            name: Session name.
            started_at: Start time in epoch seconds; defaults to now.
            config: Settings snapshot stored as JSON.

        Returns:
            The new session id.
        """
        return self.insert_row(
            name=name, started_at=started_at or time.time(),
            config_json=json.dumps(config or {}, default=str),
        )

    def get(self, session_id: int) -> Row | None:
        """Fetch one session with its configuration decoded.

        Args:
            session_id: Session id.

        Returns:
            The session row with a ``config`` dict, or None if it does not exist.
        """
        row = self.get_row(session_id)
        if row is not None:
            row["config"] = json.loads(row.pop("config_json") or "{}")
        return row

    def end(self, session_id: int, ended_at: float | None = None) -> None:
        """Mark a session as ended.

        Args:
            session_id: Session id.
            ended_at: End time in epoch seconds; defaults to now.
        """
        self.update_row(session_id, ended_at=ended_at or time.time())

    def list_with_counts(self) -> list[Row]:
        """List sessions with their packet and result counts.

        Returns:
            Rows from the ``sessions.list_with_counts`` query.
        """
        return [dict(r) for r in self.db.run("sessions.list_with_counts")]


class PacketRepository(Repository):
    table = "packets"
    columns = (
        "session_id", "connection_id", "timestamp", "direction", "source",
        "destination", "protocol", "length", "data",
    )
    _INSERT = (
        "INSERT INTO packets (session_id, connection_id, timestamp, direction, source, "
        "destination, protocol, length, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    @staticmethod
    def _insert_params(packet: Packet) -> tuple[Any, ...]:
        """Build the INSERT parameters for a packet.

        Runtime-only connections have negative ids and no ``connections`` row,
        so their connection id is stored as NULL.

        Args:
            packet: Packet to store.

        Returns:
            Values in the order of the INSERT statement.

        Raises:
            ValueError: If the packet has no session id.
        """
        if packet.session_id is None:
            raise ValueError("packet has no session_id")
        # Runtime-only connections (negative ids, e.g. the workflow server)
        # have no row in ``connections``; their packets keep the addresses only.
        connection_id = packet.connection_id if (packet.connection_id or 0) > 0 else None
        return (
            packet.session_id, connection_id, packet.timestamp,
            packet.direction.value, packet.source, packet.destination,
            packet.protocol, len(packet.data), bytes(packet.data),
        )

    @staticmethod
    def _to_model(row: Row) -> Packet:
        """Convert a ``packets`` row to a model.

        Args:
            row: Database row.

        Returns:
            The packet.
        """
        return Packet(
            id=row["id"], session_id=row["session_id"], connection_id=row["connection_id"],
            timestamp=row["timestamp"], direction=Direction(row["direction"]),
            source=row["source"], destination=row["destination"],
            protocol=row["protocol"], data=bytes(row["data"]),
        )

    def add(self, packet: Packet) -> Packet:
        """Insert one packet and assign its new id.

        Args:
            packet: Packet with a session id.

        Returns:
            The same packet with ``id`` set.
        """
        packet.id = self.db.last_id(self.db.execute(self._INSERT, self._insert_params(packet)))
        return packet

    def add_many(self, packets: Sequence[Packet]) -> int:
        """Insert a batch of packets atomically.

        Args:
            packets: Packets with session ids.

        Returns:
            The number of packets written.
        """
        if not packets:
            return 0
        with self.db.transaction():
            self.db.executemany(self._INSERT, [self._insert_params(p) for p in packets])
        return len(packets)

    def list_for_session(self, session_id: int, limit: int | None = None) -> list[Packet]:
        """List the packets of one session in time order.

        Args:
            session_id: Session id.
            limit: Maximum number of packets, or None for all.

        Returns:
            The packets.
        """
        rows = self.list_rows("session_id = ?", (session_id,), "timestamp, id", limit)
        return [self._to_model(r) for r in rows]


# -- protocols / tests / logs -------------------------------------------------
class ProtocolRepository(Repository):
    table = "protocols"
    columns = ("name", "description", "decoder_type", "definition_json")

    def upsert(self, name: str, decoder_type: str, definition: dict[str, Any], description: str = "") -> int:
        """Insert or update a protocol definition by name.

        Args:
            name: Unique protocol name.
            decoder_type: Decoder kind, e.g. ``"declarative"``.
            definition: Decoder definition stored as JSON.
            description: Human-readable description.

        Returns:
            The protocol's row id.
        """
        existing = self.db.query_one("SELECT id FROM protocols WHERE name = ?", (name,))
        payload = json.dumps(definition)
        if existing:
            self.update_row(
                existing["id"], decoder_type=decoder_type,
                definition_json=payload, description=description,
            )
            return int(existing["id"])
        return self.insert_row(
            name=name, decoder_type=decoder_type,
            definition_json=payload, description=description,
        )


class TestCaseRepository(Repository):
    __test__ = False  # not a pytest class
    table = "test_cases"
    columns = ("name", "description", "connection_id", "request", "expected_response", "timeout_ms")


class TestResultRepository(Repository):
    __test__ = False
    table = "test_results"
    columns = (
        "test_case_id", "suite", "name", "executed_at", "status",
        "actual_response", "message", "duration_ms",
    )

    def record(
        self,
        status: str,
        *,
        test_case_id: int | None = None,
        suite: str = "",
        name: str = "",
        actual_response: bytes | None = None,
        message: str = "",
        duration_ms: float = 0.0,
    ) -> int:
        """Store one test result executed now.

        Args:
            status: Result status, e.g. ``"passed"``.
            test_case_id: Test case that ran, if any.
            suite: Suite name for pytest results.
            name: Test name.
            actual_response: Bytes received from the device.
            message: Failure or detail message.
            duration_ms: Execution time in milliseconds.

        Returns:
            The new result id.
        """
        return self.insert_row(
            test_case_id=test_case_id, suite=suite, name=name, executed_at=time.time(),
            status=status, actual_response=actual_response, message=message,
            duration_ms=duration_ms,
        )

    def recent(self, limit: int = 200) -> list[Row]:
        """List the most recent test results.

        Args:
            limit: Maximum number of rows.

        Returns:
            Result rows, newest first.
        """
        return self.list_rows(order_by="executed_at DESC, id DESC", limit=limit)


class LogRepository(Repository):
    table = "application_logs"
    columns = ("timestamp", "level", "logger", "message", "context_json")

    def add(self, timestamp: float, level: str, logger: str, message: str, context: dict[str, Any]) -> None:
        """Store one application log record.

        Args:
            timestamp: Record time in epoch seconds.
            level: Level name.
            logger: Logger name.
            message: Formatted message.
            context: Structured extra data, stored as JSON.
        """
        self.insert_row(
            timestamp=timestamp, level=level, logger=logger, message=message,
            context_json=json.dumps(context, default=str),
        )

    def prune(self, keep: int) -> None:
        """Delete the oldest log rows beyond a limit.

        Args:
            keep: Number of newest rows to keep.
        """
        self.db.run_write("logs.prune", keep=keep)


# -- porting ------------------------------------------------------------------
class PortingProjectRepository(Repository):
    table = "porting_projects"
    columns = ("name", "description", "c_source_root", "python_target_root")


class PortingModuleRepository(Repository):
    table = "porting_modules"
    columns = (
        "project_id", "name", "c_source_path", "header_paths_json",
        "python_module_path", "status", "test_coverage", "updated_at",
    )

    def list_for_project(self, project_id: int) -> list[Row]:
        """List a porting project's modules with their JSON fields decoded.

        Args:
            project_id: Porting project id.

        Returns:
            Module rows ordered by C source path, with ``header_paths`` and
            ``status`` decoded.
        """
        rows = self.list_rows("project_id = ?", (project_id,), "c_source_path")
        for row in rows:
            row["header_paths"] = json.loads(row.pop("header_paths_json"))
            row["status"] = PortingStatus(row["status"])
        return rows

    def get(self, module_id: int) -> Row | None:
        """Fetch one porting module with its JSON fields decoded.

        Args:
            module_id: Module id.

        Returns:
            The module row, or None if it does not exist.
        """
        row = self.get_row(module_id)
        if row:
            row["header_paths"] = json.loads(row.pop("header_paths_json"))
            row["status"] = PortingStatus(row["status"])
        return row

    def find_by_source(self, project_id: int, c_source_path: str) -> Row | None:
        """Find a project's module by its C source path.

        Args:
            project_id: Porting project id.
            c_source_path: Path of the C file, as stored.

        Returns:
            The module row, or None if it is not registered.
        """
        row = self.db.query_one(
            "SELECT id FROM porting_modules WHERE project_id = ? AND c_source_path = ?",
            (project_id, c_source_path),
        )
        return self.get(row["id"]) if row else None

    def set_status(self, module_id: int, status: PortingStatus) -> None:
        """Change a module's porting status and refresh ``updated_at``.

        Args:
            module_id: Module id.
            status: New status.
        """
        self.db.execute(
            "UPDATE porting_modules SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status.value, module_id),
        )

    # dependencies
    def set_dependencies(self, module_id: int, depends_on: Iterable[str], kind: str = "include") -> None:
        """Replace a module's dependencies of one kind.

        Args:
            module_id: Module id.
            depends_on: Names of the dependencies, e.g. included headers.
            kind: Dependency kind, e.g. ``"include"`` or ``"call"``.
        """
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM porting_module_dependencies WHERE module_id = ? AND kind = ?",
                (module_id, kind),
            )
            self.db.executemany(
                "INSERT OR IGNORE INTO porting_module_dependencies (module_id, depends_on, kind) "
                "VALUES (?, ?, ?)",
                [(module_id, dep, kind) for dep in depends_on],
            )

    def dependencies(self, module_id: int) -> list[Row]:
        """List a module's dependencies.

        Args:
            module_id: Module id.

        Returns:
            Rows with ``depends_on`` and ``kind``, ordered by kind then name.
        """
        rows = self.db.query(
            "SELECT depends_on, kind FROM porting_module_dependencies "
            "WHERE module_id = ? ORDER BY kind, depends_on",
            (module_id,),
        )
        return [dict(r) for r in rows]

    # function mappings
    def upsert_mapping(
        self, module_id: int, c_symbol: str, python_symbol: str = "",
        c_kind: str = "function", notes: str = "",
    ) -> None:
        """Insert or update the Python counterpart of a C symbol.

        Args:
            module_id: Module id.
            c_symbol: C function, type or macro name.
            python_symbol: Name of the Python replacement.
            c_kind: Kind of C symbol.
            notes: Free-text porting notes.
        """
        self.db.execute(
            "INSERT INTO porting_function_mappings (module_id, c_symbol, c_kind, python_symbol, notes) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (module_id, c_symbol, c_kind) "
            "DO UPDATE SET python_symbol = excluded.python_symbol, notes = excluded.notes",
            (module_id, c_symbol, c_kind, python_symbol, notes),
        )

    def delete_mapping(self, mapping_id: int) -> None:
        """Delete one function mapping.

        Args:
            mapping_id: Mapping id.
        """
        self.db.execute("DELETE FROM porting_function_mappings WHERE id = ?", (mapping_id,))

    def mappings(self, module_id: int) -> list[Row]:
        """List a module's C-to-Python symbol mappings.

        Args:
            module_id: Module id.

        Returns:
            Mapping rows ordered by kind then C symbol.
        """
        rows = self.db.query(
            "SELECT * FROM porting_function_mappings WHERE module_id = ? ORDER BY c_kind, c_symbol",
            (module_id,),
        )
        return [dict(r) for r in rows]

    # notes
    def add_note(self, module_id: int, category: NoteCategory, body: str) -> int:
        """Add a porting note to a module.

        Args:
            module_id: Module id.
            category: Note category.
            body: Note text.

        Returns:
            The new note id.
        """
        cursor = self.db.execute(
            "INSERT INTO porting_notes (module_id, created_at, category, body) VALUES (?, ?, ?, ?)",
            (module_id, time.time(), category.value, body),
        )
        return self.db.last_id(cursor)

    def notes(self, module_id: int) -> list[Row]:
        """List a module's notes in creation order.

        Args:
            module_id: Module id.

        Returns:
            Note rows.
        """
        rows = self.db.query(
            "SELECT * FROM porting_notes WHERE module_id = ? ORDER BY created_at, id", (module_id,)
        )
        return [dict(r) for r in rows]

    # verification results
    def add_verification(
        self,
        module_id: int,
        reference_kind: ReferenceKind,
        functions_tested: Sequence[str],
        passed: int,
        failed: int,
        binary_compatible: bool | None,
        behavioral_differences: str = "",
        remaining_risks: str = "",
        report_path: str = "",
    ) -> int:
        """Record a verification run for a module.

        Args:
            module_id: Module id.
            reference_kind: What the Python port was compared against.
            functions_tested: Names of the functions covered.
            passed: Number of passing checks.
            failed: Number of failing checks.
            binary_compatible: Whether the output matched byte for byte, or None
                if this was not assessed.
            behavioral_differences: Known differences from the C code.
            remaining_risks: Open risks.
            report_path: Path of the detailed report.

        Returns:
            The new verification id.
        """
        cursor = self.db.execute(
            "INSERT INTO porting_verification_results (module_id, executed_at, reference_kind, "
            "functions_tested_json, passed, failed, binary_compatible, behavioral_differences, "
            "remaining_risks, report_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                module_id, time.time(), reference_kind.value, json.dumps(list(functions_tested)),
                passed, failed,
                None if binary_compatible is None else int(binary_compatible),
                behavioral_differences, remaining_risks, report_path,
            ),
        )
        return self.db.last_id(cursor)

    def verifications(self, module_id: int) -> list[Row]:
        """List a module's verification runs with their fields decoded.

        Args:
            module_id: Module id.

        Returns:
            Verification rows, newest first.
        """
        rows = self.db.query(
            "SELECT * FROM porting_verification_results WHERE module_id = ? "
            "ORDER BY executed_at DESC, id DESC",
            (module_id,),
        )
        result = []
        for r in rows:
            row = dict(r)
            row["functions_tested"] = json.loads(row.pop("functions_tested_json"))
            row["reference_kind"] = ReferenceKind(row["reference_kind"])
            if row["binary_compatible"] is not None:
                row["binary_compatible"] = bool(row["binary_compatible"])
            result.append(row)
        return result

    def latest_verification(self, module_id: int) -> Row | None:
        """Return a module's most recent verification run.

        Args:
            module_id: Module id.

        Returns:
            The newest verification row, or None if there is none.
        """
        items = self.verifications(module_id)
        return items[0] if items else None


# -- session workflow ---------------------------------------------------------
class OperationRepository(Repository):
    table = "device_operations"
    columns = (
        "name", "category", "description", "payload_format", "payload_text",
        "match_mode", "expected_text", "timeout_ms", "sort_order",
    )

    @staticmethod
    def _to_model(row: Row) -> DeviceOperation:
        """Convert a ``device_operations`` row to a model.

        Args:
            row: Database row.

        Returns:
            The device operation.
        """
        return DeviceOperation(
            id=row["id"], name=row["name"], category=row["category"],
            description=row["description"], payload_format=PayloadFormat(row["payload_format"]),
            payload_text=row["payload_text"], match_mode=MatchMode(row["match_mode"]),
            expected_text=row["expected_text"], timeout_ms=row["timeout_ms"],
            sort_order=row["sort_order"],
        )

    @staticmethod
    def _to_columns(op: DeviceOperation) -> Row:
        """Map a device operation to its column values, excluding ``id``.

        Args:
            op: Operation to store.

        Returns:
            Column name to value.
        """
        return {
            "name": op.name, "category": op.category, "description": op.description,
            "payload_format": op.payload_format.value, "payload_text": op.payload_text,
            "match_mode": op.match_mode.value, "expected_text": op.expected_text,
            "timeout_ms": op.timeout_ms, "sort_order": op.sort_order,
        }

    def save(self, op: DeviceOperation) -> DeviceOperation:
        """Insert a new operation or update an existing one.

        Args:
            op: Operation to store; inserted when ``id`` is None.

        Returns:
            The same operation with ``id`` set.
        """
        if op.id is None:
            op.id = self.insert_row(**self._to_columns(op))
        else:
            self.update_row(op.id, **self._to_columns(op))
        return op

    def get(self, op_id: int) -> DeviceOperation | None:
        """Fetch one operation.

        Args:
            op_id: Operation id.

        Returns:
            The operation, or None if it does not exist.
        """
        row = self.get_row(op_id)
        return self._to_model(row) if row else None

    def find(self, name: str) -> DeviceOperation | None:
        """Fetch an operation by its unique name.

        Args:
            name: Operation name.

        Returns:
            The operation, or None if no operation has that name.
        """
        row = self.db.query_one("SELECT * FROM device_operations WHERE name = ?", (name,))
        return self._to_model(dict(row)) if row else None

    def list(self) -> list[DeviceOperation]:
        """List every operation.

        Returns:
            Operations ordered by category, sort order and name.
        """
        return [self._to_model(r) for r in self.list_rows(order_by="category, sort_order, name")]


class SessionDeviceRepository(Repository):
    table = "session_devices"
    columns = (
        "session_id", "peer", "device_name", "connected_at", "disconnected_at",
        "rx_bytes", "tx_bytes", "rx_frames", "tx_frames",
    )

    def record_connected(self, session_id: int, peer: str, device_name: str, at: float) -> int:
        """Record that a device connected during a session.

        Args:
            session_id: Session id.
            peer: Peer address, ``host:port``.
            device_name: Name assigned to the device.
            at: Connection time in epoch seconds.

        Returns:
            The new session-device row id.
        """
        return self.insert_row(session_id=session_id, peer=peer, device_name=device_name, connected_at=at)

    def for_session(self, session_id: int) -> list[Row]:
        """List the devices seen in a session.

        Args:
            session_id: Session id.

        Returns:
            Session-device rows in connection order.
        """
        return self.list_rows("session_id = ?", (session_id,), "connected_at, id")


class OperationResultRepository(Repository):
    table = "operation_results"
    columns = (
        "session_id", "executed_at", "peer", "device_name", "operation_name", "match_mode",
        "request", "expected", "response", "status", "latency_ms", "message",
    )

    def for_session(self, session_id: int) -> list[Row]:
        """List a session's operation results for reporting.

        Args:
            session_id: Session id.

        Returns:
            Rows from the ``report.operation_results`` query.
        """
        return [dict(r) for r in self.db.run("report.operation_results", session_id=session_id)]
