"""Generic, safe access to the rows of any table, for viewing and editing.

Used by the Database Explorer and by table editor pages
(``tools/new_page.py --table <name>``).  It works on any SQLite file,
including databases that were not created by this application.

Safety rules:

* Table and column names are only ever taken from the database itself
  (``sqlite_master`` / ``PRAGMA table_info``) and are quoted, never
  interpolated from user input.  Values always go through ``?`` parameters.
* Rows are identified by ``rowid``, or by the primary key for
  ``WITHOUT ROWID`` tables.  A table with neither cannot be edited.
* :meth:`TableData.apply` writes every pending change in one transaction, so
  a constraint error leaves the table exactly as it was.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..hexutil import format_hex, parse_hex
from .connection import Database
from .migrations import WORKBENCH_TABLES

#: Key that identifies one row: ``(rowid,)`` or the primary key values.
RowKey = tuple[Any, ...]


class TableDataError(ValueError):
    """A table or column does not exist, or the table cannot be edited."""


def quote_identifier(name: str) -> str:
    """Quote a table or column name for use in SQL.

    Args:
        name: Identifier taken from the database schema.

    Returns:
        The name in double quotes, with embedded quotes doubled.
    """
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class ColumnInfo:
    """One column as reported by ``PRAGMA table_info``."""

    name: str
    declared_type: str
    not_null: bool
    default: str | None
    primary_key: int  # 1-based position in the primary key, 0 if not part of it

    @property
    def affinity(self) -> str:
        """SQLite type affinity of the declared type.

        One of ``INTEGER``, ``TEXT``, ``BLOB``, ``REAL`` or ``NUMERIC``,
        following the rules in section 3.1 of the SQLite datatype docs.
        """
        declared = self.declared_type.upper()
        if "INT" in declared:
            return "INTEGER"
        if any(word in declared for word in ("CHAR", "CLOB", "TEXT")):
            return "TEXT"
        if "BLOB" in declared or not declared:
            return "BLOB"
        if any(word in declared for word in ("REAL", "FLOA", "DOUB")):
            return "REAL"
        return "NUMERIC"


@dataclass
class RowChanges:
    """Pending edits to one table, applied together by :meth:`TableData.apply`."""

    updates: dict[RowKey, dict[str, Any]] = field(default_factory=dict)
    inserts: list[dict[str, Any]] = field(default_factory=list)
    deletes: list[RowKey] = field(default_factory=list)

    def __bool__(self) -> bool:
        """Report whether there is anything to apply.

        Returns:
            True if there is at least one update, insert or delete.
        """
        return bool(self.updates or self.inserts or self.deletes)


class TableData:
    """Read and edit the rows of one table of a database."""

    def __init__(self, db: Database, table: str) -> None:
        """Load the table's columns and decide how rows are identified.

        Args:
            db: Database holding the table; any SQLite file works.
            table: Table name; it must exist in ``db``.

        Raises:
            TableDataError: If the table does not exist.
        """
        if table not in db.table_names():
            raise TableDataError(f"table {table!r} does not exist in {db.path}")
        self.db = db
        self.table = table
        self._quoted = quote_identifier(table)
        self.columns = [
            ColumnInfo(row["name"], row["type"] or "", bool(row["notnull"]), row["dflt_value"], row["pk"])
            for row in db.query(f"PRAGMA table_info({self._quoted})")
        ]
        self._by_name = {c.name: c for c in self.columns}
        self.uses_rowid = self._has_rowid()
        if self.uses_rowid:
            self.key_columns = ["rowid"]
        else:
            self.key_columns = [c.name for c in sorted(self.columns, key=lambda c: c.primary_key) if c.primary_key]
        self.editable = bool(self.key_columns)

    def _has_rowid(self) -> bool:
        """Report whether the table has an implicit ``rowid``.

        Returns:
            False for ``WITHOUT ROWID`` tables, True otherwise.
        """
        try:
            self.db.execute(f"SELECT rowid FROM {self._quoted} LIMIT 0")
        except sqlite3.OperationalError:
            return False
        return True

    @property
    def column_names(self) -> list[str]:
        """Column names in table order."""
        return [c.name for c in self.columns]

    def column(self, name: str) -> ColumnInfo:
        """Look up a column by name.

        Args:
            name: Column name.

        Returns:
            The column's schema information.

        Raises:
            TableDataError: If the table has no such column.
        """
        try:
            return self._by_name[name]
        except KeyError:
            raise TableDataError(f"{self.table} has no column {name!r}") from None

    # -- reading ------------------------------------------------------------
    def _filter_clause(self, text: str) -> tuple[str, list[Any]]:
        """Build a WHERE clause matching text in any column.

        Args:
            text: Search text; empty for no filter.

        Returns:
            The clause (empty or starting with `` WHERE``) and its parameters.
        """
        if not text.strip():
            return "", []
        conditions = " OR ".join(f"CAST({quote_identifier(c.name)} AS TEXT) LIKE ?" for c in self.columns)
        return f" WHERE ({conditions})", [f"%{text.strip()}%"] * len(self.columns)

    def count(self, filter_text: str = "") -> int:
        """Count the rows, optionally only those matching a search text.

        Args:
            filter_text: Text that must appear in at least one column.

        Returns:
            The number of matching rows.
        """
        where, params = self._filter_clause(filter_text)
        return int(self.db.scalar(f"SELECT COUNT(*) FROM {self._quoted}{where}", params))

    def fetch(
        self, limit: int, offset: int = 0, filter_text: str = "", newest_first: bool = False
    ) -> list[tuple[RowKey, tuple[Any, ...]]]:
        """Read one page of rows.

        Args:
            limit: Maximum number of rows.
            offset: Number of rows to skip.
            filter_text: Text that must appear in at least one column.
            newest_first: Order by descending key instead of ascending.

        Returns:
            ``(key, values)`` pairs. ``key`` identifies the row for
            :meth:`apply` and ``values`` follows :attr:`column_names`. The
            key is empty for tables that cannot be edited.
        """
        where, params = self._filter_clause(filter_text)
        key_sql = [quote_identifier(k) if k != "rowid" else "rowid" for k in self.key_columns]
        select = ", ".join(key_sql + [quote_identifier(c) for c in self.column_names])
        order = ""
        if key_sql:
            direction = " DESC" if newest_first else ""
            order = " ORDER BY " + ", ".join(k + direction for k in key_sql)
        rows = self.db.query(
            f"SELECT {select} FROM {self._quoted}{where}{order} LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        )
        width = len(key_sql)
        return [(tuple(row[:width]), tuple(row[width:])) for row in rows]

    def schema_sql(self) -> str:
        """Return the table's ``CREATE TABLE`` statement.

        Returns:
            The statement on one line, or ``""`` if it is not recorded.
        """
        row = self.db.query_one("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (self.table,))
        return " ".join((row["sql"] if row and row["sql"] else "").split())

    # -- converting user input ------------------------------------------------
    def parse_value(self, column: str, text: str) -> Any:
        """Convert text typed into a cell to a value for a column.

        Empty text becomes NULL, unless the column is ``NOT NULL`` and holds
        text, in which case it is the empty string. ``BLOB`` columns take
        hex such as ``01 02 FF``.

        Args:
            column: Column name.
            text: Text typed by the user.

        Returns:
            An int, float, bytes, str or None.

        Raises:
            TableDataError: If the column does not exist.
            ValueError: If the text does not fit the column, e.g. ``abc`` in
                an ``INTEGER`` column, or empty text in a ``NOT NULL``
                number column.
        """
        info = self.column(column)
        affinity = info.affinity
        if text == "":
            if not info.not_null:
                return None
            if affinity in ("TEXT", "NUMERIC") or not info.declared_type:
                return ""
            raise ValueError(f"{column} is NOT NULL and needs a value")
        if affinity == "INTEGER":
            for base in (10, 0):  # decimal (leading zeros allowed), then 0x / 0o / 0b prefixes
                try:
                    return int(text.strip(), base)
                except ValueError:
                    pass
            raise ValueError(f"{column} expects a whole number, got {text!r}")
        if affinity == "REAL":
            try:
                return float(text.strip())
            except ValueError:
                raise ValueError(f"{column} expects a number, got {text!r}") from None
        if affinity == "BLOB" and info.declared_type:
            return parse_hex(text)
        if affinity == "NUMERIC":
            for kind in (int, float):
                try:
                    return kind(text.strip())
                except ValueError:
                    pass
        return text

    @staticmethod
    def format_value(value: Any) -> str:
        """Render a stored value as editable text.

        Args:
            value: Value read from the database.

        Returns:
            ``""`` for NULL, hex for bytes, otherwise ``str(value)``.
        """
        if value is None:
            return ""
        if isinstance(value, (bytes, memoryview)):
            return format_hex(bytes(value))
        return str(value)

    # -- writing ---------------------------------------------------------------
    def apply(self, changes: RowChanges) -> int:
        """Apply deletes, then updates, then inserts, in one transaction.

        Args:
            changes: Pending edits. Update and insert keys must be column
                names of this table.

        Returns:
            The number of rows deleted, updated or inserted.

        Raises:
            TableDataError: If the table cannot be edited or a column name
                is unknown.
            sqlite3.Error: If a constraint fails; nothing is changed.
        """
        if not changes:
            return 0
        if not self.editable:
            raise TableDataError(f"{self.table} has no rowid or primary key, so rows cannot be edited")
        for values in [*changes.updates.values(), *changes.inserts]:
            for name in values:
                self.column(name)
        key_where = " AND ".join(
            f"{'rowid' if k == 'rowid' else quote_identifier(k)} = ?" for k in self.key_columns
        )
        touched = 0
        with self.db.transaction():
            for key in changes.deletes:
                touched += self.db.execute(f"DELETE FROM {self._quoted} WHERE {key_where}", key).rowcount
            for key, values in changes.updates.items():
                if values:
                    assignments = ", ".join(f"{quote_identifier(n)} = ?" for n in values)
                    touched += self.db.execute(
                        f"UPDATE {self._quoted} SET {assignments} WHERE {key_where}", [*values.values(), *key]
                    ).rowcount
            for values in changes.inserts:
                if values:
                    names = ", ".join(quote_identifier(n) for n in values)
                    marks = ", ".join("?" for _ in values)
                    self.db.execute(f"INSERT INTO {self._quoted} ({names}) VALUES ({marks})", list(values.values()))
                else:
                    self.db.execute(f"INSERT INTO {self._quoted} DEFAULT VALUES")
                touched += 1
        return touched


def user_table_names(db: Database, include_workbench: bool = False) -> list[str]:
    """List a database's tables, normally without the workbench's own tables.

    Args:
        db: Database to inspect.
        include_workbench: Also list the tables this application creates.

    Returns:
        Table names, sorted.
    """
    names = db.table_names()
    return names if include_workbench else [n for n in names if n not in WORKBENCH_TABLES]
