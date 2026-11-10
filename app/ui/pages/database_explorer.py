from __future__ import annotations

import csv
import sqlite3
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
from ...core.hexutil import format_hex
from ..styles import theme
from ..widgets.common import PageHeader, configure_table, section_label, show_error


class SqlRowsModel(QAbstractTableModel):
    """Read-only grid over query results; BLOBs shown as a HEX preview."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.headers: list[str] = []
        self.rows: list[tuple[Any, ...]] = []

    def set_rows(self, headers: list[str], rows: list[tuple[Any, ...]]) -> None:
        self.beginResetModel()
        self.headers, self.rows = headers, rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
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
    PAGE_SIZE = 500

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._offset = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        header = PageHeader("Database Explorer", f"Read-only view of {ctx.db.path}")
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.reload_tables)
        backup = QPushButton("Backup now")
        backup.setObjectName("Primary")
        backup.clicked.connect(self.backup)
        header.actions.addWidget(refresh)
        header.actions.addWidget(backup)
        layout.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(section_label("Tables"))
        self.tables = QListWidget()
        lv.addWidget(self.tables, 1)
        self.schema = QLabel("")
        self.schema.setObjectName("Dim")
        self.schema.setWordWrap(True)
        self.schema.setFont(theme.monospace_font(8))
        self.schema.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lv.addWidget(self.schema)
        splitter.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.model = SqlRowsModel(self)
        self.view = QTableView()
        self.view.setModel(self.model)
        configure_table(self.view)
        self.view.setFont(theme.monospace_font(9))
        rv.addWidget(self.view, 1)
        pager = QHBoxLayout()
        self.prev_button = QPushButton("◀ Previous")
        self.next_button = QPushButton("Next ▶")
        self.prev_button.clicked.connect(lambda: self._page(-1))
        self.next_button.clicked.connect(lambda: self._page(1))
        self.page_label = QLabel("")
        self.page_label.setObjectName("Dim")
        pager.addWidget(self.prev_button)
        pager.addWidget(self.next_button)
        pager.addWidget(self.page_label, 1)
        rv.addLayout(pager)
        splitter.addWidget(right)
        splitter.setSizes([240, 900])
        tabs = QTabWidget()
        tabs.addTab(splitter, "Tables")
        self.queries = SavedQueriesPanel(ctx)
        tabs.addTab(self.queries, "Saved queries")
        layout.addWidget(tabs, 1)

        self.tables.currentTextChanged.connect(self._select_table)
        self.reload_tables()

    def reload_tables(self) -> None:
        current = self.tables.currentItem().text() if self.tables.currentItem() else None
        self.tables.blockSignals(True)
        self.tables.clear()
        names = self.ctx.db.table_names()
        for name in names:
            count = self.ctx.db.query_one(f'SELECT COUNT(*) AS n FROM "{name}"')["n"]
            self.tables.addItem(name)
            self.tables.item(self.tables.count() - 1).setToolTip(f"{count} rows")
        self.tables.blockSignals(False)
        if current in names:
            self.tables.setCurrentRow(names.index(current))
        elif names:
            self.tables.setCurrentRow(0)
        self._load()

    def _select_table(self, _name: str) -> None:
        self._offset = 0
        self._load()

    def _page(self, step: int) -> None:
        self._offset = max(0, self._offset + step * self.PAGE_SIZE)
        self._load()

    def _load(self) -> None:
        item = self.tables.currentItem()
        if item is None:
            self.model.set_rows([], [])
            return
        name = item.text()
        if name not in self.ctx.db.table_names():  # never interpolate unknown names
            return
        total = self.ctx.db.query_one(f'SELECT COUNT(*) AS n FROM "{name}"')["n"]
        cursor = self.ctx.db.execute(f'SELECT * FROM "{name}" ORDER BY rowid DESC LIMIT ? OFFSET ?',
                                     (self.PAGE_SIZE, self._offset))
        headers = [d[0] for d in cursor.description]
        self.model.set_rows(headers, [tuple(r) for r in cursor.fetchall()])
        self.view.resizeColumnsToContents()
        end = min(total, self._offset + self.PAGE_SIZE)
        self.page_label.setText(f"{name}: rows {self._offset + 1 if total else 0}-{end} of {total} (newest first)")
        self.prev_button.setEnabled(self._offset > 0)
        self.next_button.setEnabled(end < total)
        sql = self.ctx.db.query_one("SELECT sql FROM sqlite_master WHERE name = ?", (name,))
        self.schema.setText(" ".join((sql["sql"] if sql else "").split()))

    def backup(self) -> None:
        try:
            path = self.ctx.backup_database()
        except Exception as exc:
            show_error(self, "Backup", exc)
            return
        QMessageBox.information(self, "Backup", f"Backup written to\n{path}")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.reload_tables()


def _coerce(text: str) -> Any:
    """Parameter text -> int / float / None / str."""
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
        run.clicked.connect(self.run)
        export = QPushButton("Export CSV…")
        export.clicked.connect(self.export)
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
        self.editor.textChanged.connect(self._rebuild_params)
        self._param_edits: dict[str, QLineEdit] = {}
        self.reload_list()

    def reload_list(self) -> None:
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
        item = self.list.currentItem()
        if item is None:
            return
        q = self.ctx.db.sql.get(item.text())
        self.description.setText(f"<b>{q.name}</b> — {q.description or 'no description'}"
                                 f"<br><span style='color:{theme.TEXT_DIM}'>{q.source}</span>")
        self.editor.setPlainText(q.sql)
        args = ", ".join(f"{p}=..." for p in q.parameters)
        self.python_call.setText(f'ctx.db.run("{q.name}"{", " + args if args else ""})')

    def _rebuild_params(self) -> None:
        from ...core.database.sql_library import NamedQuery

        names = NamedQuery("adhoc", self.editor.toPlainText()).parameters
        old = {k: e.text() for k, e in self._param_edits.items()}
        while self.params.rowCount():
            self.params.removeRow(0)
        self._param_edits = {}
        for name in names:
            edit = QLineEdit(old.get(name, ""))
            edit.setPlaceholderText("number or text; empty = NULL")
            edit.returnPressed.connect(self.run)
            self.params.addRow(f":{name}", edit)
            self._param_edits[name] = edit

    def run(self) -> None:
        sql = self.editor.toPlainText().strip().rstrip(";")
        if not sql:
            return
        params = {name: _coerce(edit.text()) for name, edit in self._param_edits.items()}
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

    def export(self) -> None:
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
        super().showEvent(event)
        self.reload_list()
