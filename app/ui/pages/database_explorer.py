"""Database Explorer: browse tables read-only and run saved (named) queries."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...context import AppContext
from ...core.database import Database
from ...core.hexutil import format_hex
from ..styles import theme
from ..widgets.common import PageHeader, configure_table, section_label, show_error
from ..widgets.table_editor import TableEditor, open_database_file


class SqlRowsModel(QAbstractTableModel):
    """Read-only grid over query results; BLOBs shown as a HEX preview."""

    def __init__(self, parent=None) -> None:
        """Create an empty result model.

        Args:
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self.headers: list[str] = []
        self.rows: list[tuple[Any, ...]] = []

    def set_rows(self, headers: list[str], rows: list[tuple[Any, ...]]) -> None:
        """Replace the column headers and rows and reset attached views.

        Args:
            headers: Column names.
            rows: Result rows as tuples.
        """
        self.beginResetModel()
        self.headers, self.rows = headers, rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        """Return the number of result rows.

        Args:
            parent: Parent index; only the invalid root index has rows.

        Returns:
            The row count.
        """
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        """Return the number of result columns.

        Args:
            parent: Parent index; only the invalid root index has columns.

        Returns:
            The column count.
        """
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        """Return the horizontal header text.

        Args:
            section: Column index.
            orientation: Header orientation.
            role: Qt item data role.

        Returns:
            The column name for the display role, otherwise None.
        """
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        """Return cell data for a role.

        BLOBs are shown as their size plus a hex preview, and NULLs as a dimmed
        ``NULL``.

        Args:
            index: Cell index.
            role: Qt item data role.

        Returns:
            The display text or foreground colour, otherwise None.
        """
        if not index.isValid():
            return None
        value = self.rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            if isinstance(value, (bytes, memoryview)):
                raw = bytes(value)
                return f"<{len(raw)} B> {format_hex(raw[:32])}{' …' if len(raw) > 32 else ''}"
            return "NULL" if value is None else str(value)
        if role == Qt.ItemDataRole.ForegroundRole and value is None:
            return QColor(theme.TEXT_DIM)
        return None


class DatabaseExplorerPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        """Build the table browser and the saved queries tab.

        The browser starts on the workbench database and hides the
        workbench's own tables, so a project database shows only its tables.

        Args:
            ctx: Application context.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.ctx = ctx
        self._external: Database | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        header = PageHeader("Database Explorer", "Read-only view. Make an editable page with "
                                                 "python tools/new_page.py <key> \"<Title>\" --table <table>.")
        for text, slot, name in (("Open file…", self.open_file, ""), ("Workbench database", self.use_workbench_database, ""),
                                 ("Refresh", self.reload_tables, ""), ("Backup now", self.backup_database, "Primary")):
            button = QPushButton(text)
            button.setObjectName(name)
            button.clicked.connect(lambda _c=False, f=slot: f())
            header.actions.addWidget(button)
        layout.addWidget(header)
        self.source_label = QLabel("")
        self.source_label.setObjectName("Dim")
        self.source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.source_label)

        self.tables_tab = QWidget()
        self._tables_layout = QVBoxLayout(self.tables_tab)
        self._tables_layout.setContentsMargins(0, 6, 0, 0)
        self.browser: TableEditor | None = None
        tabs = QTabWidget()
        tabs.addTab(self.tables_tab, "Tables")
        self.queries = SavedQueriesPanel(ctx)
        tabs.addTab(self.queries, "Saved queries")
        layout.addWidget(tabs, 1)

        workflow = getattr(ctx, "workflow", None)
        if workflow is not None:
            workflow.database_changed.connect(lambda _p: self._external is None and self._show_database(ctx.db))
        self._show_database(ctx.db)

    @property
    def current_database(self) -> Database:
        """Database being browsed: an opened file, or the workbench database."""
        return self._external or self.ctx.db

    def _show_database(self, db: Database) -> None:
        """Replace the table browser with one for another database.

        Args:
            db: Database to browse.
        """
        if self.browser is not None:
            self._tables_layout.removeWidget(self.browser)
            self.browser.deleteLater()
        self.browser = TableEditor(db, read_only=True)
        self._tables_layout.addWidget(self.browser)
        kind = "opened file (not modified)" if db is self._external else "workbench database"
        self.source_label.setText(f"Browsing {db.path}  ·  {kind}")

    def open_file(self, path: str | None = None) -> None:
        """Browse another SQLite file without adding the workbench tables to it.

        Args:
            path: File to open; asks with a file dialog when None.
        """
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Open database", str(self.ctx.db.path.parent),
                                                  "SQLite database (*.db *.sqlite *.sqlite3);;All files (*)")
            if not path:
                return
        if Path(path).resolve() == self.ctx.db.path.resolve():
            self.use_workbench_database()
            return
        try:
            db = open_database_file(path)
        except (OSError, sqlite3.DatabaseError) as exc:
            show_error(self, "Open database", exc)
            return
        self._close_external()
        self._external = db
        self._show_database(db)

    def use_workbench_database(self) -> None:
        """Go back to browsing the workbench database."""
        self._close_external()
        self._show_database(self.ctx.db)

    def _close_external(self) -> None:
        """Close the opened file, if any."""
        if self._external is not None:
            self._external.close()
            self._external = None

    def reload_tables(self) -> None:
        """Reload the table list and the current page."""
        if self.browser is not None:
            self.browser.reload_table_list()

    def backup_database(self) -> None:
        """Back up the workbench database and report where it was written."""
        try:
            path = self.ctx.backup_database()
        except Exception as exc:
            show_error(self, "Backup", exc)
            return
        QMessageBox.information(self, "Backup", f"Backup written to\n{path}")

    def showEvent(self, event) -> None:  # noqa: N802
        """Reload the table list whenever the page becomes visible.

        Args:
            event: Qt show event.
        """
        super().showEvent(event)
        self.reload_tables()


def _coerce_param_text(text: str) -> Any:
    """Convert parameter text to an SQL value.

    Args:
        text: Text typed by the user.

    Returns:
        An int or float if the text parses as one, None for empty text,
        otherwise the original text.
    """
    value = text.strip()
    if value == "":
        return None
    for kind in (int, float):
        try:
            return kind(value)
        except ValueError:
            pass
    return text


class SavedQueriesPanel(QWidget):
    """Runs named queries from the .sql files (or edited SQL) read-only."""

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        """Build the query list, SQL editor, parameter inputs and result grid.

        Args:
            ctx: Application context.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.ctx = ctx
        self._headers: list[str] = []
        self._rows: list[tuple[Any, ...]] = []
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(section_label("Named queries"))
        self.list = QListWidget()
        lv.addWidget(self.list, 1)
        reload_button = QPushButton("Reload .sql files")
        reload_button.clicked.connect(self.reload_library)
        lv.addWidget(reload_button)
        self.dirs = QLabel("")
        self.dirs.setObjectName("Dim")
        self.dirs.setWordWrap(True)
        lv.addWidget(self.dirs)
        splitter.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.description = QLabel("Select a query. Queries come from app/core/database/sql/ and the "
                                  "folders in project.json -> sql_dirs (default sql/).")
        self.description.setWordWrap(True)
        rv.addWidget(self.description)
        self.editor = QPlainTextEdit()
        self.editor.setFont(theme.monospace_font(10))
        self.editor.setPlaceholderText("SELECT ...  (you can also type ad-hoc SQL here)")
        self.editor.setFixedHeight(130)
        rv.addWidget(self.editor)
        self.params_box = QWidget()
        self.params = QFormLayout(self.params_box)
        self.params.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(self.params_box)
        row = QHBoxLayout()
        run = QPushButton("Run (read-only)")
        run.setObjectName("Primary")
        run.clicked.connect(self.run_query)
        export = QPushButton("Export CSV…")
        export.clicked.connect(self.export_results_csv)
        self.python_call = QLineEdit()
        self.python_call.setReadOnly(True)
        self.python_call.setFont(theme.monospace_font(9))
        self.python_call.setToolTip("How to call this query from Python")
        row.addWidget(run)
        row.addWidget(export)
        row.addWidget(self.python_call, 1)
        rv.addLayout(row)
        self.model = SqlRowsModel(self)
        view = QTableView()
        view.setModel(self.model)
        configure_table(view)
        view.setFont(theme.monospace_font(9))
        rv.addWidget(view, 1)
        self.status = QLabel("")
        self.status.setObjectName("Dim")
        rv.addWidget(self.status)
        splitter.addWidget(right)
        splitter.setSizes([260, 880])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.addWidget(splitter)

        self.list.currentItemChanged.connect(lambda *_: self.show_query())
        self.editor.textChanged.connect(self._rebuild_param_inputs)
        self._param_edits: dict[str, QLineEdit] = {}
        self.reload_list()

    def reload_list(self) -> None:
        """Refill the query list from the SQL library, keeping the selection.

        Queries that modify data are dimmed.
        """
        library = self.ctx.db.sql
        current = self.list.currentItem().text() if self.list.currentItem() else None
        self.list.clear()
        for name in library.names():
            q = library.get(name)
            item = QListWidgetItem(name)
            item.setToolTip(f"{q.description}\n{q.source}" + ("" if q.read_only else "\n(modifies data: not run here)"))
            if not q.read_only:
                item.setForeground(QColor(theme.TEXT_DIM))
            self.list.addItem(item)
        self.dirs.setText("Folders: " + ", ".join(str(d) for d in library.directories))
        if current:
            matches = self.list.findItems(current, Qt.MatchFlag.MatchExactly)
            if matches:
                self.list.setCurrentItem(matches[0])

    def reload_library(self) -> None:
        """Reload every ``.sql`` folder from disk and refresh the list.

        Errors are shown in a dialog and the old library is kept.
        """
        from ...core.database.sql_library import BUILTIN_DIR, SqlLibrary, SqlLibraryError

        extra = [d for d in self.ctx.db.sql.directories if d != BUILTIN_DIR]
        try:
            self.ctx.db.sql = SqlLibrary.load(extra)
        except (SqlLibraryError, OSError) as exc:
            show_error(self, "Reload SQL", exc)
            return
        self.reload_list()
        self.status.setText(f"Loaded {len(self.ctx.db.sql.queries)} queries")

    def show_query(self) -> None:
        """Show the selected query's description, SQL and Python call."""
        item = self.list.currentItem()
        if item is None:
            return
        q = self.ctx.db.sql.get(item.text())
        self.description.setText(f"<b>{q.name}</b> — {q.description or 'no description'}"
                                 f"<br><span style='color:{theme.TEXT_DIM}'>{q.source}</span>")
        self.editor.setPlainText(q.sql)
        args = ", ".join(f"{p}=..." for p in q.parameters)
        self.python_call.setText(f'ctx.db.run("{q.name}"{", " + args if args else ""})')

    def _rebuild_param_inputs(self) -> None:
        """Recreate one input per ``:param`` in the editor, keeping typed values."""
        from ...core.database.sql_library import NamedQuery

        names = NamedQuery("adhoc", self.editor.toPlainText()).parameters
        old = {k: e.text() for k, e in self._param_edits.items()}
        while self.params.rowCount():
            self.params.removeRow(0)
        self._param_edits = {}
        for name in names:
            edit = QLineEdit(old.get(name, ""))
            edit.setPlaceholderText("number or text; empty = NULL")
            edit.returnPressed.connect(self.run_query)
            self.params.addRow(f":{name}", edit)
            self._param_edits[name] = edit

    def run_query(self) -> None:
        """Run the editor's SQL read-only with the entered parameters.

        Pending captured packets are written first so they are visible.
        Errors are shown in a dialog.
        """
        sql = self.editor.toPlainText().strip().rstrip(";")
        if not sql:
            return
        params = {name: _coerce_param_text(edit.text()) for name, edit in self._param_edits.items()}
        self.ctx.capture.flush()
        try:
            headers, rows = self.ctx.db.run_read_only(sql, params)
        except sqlite3.Error as exc:
            message = str(exc)
            if "readonly" in message.lower():
                message += "\n\nThe explorer only reads. Run modifying queries from code with db.run_write()."
            show_error(self, "Query", message)
            return
        self._headers, self._rows = headers, [tuple(r) for r in rows]
        self.model.set_rows(headers, self._rows)
        self.status.setText(f"{len(rows)} row(s)" + (" (first 5000 shown)" if len(rows) >= 5000 else ""))

    def export_results_csv(self) -> None:
        """Save the last query result as CSV, with BLOBs written as hex."""
        if not self._headers:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export results", "query.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(self._headers)
            for row in self._rows:
                writer.writerow([format_hex(bytes(v), "") if isinstance(v, (bytes, memoryview)) else v for v in row])
        self.status.setText(f"Exported {len(self._rows)} row(s) to {path}")

    def showEvent(self, event) -> None:  # noqa: N802
        """Reload the query list whenever the panel becomes visible.

        Args:
            event: Qt show event.
        """
        super().showEvent(event)
        self.reload_list()
