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
        self.db = db

    def _check_columns(self, names: Iterable[str]) -> None:
        unknown = [n for n in names if n not in self.columns]
        if unknown:
            raise KeyError(f"{self.table}: unknown columns {unknown}")

    def insert_row(self, **values: Any) -> int:
        self._check_columns(values)
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cursor = self.db.execute(
            f"INSERT INTO {self.table} ({names}) VALUES ({marks})", tuple(values.values())
        )
        return self.db.last_id(cursor)

    def update_row(self, row_id: int, **values: Any) -> bool:
        self._check_columns(values)
        if not values:
            return False
        assignments = ", ".join(f"{name} = ?" for name in values)
        cursor = self.db.execute(
            f"UPDATE {self.table} SET {assignments} WHERE id = ?", (*values.values(), row_id)
        )
        return cursor.rowcount > 0

    def delete(self, row_id: int) -> bool:
        cursor = self.db.execute(f"DELETE FROM {self.table} WHERE id = ?", (row_id,))
        return cursor.rowcount > 0

    def get_row(self, row_id: int) -> Row | None:
        row = self.db.query_one(f"SELECT * FROM {self.table} WHERE id = ?", (row_id,))
        return dict(row) if row else None

    def list_rows(
        self,
        where: str = "",
        params: Sequence[Any] = (),
        order_by: str = "id",
        limit: int | None = None,
    ) -> list[Row]:
        sql = f"SELECT * FROM {self.table}"
        if where:
            sql += f" WHERE {where}"
        sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [dict(row) for row in self.db.query(sql, params)]

    def count(self, where: str = "", params: Sequence[Any] = ()) -> int:
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
        return Device(
            id=row["id"], name=row["name"], description=row["description"],
            host=row["host"], port=row["port"], transport=Transport(row["transport"]),
            protocol_name=row["protocol_name"], notes=row["notes"],
        )

    @staticmethod
    def _values(device: Device) -> Row:
        return {
            "name": device.name, "description": device.description, "host": device.host,
            "port": device.port, "transport": device.transport.value,
            "protocol_name": device.protocol_name, "notes": device.notes,
        }

    def add(self, device: Device) -> Device:
        device.id = self.insert_row(**self._values(device))
        return device

    def update(self, device: Device) -> None:
        if device.id is None:
            raise ValueError("device has no id")
        self.db.execute(
            "UPDATE devices SET name=?, description=?, host=?, port=?, transport=?, "
            "protocol_name=?, notes=?, updated_at=datetime('now') WHERE id=?",
            (*self._values(device).values(), device.id),
        )

    def get(self, device_id: int) -> Device | None:
        row = self.get_row(device_id)
        return self._to_model(row) if row else None

    def list(self) -> list[Device]:
        return [self._to_model(r) for r in self.list_rows(order_by="name")]


class ConnectionRepository(Repository):
    table = "connections"
    columns = (
        "name", "device_id", "transport", "host", "port", "local_port", "framing_json",
        "protocol_name", "timeout_ms", "auto_reconnect", "reconnect_interval_ms",
    )

    @staticmethod
    def _to_model(row: Row) -> ConnectionConfig:
        return ConnectionConfig(
            id=row["id"], name=row["name"], device_id=row["device_id"],
            transport=Transport(row["transport"]), host=row["host"], port=row["port"],
            local_port=row["local_port"], framing=json.loads(row["framing_json"]),
            protocol_name=row["protocol_name"], timeout_ms=row["timeout_ms"],
            auto_reconnect=bool(row["auto_reconnect"]),
            reconnect_interval_ms=row["reconnect_interval_ms"],
        )

    @staticmethod
    def _values(config: ConnectionConfig) -> Row:
        return {
            "name": config.name, "device_id": config.device_id,
            "transport": config.transport.value, "host": config.host, "port": config.port,
            "local_port": config.local_port, "framing_json": json.dumps(config.framing),
            "protocol_name": config.protocol_name, "timeout_ms": config.timeout_ms,
            "auto_reconnect": int(config.auto_reconnect),
            "reconnect_interval_ms": config.reconnect_interval_ms,
        }

    def add(self, config: ConnectionConfig) -> ConnectionConfig:
        config.id = self.insert_row(**self._values(config))
        return config

    def update(self, config: ConnectionConfig) -> None:
        if config.id is None:
            raise ValueError("connection has no id")
        self.update_row(config.id, **self._values(config))

    def get(self, connection_id: int) -> ConnectionConfig | None:
        row = self.get_row(connection_id)
        return self._to_model(row) if row else None

    def list(self) -> list[ConnectionConfig]:
        return [self._to_model(r) for r in self.list_rows(order_by="name")]


