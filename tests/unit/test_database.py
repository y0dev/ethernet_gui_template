from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from app.core.database import (
    ConnectionRepository,
    Database,
    DeviceRepository,
    LogRepository,
    PacketRepository,
    SessionRepository,
    current_version,
    migrate,
)
from app.core.database.migrations import MIGRATIONS
from app.core.models import ConnectionConfig, Device, Direction, Packet, Transport

REQUIRED_TABLES = {
    "devices", "connections", "sessions", "packets", "protocols", "test_cases",
    "test_results", "application_logs", "porting_projects", "porting_modules",
}


def test_schema_created_with_wal_and_foreign_keys(db):
    """Migration creates every table and enables WAL and foreign keys.

    Args:
        db: Migrated test database.
    """
    assert REQUIRED_TABLES <= set(db.table_names())
    assert db.query_one("PRAGMA journal_mode")[0].lower() == "wal"
    assert db.query_one("PRAGMA foreign_keys")[0] == 1
    assert current_version(db) == max(m.version for m in MIGRATIONS)


def test_migrate_is_idempotent(db):
    """Running migrations again changes nothing.

    Args:
        db: Migrated test database.
    """
    version = current_version(db)
    assert migrate(db) == version


def test_newer_schema_is_refused(db):
    """A database newer than the application is refused.

    Args:
        db: Migrated test database.
    """
    db.execute("INSERT INTO schema_migrations (version, name) VALUES (999, 'future')")
    with pytest.raises(RuntimeError):
        migrate(db)


def test_indexes_exist(db):
    """The query-performance indexes exist.

    Args:
        db: Migrated test database.
    """
    names = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_packets_session_time", "idx_logs_time", "idx_porting_modules_project"} <= names


def test_device_and_connection_round_trip(db):
    """Devices and connections round-trip, and deleting a device unlinks its connections.

    Args:
        db: Migrated test database.
    """
    devices = DeviceRepository(db)
    device = devices.add(Device("PLC-1", "10.0.0.5", 502, Transport.TCP_CLIENT))
    loaded = devices.get(device.id)
    assert loaded == device
    connections = ConnectionRepository(db)
    config = connections.add(ConnectionConfig(
        "c1", Transport.TCP_CLIENT, "10.0.0.5", 502,
        framing={"type": "delimiter", "delimiter_hex": "0a"}, device_id=device.id,
    ))
    assert connections.get(config.id).framing == {"type": "delimiter", "delimiter_hex": "0a"}
    devices.delete(device.id)  # ON DELETE SET NULL
    assert connections.get(config.id).device_id is None


def test_packets_stored_as_blobs_and_cascade(db):
    """Packet data is stored as BLOBs and deleted with its session.

    Args:
        db: Migrated test database.
    """
    sessions = SessionRepository(db)
    packets = PacketRepository(db)
    session_id = sessions.start("s")
    payload = bytes(range(256))
    batch = [
        Packet(time.time() + i, Direction.RX, "a:1", "b:2", payload, session_id=session_id)
        for i in range(10)
    ]
    assert packets.add_many(batch) == 10
    stored = packets.list_for_session(session_id)
    assert len(stored) == 10 and stored[0].data == payload
    row = db.query_one("SELECT typeof(data) AS t FROM packets LIMIT 1")
    assert row["t"] == "blob"
    sessions.delete(session_id)
    assert packets.count() == 0


def test_foreign_key_violation_rejected(db):
    """A packet for a missing session violates the foreign key.

    Args:
        db: Migrated test database.
    """
    with pytest.raises(sqlite3.IntegrityError):
        PacketRepository(db).add(Packet(0.0, Direction.TX, "", "", b"x", session_id=12345))


def test_transaction_rollback_and_savepoint(db):
    """A failing block rolls back, and a failing nested block rolls back only its savepoint.

    Args:
        db: Migrated test database.
    """
    devices = DeviceRepository(db)
    with pytest.raises(RuntimeError):
        with db.transaction():
            devices.add(Device("a"))
            raise RuntimeError
    assert devices.count() == 0
    with db.transaction():
        devices.add(Device("b"))
        with pytest.raises(ValueError):
            with db.transaction():
                devices.add(Device("c"))
                raise ValueError
    assert [d.name for d in devices.list()] == ["b"]


def test_each_thread_gets_its_own_connection(db):
    """Every thread gets its own connection to the same file.

    Args:
        db: Migrated test database.
    """
    main_conn = db.connection()
    seen = []

    def use_database_from_thread():
        """Record whether this thread got a new connection and insert one device."""
        conn = db.connection()
        seen.append(conn is not main_conn)
        DeviceRepository(db).add(Device(f"t{threading.get_ident()}"))

    threads = [threading.Thread(target=use_database_from_thread) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == [True] * 4
    assert DeviceRepository(db).count() == 4


def test_backup(db, tmp_path):
    """A backup is a readable copy, and backing up onto the live file is refused.

    Args:
        db: Migrated test database.
        tmp_path: pytest temporary directory.
    """
    DeviceRepository(db).add(Device("keep"))
    target = db.backup(tmp_path / "backup" / "copy.db")
    copy = Database(target)
    try:
        assert [d.name for d in DeviceRepository(copy).list()] == ["keep"]
    finally:
        copy.close()
    with pytest.raises(ValueError):
        db.backup(db.path)


def test_log_prune(db):
    """Pruning keeps only the newest log rows.

    Args:
        db: Migrated test database.
    """
    logs = LogRepository(db)
    for i in range(20):
        logs.add(float(i), "INFO", "x", f"m{i}", {})
    logs.prune(5)
    assert logs.count() == 5


def test_repository_rejects_unknown_columns(db):
    """Inserting a column that is not whitelisted raises ``KeyError``.

    Args:
        db: Migrated test database.
    """
    with pytest.raises(KeyError):
        DeviceRepository(db).insert_row(name="x", bogus=1)
