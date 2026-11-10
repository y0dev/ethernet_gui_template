"""Named SQL queries kept in ``.sql`` files.

Write queries once, in plain SQL, and call them by name from anywhere::

    -- name: devices.by_host
    -- Devices registered for one IP address.
    SELECT * FROM devices WHERE host = :host ORDER BY name;

    rows = db.run("devices.by_host", host="10.0.0.5")

File format
-----------
* ``-- name: <name>`` starts a query; everything until the next ``-- name:``
  belongs to it.  Names may contain dots to group them (``reports.traffic``).
* ``--`` comment lines directly after the name line are its description
  (shown in the Database Explorer).
* Parameters use SQLite's named style: ``:host``, ``:session_id``.
* One statement per query (SQLite executes a single statement per call).

Search order
------------
The built-in queries (``app/core/database/sql/``) load first, then every
directory listed in ``project.json`` → ``sql_dirs`` (default: ``sql/`` at the
project root).  A later file may redefine a name; the override is logged so
it is never silent.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).with_name("sql")
_NAME = re.compile(r"^--\s*name:\s*([A-Za-z_][\w.\-]*)\s*$")
_PARAM = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")
_READ_ONLY_START = ("select", "with", "pragma", "explain", "values")


class SqlLibraryError(ValueError):
    """A query file is malformed or a query name is unknown."""


@dataclass(frozen=True)
class NamedQuery:
    name: str
    sql: str
    description: str = ""
    source: str = ""                     # "file.sql:line"

    @property
    def parameters(self) -> list[str]:
        """Named ``:param`` placeholders, in order of first appearance.

        Placeholders inside string literals and comments are ignored.
        """
        seen: list[str] = []
        for match in _PARAM.finditer(_strip_literals(self.sql)):
            if match.group(1) not in seen:
                seen.append(match.group(1))
        return seen

    @property
    def read_only(self) -> bool:
        """Best-effort hint that the query only reads data.

        The UI uses this hint. The Explorer also enforces read-only access with
        a read-only connection.
        """
        first = _strip_comments(self.sql).lstrip().split(None, 1)
        return bool(first) and first[0].lower() in _READ_ONLY_START


def _strip_comments(sql: str) -> str:
    """Remove ``--`` comment lines from SQL text.

    Args:
        sql: SQL text.

    Returns:
        The SQL without lines that start with ``--``.
    """
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


def _strip_literals(sql: str) -> str:
    """Remove comments and string literals so ``:x`` inside them is not a parameter.

    Args:
        sql: SQL text.

    Returns:
        The SQL with comment lines dropped and every literal replaced by ``''``.
    """
    sql = _strip_comments(sql)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def parse_sql_file(text: str, source: str = "") -> list[NamedQuery]:
    """Parse the named queries from the text of one ``.sql`` file.

    Args:
        text: File contents in the ``-- name:`` format.
        source: File name, used in error messages and ``NamedQuery.source``.

    Returns:
        The queries in file order.

    Raises:
        SqlLibraryError: If SQL appears before the first name line, a query is
            empty or incomplete, or a name is duplicated.
    """
    queries: list[NamedQuery] = []
    name: str | None = None
    start_line = 0
    description: list[str] = []
    body: list[str] = []
    in_description = False

    def finish_current_query() -> None:
        """Store the query collected so far, if any, after validating it.

        Raises:
            SqlLibraryError: If the query is empty or not a complete statement.
        """
        if name is None:
            return
        sql = "\n".join(body).strip()
        if not sql:
            raise SqlLibraryError(f"{source}:{start_line}: query '{name}' has no SQL")
        if not sqlite3.complete_statement(sql if sql.endswith(";") else sql + ";"):
            raise SqlLibraryError(f"{source}:{start_line}: query '{name}' is not a complete statement")
        queries.append(NamedQuery(name, sql.rstrip(";").rstrip(), " ".join(description).strip(),
                                  f"{source}:{start_line}"))

    for number, line in enumerate(text.splitlines(), start=1):
        match = _NAME.match(line.strip())
        if match:
            finish_current_query()
            name, start_line, description, body, in_description = match.group(1), number, [], [], True
            continue
        if name is None:
            if line.strip() and not line.strip().startswith("--"):
                raise SqlLibraryError(f"{source}:{number}: SQL before the first '-- name:' line")
            continue
        if in_description and line.strip().startswith("--"):
            description.append(line.strip()[2:].strip())
            continue
        in_description = False
        body.append(line)
    finish_current_query()
    names = [q.name for q in queries]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise SqlLibraryError(f"{source}: duplicate query names {sorted(duplicates)}")
    return queries


@dataclass
class SqlLibrary:
    queries: dict[str, NamedQuery] = field(default_factory=dict)
    directories: list[Path] = field(default_factory=list)

    @classmethod
    def load(cls, directories: Iterable[str | Path] = ()) -> SqlLibrary:
        """Load the built-in queries, then each extra folder in order.

        Queries in later folders override earlier ones with the same name.

        Args:
            directories: Extra folders of ``.sql`` files.

        Returns:
            The loaded library.
        """
        library = cls()
        for directory in [BUILTIN_DIR, *[Path(d) for d in directories]]:
            library.add_directory(directory)
        return library

    def add_directory(self, directory: Path) -> None:
        """Add every ``.sql`` file in a folder, overriding queries with the same name.

        A missing folder is skipped and logged. Overrides are logged as warnings.

        Args:
            directory: Folder to load.

        Raises:
            SqlLibraryError: If a file is malformed.
        """
        directory = Path(directory)
        if not directory.is_dir():
            if directory != BUILTIN_DIR:
                log.info("sql directory not found (skipped)", extra={"context": {"path": str(directory)}})
            return
        self.directories.append(directory)
        for path in sorted(directory.glob("*.sql")):
            for query in parse_sql_file(path.read_text(encoding="utf-8"), path.name):
                previous = self.queries.get(query.name)
                if previous is not None:
                    log.warning(
                        "sql query overridden",
                        extra={"context": {"name": query.name, "was": previous.source, "now": query.source}},
                    )
                self.queries[query.name] = query

    def get(self, name: str) -> NamedQuery:
        """Look up a query by name.

        Args:
            name: Query name.

        Returns:
            The named query.

        Raises:
            SqlLibraryError: If no query has that name.
        """
        try:
            return self.queries[name]
        except KeyError:
            raise SqlLibraryError(f"unknown SQL query '{name}' (looked in {[str(d) for d in self.directories]})") from None

    def names(self, prefix: str = "") -> list[str]:
        """List query names, optionally filtered by prefix.

        Args:
            prefix: Only names starting with this text are returned.

        Returns:
            The matching names, sorted.
        """
        return sorted(n for n in self.queries if n.startswith(prefix))

    def __contains__(self, name: str) -> bool:
        """Report whether a query with this name is loaded.

        Args:
            name: Query name.

        Returns:
            True if the library contains the query.
        """
        return name in self.queries
