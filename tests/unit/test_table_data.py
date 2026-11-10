"""Generic table access used by the Database Explorer and table editor pages."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from app.core.database import (
    MIGRATIONS,
    WORKBENCH_TABLES,
    Database,
    RowChanges,
    TableData,
    TableDataError,
    quote_identifier,
    user_table_names,
)


@pytest.fixture
def project_db(tmp_path: Path):
    """Provide a database that the workbench did not create, with typed tables.

    Args:
        tmp_path: pytest temporary directory.

    Yields:
        The database, closed after the test.
    """
    db = Database(tmp_path / "plant.db", wal=False)
    db.execute(
        "CREATE TABLE readings (id INTEGER PRIMARY KEY, sensor TEXT NOT NULL, value REAL, "
        "raw BLOB, count INTEGER DEFAULT 0, note TEXT, UNIQUE (sensor, value))"
    )
    db.execute('CREATE TABLE "odd ""name""" (k TEXT PRIMARY KEY, v NUMERIC) WITHOUT ROWID')
    db.execute("INSERT INTO readings (sensor, value, raw) VALUES ('t1', 1.5, x'0102'), ('t2', 2.0, NULL)")
    yield db
    db.close()


def test_workbench_tables_match_the_shipped_migrations():
    """``WORKBENCH_TABLES`` lists exactly the tables created by migrations 0001-0003."""
    created = set()
    for migration in MIGRATIONS:
        if migration.version <= 3:
            for statement in migration.statements:
                created |= set(re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", statement, re.IGNORECASE))
    assert WORKBENCH_TABLES == created | {"schema_migrations"}


def test_user_table_names_hide_workbench_tables(db, project_db):
    """The workbench's own tables are hidden unless asked for; project tables always show.

    Args:
        db: Migrated workbench test database.
        project_db: Database the workbench did not create.
    """
    db.execute("CREATE TABLE calibration (id INTEGER PRIMARY KEY, value REAL)")
    assert user_table_names(db) == ["calibration"]
    assert "sessions" in user_table_names(db, include_workbench=True)
    assert user_table_names(project_db) == ['odd "name"', "readings"]


def test_columns_keys_and_reading(project_db):
    """Columns, affinities and row keys are read from the schema; search and paging work.

    Args:
        project_db: Database the workbench did not create.
    """
    readings = TableData(project_db, "readings")
    assert readings.column_names == ["id", "sensor", "value", "raw", "count", "note"]
    assert [c.affinity for c in readings.columns] == ["INTEGER", "TEXT", "REAL", "BLOB", "INTEGER", "TEXT"]
    assert readings.uses_rowid and readings.key_columns == ["rowid"] and readings.editable
    assert readings.count() == 2 and readings.count("t2") == 1
    rows = readings.fetch(10)
    assert rows[0] == ((1,), (1, "t1", 1.5, b"\x01\x02", 0, None))
    assert [key for key, _ in readings.fetch(1, offset=1)] == [(2,)]
    assert [key for key, _ in readings.fetch(10, newest_first=True)] == [(2,), (1,)]
    assert "CREATE TABLE readings" in readings.schema_sql()

    odd = TableData(project_db, 'odd "name"')
    assert not odd.uses_rowid and odd.key_columns == ["k"] and odd.editable
    assert quote_identifier('odd "name"') == '"odd ""name"""'
    with pytest.raises(TableDataError):
        TableData(project_db, "readings; DROP TABLE readings")


def test_parse_value_follows_column_types(project_db):
    """Typed text is converted by column affinity; bad input and missing values are rejected.

    Args:
        project_db: Database the workbench did not create.
    """
    t = TableData(project_db, "readings")
    assert t.parse_value("count", "007") == 7 and t.parse_value("count", "0x10") == 16
    assert t.parse_value("value", "2.5") == 2.5
    assert t.parse_value("raw", "01 ff") == b"\x01\xff"
    assert t.parse_value("note", "") is None            # nullable: empty means NULL
    assert t.parse_value("sensor", "") == ""            # NOT NULL text: empty string
    assert t.parse_value("note", "hi") == "hi"
    with pytest.raises(ValueError, match="whole number"):
        t.parse_value("count", "abc")
    with pytest.raises(ValueError):
        t.parse_value("raw", "zz")
    with pytest.raises(TableDataError):
        t.parse_value("missing", "1")
    assert TableData.format_value(b"\x01\x02") == "01 02" and TableData.format_value(None) == ""


def test_apply_writes_all_changes_or_none(project_db):
    """Updates, inserts and deletes are applied together; a constraint error changes nothing.

    Args:
        project_db: Database the workbench did not create.
    """
    t = TableData(project_db, "readings")
    touched = t.apply(RowChanges(
        updates={(1,): {"note": "checked"}},
        inserts=[{"sensor": "t3", "value": 3.0}, {"sensor": "t4"}],
        deletes=[(2,)],
    ))
    assert touched == 4
    assert [values[1] for _, values in t.fetch(10)] == ["t1", "t3", "t4"]
    assert t.fetch(10)[0][1][5] == "checked"
    assert t.fetch(10)[2][1][4] == 0                     # unset column took its DEFAULT

    before = t.fetch(10)
    with pytest.raises(sqlite3.IntegrityError):
        t.apply(RowChanges(updates={(1,): {"note": "lost"}}, inserts=[{"sensor": "t3", "value": 3.0}]))
    assert t.fetch(10) == before                         # whole batch rolled back
    with pytest.raises(TableDataError):
        t.apply(RowChanges(updates={(1,): {"bogus": 1}}))
    assert t.apply(RowChanges()) == 0

    odd = TableData(project_db, 'odd "name"')
    odd.apply(RowChanges(inserts=[{"k": "a", "v": "1"}]))
    odd.apply(RowChanges(updates={("a",): {"v": 2}}))
    assert odd.fetch(10) == [(("a",), ("a", 2))]


def test_opening_a_project_file_leaves_it_unchanged(tmp_path):
    """``wal=False`` keeps the file's journal mode, and no workbench tables are added.

    Args:
        tmp_path: pytest temporary directory.
    """
    path = tmp_path / "theirs.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE t (x)")
    db = Database(path, wal=False)
    try:
        assert db.table_names() == ["t"]
        assert db.query_one("PRAGMA journal_mode")[0].lower() == "delete"
    finally:
        db.close()
