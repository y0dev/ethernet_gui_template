"""Schema migrations, defined as numbered ``.sql`` files.

Files live in ``app/core/database/schema/`` and are named
``NNNN_short_name.sql`` (``0004_add_calibration_table.sql``).  Each file is
one migration; it runs inside a transaction together with its bookkeeping row
in ``schema_migrations``.  ``python tools/new_migration.py "add calibration"``
creates the next file.

Migrations are append-only: never edit one that has been applied to a real
database, add a new one.  Statements are split with SQLite's own
``complete_statement`` so triggers and string literals containing ``;`` work.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .connection import Database

log = logging.getLogger(__name__)

SCHEMA_DIR = Path(__file__).with_name("schema")
_FILE = re.compile(r"^(\d{4})_([A-Za-z0-9_\-]+)\.sql$")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    source: str = ""


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into complete statements.

    SQLite's ``complete_statement`` decides where statements end, so
    triggers and literals containing ``;`` stay intact. Leading comment-only
    lines are dropped and trailing semicolons removed.

    Args:
        sql: Script text.

    Returns:
        The statements in order.

    Raises:
        MigrationError: If the script ends with an incomplete statement.
    """
    statements: list[str] = []
    buffer: list[str] = []
    for line in sql.splitlines():
        if not buffer and (not line.strip() or line.strip().startswith("--")):
            continue
        buffer.append(line)
        candidate = "\n".join(buffer)
        if sqlite3.complete_statement(candidate):
            statement = candidate.strip()
            if statement.endswith(";"):
                statement = statement[:-1].rstrip()
            statements.append(statement)
            buffer = []
    rest = "\n".join(buffer).strip()
    if rest and any(not ln.strip().startswith("--") for ln in rest.splitlines() if ln.strip()):
        raise MigrationError(f"incomplete SQL statement at end of script: {rest[:80]!r}")
    return statements


def load_migrations(directory: Path = SCHEMA_DIR) -> tuple[Migration, ...]:
    """Load and validate every numbered migration file in a folder.

    Args:
        directory: Folder of ``NNNN_name.sql`` files.

    Returns:
        The migrations ordered by version.

    Raises:
        MigrationError: If a file is misnamed or empty, or the version numbers
            are duplicated or not consecutive from 0001.
    """
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILE.match(path.name)
        if not match:
            raise MigrationError(f"{path.name}: migration files must be named NNNN_name.sql")
        statements = tuple(split_statements(path.read_text(encoding="utf-8")))
        if not statements:
            raise MigrationError(f"{path.name}: no SQL statements")
        migrations.append(Migration(int(match.group(1)), match.group(2).replace("_", " "), statements, path.name))
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration numbers in {directory}")
    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        raise MigrationError(f"migration numbers must be consecutive from 0001; found {versions}")
    return tuple(migrations)


MIGRATIONS: tuple[Migration, ...] = load_migrations()

#: Tables created by the template's own migrations (0001-0003) and the
#: migration bookkeeping table.  The Database Explorer and table editor
#: pages hide these by default, so a project database shows only its own
#: tables.  Tables added by your migrations (0004+) are not listed here.
WORKBENCH_TABLES: frozenset[str] = frozenset({
    "application_logs", "connections", "device_operations", "devices", "operation_results",
    "packets", "porting_function_mappings", "porting_module_dependencies", "porting_modules",
    "porting_notes", "porting_projects", "porting_verification_results", "protocols",
    "schema_migrations", "session_devices", "sessions", "test_cases", "test_results",
})


def current_version(db: Database) -> int:
    """Read the schema version of a database.

    Creates the ``schema_migrations`` table if it does not exist.

    Args:
        db: Database to inspect.

    Returns:
        The highest applied migration version, or 0 for a new database.
    """
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return int(db.scalar("SELECT COALESCE(MAX(version), 0) FROM schema_migrations"))


def migrate(db: Database, migrations: tuple[Migration, ...] = MIGRATIONS) -> int:
    """Apply every pending migration in order, each in its own transaction.

    Args:
        db: Database to upgrade.
        migrations: Available migrations; defaults to the bundled schema.

    Returns:
        The schema version after migrating.

    Raises:
        RuntimeError: If the database is newer than the application.
        MigrationError: If a statement fails; that migration is rolled back.
    """
    version = current_version(db)
    latest = max((m.version for m in migrations), default=0)
    if version > latest:
        raise RuntimeError(
            f"database schema version {version} is newer than this application ({latest})"
        )
    for migration in sorted(migrations, key=lambda m: m.version):
        if migration.version <= version:
            continue
        with db.transaction():
            for statement in migration.statements:
                try:
                    db.execute(statement)
                except sqlite3.Error as exc:
                    raise MigrationError(f"{migration.source}: {exc}\n  in: {statement[:200]}") from exc
            db.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
        log.info(
            "applied migration",
            extra={"context": {"version": migration.version, "name": migration.name}},
        )
        version = migration.version
    return version
