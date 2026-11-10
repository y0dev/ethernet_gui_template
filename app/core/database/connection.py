"""SQLite connection management.

One :class:`Database` object is shared by the whole application.  SQLite
connections are not shared between threads: each thread that touches the
database lazily gets its own connection, all pointing at the same file in WAL
mode (many readers, one writer, readers never block the writer).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .sql_library import SqlLibrary

log = logging.getLogger(__name__)


class Database:
    """Thread-safe access to one SQLite database file."""

    def __init__(
        self, path: str | Path, busy_timeout_ms: int = 5000, sql: SqlLibrary | None = None, wal: bool = True
    ) -> None:
        """Prepare access to a database file; connections are opened lazily.

        Args:
            path: SQLite file path, or ``":memory:"``. Parent folders are created.
            busy_timeout_ms: How long a connection waits for a lock.
            sql: Named-query library; defaults to the built-in queries.
            wal: Switch the file to WAL journal mode (stored in the file). Pass
                False to leave someone else's database file as it is.
        """
        self.path = Path(path)
        #: named queries (see sql_library.py); built-ins unless the app passes more
        self.sql = sql if sql is not None else SqlLibrary.load()
        self._busy_timeout_ms = busy_timeout_ms
        self._wal = wal
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._closed = False
        self._generation = 0  # bumped by reopen(); stale thread connections are replaced
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- connections ------------------------------------------------------
    def connection(self) -> sqlite3.Connection:
        """Return the calling thread's connection, creating it if needed.

        New connections use autocommit mode and enforced foreign keys, plus
        WAL journaling unless the database was created with ``wal=False``.

        Returns:
            The thread's connection.

        Raises:
            RuntimeError: If the database has been closed.
        """
        if self._closed:
            raise RuntimeError("database is closed")
        conn = getattr(self._local, "conn", None)
        if conn is not None and getattr(self._local, "generation", -1) != self._generation:
            conn = None  # opened before reopen(); already closed there
        if conn is None:
            # isolation_level=None: no implicit transactions; see transaction().
            # check_same_thread=False only so close() can run from any thread.
            conn = sqlite3.connect(
                str(self.path), isolation_level=None, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_ms)}")
            conn.execute("PRAGMA foreign_keys = ON")
            if self._wal:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
            self._local.conn = conn
            self._local.depth = 0
            self._local.generation = self._generation
            with self._lock:
                self._connections.append(conn)
        return conn

    def close(self) -> None:
        """Close every connection opened through this object."""
        with self._lock:
            self._closed = True
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error:
                    log.exception("error closing database connection")
            self._connections.clear()
        self._local = threading.local()

    def reopen(self, path: str | Path) -> None:
        """Switch this object, and every repository using it, to another file.

        All existing connections are closed. The caller must run migrations on
        the new file and make sure no transaction or recording is in progress.

        Args:
            path: New database file; parent folders are created.
        """
        new_path = Path(path)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._connections.clear()
            self.path = new_path
            self._generation += 1
            self._closed = False
        self._local = threading.local()

    @property
    def closed(self) -> bool:
        """True after :meth:`close` and before any :meth:`reopen`."""
        return self._closed

    # -- statements -------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        """Execute one SQL statement on the thread's connection.

        Args:
            sql: Statement with ``?`` or ``:name`` placeholders.
            params: Placeholder values.

        Returns:
            The cursor.
        """
        return self.connection().execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        """Execute one SQL statement for each parameter row.

        Args:
            sql: Statement with placeholders.
            rows: One parameter sequence per execution.

        Returns:
            The cursor.
        """
        return self.connection().executemany(sql, rows)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        """Run a query and fetch every row.

        Args:
            sql: Query with placeholders.
            params: Placeholder values.

        Returns:
            All result rows.
        """
        return self.connection().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        """Run a query and fetch the first row.

        Args:
            sql: Query with placeholders.
            params: Placeholder values.

        Returns:
            The first row, or None if there are no rows.
        """
        return self.connection().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        """Return the first column of the first row, for COUNT/MAX style queries.

        Args:
            sql: Query with placeholders.
            params: Placeholder values.

        Returns:
            The scalar value.

        Raises:
            LookupError: If the query returns no rows.
        """
        row = self.connection().execute(sql, params).fetchone()
        if row is None:
            raise LookupError(f"query returned no rows: {sql}")
        return row[0]

    # -- named queries (.sql files) ----------------------------------------
    def run(self, name: str, **params: Any) -> list[sqlite3.Row]:
        """Run a named query from the SQL library and fetch every row.

        Args:
            name: Query name, e.g. ``"schema.tables"``.
            **params: Named placeholder values.

        Returns:
            All result rows.

        Raises:
            SqlLibraryError: If no query has that name.
        """
        return self.connection().execute(self.sql.get(name).sql, params).fetchall()

    def run_one(self, name: str, **params: Any) -> sqlite3.Row | None:
        """Run a named query and fetch the first row.

        Args:
            name: Query name.
            **params: Named placeholder values.

        Returns:
            The first row, or None if there are no rows.

        Raises:
            SqlLibraryError: If no query has that name.
        """
        return self.connection().execute(self.sql.get(name).sql, params).fetchone()

    def run_scalar(self, name: str, **params: Any) -> Any:
        """Run a named query and return the first column of its first row.

        Args:
            name: Query name.
            **params: Named placeholder values.

        Returns:
            The scalar value.

        Raises:
            SqlLibraryError: If no query has that name.
            LookupError: If the query returns no rows.
        """
        row = self.run_one(name, **params)
        if row is None:
            raise LookupError(f"query '{name}' returned no rows")
        return row[0]

    def run_write(self, name: str, **params: Any) -> int:
        """Execute a modifying named query.

        Args:
            name: Query name.
            **params: Named placeholder values.

        Returns:
            The number of affected rows.

        Raises:
            SqlLibraryError: If no query has that name.
        """
        return self.connection().execute(self.sql.get(name).sql, params).rowcount

    def run_read_only(self, sql: str, params: dict[str, Any] | Sequence[Any] = (), limit: int = 5000
                      ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Run SQL on a separate read-only connection.

        The Database Explorer uses this so ad-hoc queries can never modify data.

        Args:
            sql: Query text.
            params: Placeholder values.
            limit: Maximum number of rows fetched.

        Returns:
            The column headers and up to ``limit`` rows. Both are empty for
            statements that return no result set.

        Raises:
            sqlite3.Error: If the SQL is invalid or attempts a write.
        """
        uri = f"file:{self.path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            cursor = conn.execute(sql, params)
            headers = [d[0] for d in cursor.description] if cursor.description else []
            return headers, cursor.fetchmany(limit) if headers else []
        finally:
            conn.close()

    @staticmethod
    def last_id(cursor: sqlite3.Cursor) -> int:
        """Return the row id inserted by a cursor's last statement.

        Args:
            cursor: Cursor that executed an INSERT.

        Returns:
            The new row id.

        Raises:
            RuntimeError: If the statement did not insert a row.
        """
        if cursor.lastrowid is None:
            raise RuntimeError("statement did not insert a row")
        return int(cursor.lastrowid)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block atomically; nested blocks become savepoints.

        The outermost block uses ``BEGIN IMMEDIATE`` so a writer conflict is
        reported at the start instead of in the middle of the block. The block
        rolls back if it raises.

        Yields:
            The thread's connection.
        """
        conn = self.connection()
        depth = self._local.depth
        name = f"sp_{depth}"
        conn.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {name}")
        self._local.depth = depth + 1
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK" if depth == 0 else f"ROLLBACK TO {name}")
            if depth:
                conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute("COMMIT" if depth == 0 else f"RELEASE {name}")
        finally:
            self._local.depth = depth

    # -- maintenance ------------------------------------------------------
    def backup(self, destination: str | Path) -> Path:
        """Write a consistent copy of the live database.

        Args:
            destination: Backup file path; parent folders are created.

        Returns:
            The destination path.

        Raises:
            ValueError: If ``destination`` is the live database file.
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.resolve() == self.path.resolve():
            raise ValueError("backup destination must differ from the live database")
        target = sqlite3.connect(str(destination))
        try:
            self.connection().backup(target)
        finally:
            target.close()
        log.info("database backup written", extra={"context": {"path": str(destination)}})
        return destination

    def table_names(self) -> list[str]:
        """List the tables in the database.

        Returns:
            Table names, as returned by the ``schema.tables`` query.
        """
        return [row["name"] for row in self.run("schema.tables")]
