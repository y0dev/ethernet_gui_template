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
    """Split a script into complete statements (comment-only lines dropped)."""
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


def current_version(db: Database) -> int:
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return int(db.scalar("SELECT COALESCE(MAX(version), 0) FROM schema_migrations"))


def migrate(db: Database, migrations: tuple[Migration, ...] = MIGRATIONS) -> int:
    """Apply all pending migrations in order; returns the resulting version."""
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