# -- sessions / packets -------------------------------------------------------
class SessionRepository(Repository):
    table = "sessions"
    columns = ("name", "started_at", "ended_at", "notes", "config_json")

    def start(self, name: str, started_at: float | None = None, config: dict[str, Any] | None = None) -> int:
        return self.insert_row(
            name=name, started_at=started_at or time.time(),
            config_json=json.dumps(config or {}, default=str),
        )

    def get(self, session_id: int) -> Row | None:
        row = self.get_row(session_id)
        if row is not None:
            row["config"] = json.loads(row.pop("config_json") or "{}")
        return row

    def end(self, session_id: int, ended_at: float | None = None) -> None:
        self.update_row(session_id, ended_at=ended_at or time.time())

    def list_with_counts(self) -> list[Row]:
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
    def _params(packet: Packet) -> tuple[Any, ...]:
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
        return Packet(
            id=row["id"], session_id=row["session_id"], connection_id=row["connection_id"],
            timestamp=row["timestamp"], direction=Direction(row["direction"]),
            source=row["source"], destination=row["destination"],
            protocol=row["protocol"], data=bytes(row["data"]),
        )

    def add(self, packet: Packet) -> Packet:
        packet.id = self.db.last_id(self.db.execute(self._INSERT, self._params(packet)))
        return packet

    def add_many(self, packets: Sequence[Packet]) -> int:
        """Insert a batch atomically; returns the number written."""
        if not packets:
            return 0
        with self.db.transaction():
            self.db.executemany(self._INSERT, [self._params(p) for p in packets])
        return len(packets)

    def list_for_session(self, session_id: int, limit: int | None = None) -> list[Packet]:
        rows = self.list_rows("session_id = ?", (session_id,), "timestamp, id", limit)
        return [self._to_model(r) for r in rows]


# -- protocols / tests / logs -------------------------------------------------
class ProtocolRepository(Repository):
    table = "protocols"
    columns = ("name", "description", "decoder_type", "definition_json")

    def upsert(self, name: str, decoder_type: str, definition: dict[str, Any], description: str = "") -> int:
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
        return self.insert_row(
            test_case_id=test_case_id, suite=suite, name=name, executed_at=time.time(),
            status=status, actual_response=actual_response, message=message,
            duration_ms=duration_ms,
        )

    def recent(self, limit: int = 200) -> list[Row]:
        return self.list_rows(order_by="executed_at DESC, id DESC", limit=limit)


class LogRepository(Repository):
    table = "application_logs"
    columns = ("timestamp", "level", "logger", "message", "context_json")

    def add(self, timestamp: float, level: str, logger: str, message: str, context: dict[str, Any]) -> None:
        self.insert_row(
            timestamp=timestamp, level=level, logger=logger, message=message,
            context_json=json.dumps(context, default=str),
        )

    def prune(self, keep: int) -> None:
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
        rows = self.list_rows("project_id = ?", (project_id,), "c_source_path")
        for row in rows:
            row["header_paths"] = json.loads(row.pop("header_paths_json"))
            row["status"] = PortingStatus(row["status"])
        return rows

    def get(self, module_id: int) -> Row | None:
        row = self.get_row(module_id)
        if row:
            row["header_paths"] = json.loads(row.pop("header_paths_json"))
            row["status"] = PortingStatus(row["status"])
        return row

    def find_by_source(self, project_id: int, c_source_path: str) -> Row | None:
        row = self.db.query_one(
            "SELECT id FROM porting_modules WHERE project_id = ? AND c_source_path = ?",
            (project_id, c_source_path),
        )
        return self.get(row["id"]) if row else None

    def set_status(self, module_id: int, status: PortingStatus) -> None:
        self.db.execute(
            "UPDATE porting_modules SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status.value, module_id),
        )

    # dependencies
    def set_dependencies(self, module_id: int, depends_on: Iterable[str], kind: str = "include") -> None:
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
        self.db.execute(
            "INSERT INTO porting_function_mappings (module_id, c_symbol, c_kind, python_symbol, notes) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (module_id, c_symbol, c_kind) "
            "DO UPDATE SET python_symbol = excluded.python_symbol, notes = excluded.notes",
            (module_id, c_symbol, c_kind, python_symbol, notes),
        )

    def delete_mapping(self, mapping_id: int) -> None:
        self.db.execute("DELETE FROM porting_function_mappings WHERE id = ?", (mapping_id,))

    def mappings(self, module_id: int) -> list[Row]:
        rows = self.db.query(
            "SELECT * FROM porting_function_mappings WHERE module_id = ? ORDER BY c_kind, c_symbol",
            (module_id,),
        )
        return [dict(r) for r in rows]

    # notes
    def add_note(self, module_id: int, category: NoteCategory, body: str) -> int:
        cursor = self.db.execute(
            "INSERT INTO porting_notes (module_id, created_at, category, body) VALUES (?, ?, ?, ?)",
            (module_id, time.time(), category.value, body),
        )
        return self.db.last_id(cursor)

    def notes(self, module_id: int) -> list[Row]:
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
        return DeviceOperation(
            id=row["id"], name=row["name"], category=row["category"],
            description=row["description"], payload_format=PayloadFormat(row["payload_format"]),
            payload_text=row["payload_text"], match_mode=MatchMode(row["match_mode"]),
            expected_text=row["expected_text"], timeout_ms=row["timeout_ms"],
            sort_order=row["sort_order"],
        )

    @staticmethod
    def _values(op: DeviceOperation) -> Row:
        return {
            "name": op.name, "category": op.category, "description": op.description,
            "payload_format": op.payload_format.value, "payload_text": op.payload_text,
            "match_mode": op.match_mode.value, "expected_text": op.expected_text,
            "timeout_ms": op.timeout_ms, "sort_order": op.sort_order,
        }

    def save(self, op: DeviceOperation) -> DeviceOperation:
        if op.id is None:
            op.id = self.insert_row(**self._values(op))
        else:
            self.update_row(op.id, **self._values(op))
        return op

    def get(self, op_id: int) -> DeviceOperation | None:
        row = self.get_row(op_id)
        return self._to_model(row) if row else None

    def find(self, name: str) -> DeviceOperation | None:
        row = self.db.query_one("SELECT * FROM device_operations WHERE name = ?", (name,))
        return self._to_model(dict(row)) if row else None

    def list(self) -> list[DeviceOperation]:
        return [self._to_model(r) for r in self.list_rows(order_by="category, sort_order, name")]


class SessionDeviceRepository(Repository):
    table = "session_devices"
    columns = (
        "session_id", "peer", "device_name", "connected_at", "disconnected_at",
        "rx_bytes", "tx_bytes", "rx_frames", "tx_frames",
    )

    def connected(self, session_id: int, peer: str, device_name: str, at: float) -> int:
        return self.insert_row(session_id=session_id, peer=peer, device_name=device_name, connected_at=at)

    def for_session(self, session_id: int) -> list[Row]:
        return self.list_rows("session_id = ?", (session_id,), "connected_at, id")


class OperationResultRepository(Repository):
    table = "operation_results"
    columns = (
        "session_id", "executed_at", "peer", "device_name", "operation_name", "match_mode",
        "request", "expected", "response", "status", "latency_ms", "message",
    )

    def for_session(self, session_id: int) -> list[Row]:
        return [dict(r) for r in self.db.run("report.operation_results", session_id=session_id)]
