"""Spreadsheet-style viewer and editor for one table of any SQLite database.

Drop :class:`TableEditor` into a page to view and edit a table::

    TableEditor(ctx.db, "calibration")              # one fixed table
    TableEditor(ctx.db)                             # table chooser, your tables only
    TableEditor(Database("data/plant.db"), read_only=True)

Edits are collected first and written together with Save (one transaction),
so a constraint error leaves the table unchanged.  Changed cells, new rows and
rows marked for deletion are coloured until they are saved or reverted.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...core.database import Database, RowChanges, TableData, TableDataError, user_table_names
from ...core.hexutil import format_hex
from ..styles import theme
from .common import configure_table

Index = QModelIndex | QPersistentModelIndex


@dataclass
class EditableRow:
    """One row of the editor with its pending changes."""

    key: tuple[Any, ...] | None          # None for a row that is not saved yet
    values: list[Any]
    edits: dict[int, Any] = field(default_factory=dict)  # column index -> new value
    deleted: bool = False

    @property
    def is_new(self) -> bool:
        """True for a row added in the editor and not saved yet."""
        return self.key is None


def _tinted(color: str, alpha: int = 70) -> QColor:
    """Make a translucent version of a theme colour for cell backgrounds.

    Args:
        color: Theme colour, e.g. ``theme.WARN``.
        alpha: Opacity from 0 to 255.

    Returns:
        The translucent colour.
    """
    tinted = QColor(color)
    tinted.setAlpha(alpha)
    return tinted


class EditableTableModel(QAbstractTableModel):
    """Qt model over one page of a :class:`TableData`, with pending edits."""

    #: a value could not be converted; the message explains why
    error_occurred = Signal(str)
    #: pending changes were added or discarded
    changes_changed = Signal()

    def __init__(self, read_only: bool = False, parent=None) -> None:
        """Create an empty model.

        Args:
            read_only: Refuse every edit.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self.read_only = read_only
        self.table: TableData | None = None
        self.rows: list[EditableRow] = []

    # -- loading and pending changes -----------------------------------------
    def set_page(self, table: TableData | None, rows: list[tuple[tuple[Any, ...], tuple[Any, ...]]]) -> None:
        """Show a page of rows, discarding pending changes.

        Args:
            table: Table the rows belong to, or None for an empty model.
            rows: ``(key, values)`` pairs from :meth:`TableData.fetch`.
        """
        self.beginResetModel()
        self.table = table
        self.rows = [EditableRow(key if table and table.editable else (), list(values)) for key, values in rows]
        self.endResetModel()
        self.changes_changed.emit()

    @property
    def can_edit(self) -> bool:
        """True when a table is loaded, it has row keys and the model is not read-only."""
        return self.table is not None and self.table.editable and not self.read_only

    def has_changes(self) -> bool:
        """Report whether anything is waiting to be saved.

        Returns:
            True if a cell was edited, a row added or a row marked for deletion.
        """
        return bool(self.pending_changes())

    def pending_changes(self) -> RowChanges:
        """Collect the pending edits in the form :meth:`TableData.apply` takes.

        Returns:
            The updates, inserts and deletes.
        """
        changes = RowChanges()
        if self.table is None:
            return changes
        names = self.table.column_names
        for row in self.rows:
            if row.is_new:
                if not row.deleted:
                    changes.inserts.append({names[c]: v for c, v in row.edits.items()})
            elif row.deleted:
                changes.deletes.append(row.key)
            elif row.edits:
                changes.updates[row.key] = {names[c]: v for c, v in row.edits.items()}
        return changes

    def add_row(self) -> int:
        """Append an empty new row.

        Columns that are left unset get their database default on save.

        Returns:
            The index of the new row.
        """
        if not self.can_edit or self.table is None:
            return -1
        position = len(self.rows)
        self.beginInsertRows(QModelIndex(), position, position)
        self.rows.append(EditableRow(None, [None] * len(self.table.columns)))
        self.endInsertRows()
        self.changes_changed.emit()
        return position

    def toggle_deleted(self, row_indexes: list[int]) -> None:
        """Mark rows for deletion, or unmark them; new rows are removed at once.

        Args:
            row_indexes: Rows to toggle.
        """
        if not self.can_edit:
            return
        for index in sorted(set(row_indexes), reverse=True):
            if not 0 <= index < len(self.rows):
                continue
            row = self.rows[index]
            if row.is_new:
                self.beginRemoveRows(QModelIndex(), index, index)
                del self.rows[index]
                self.endRemoveRows()
            else:
                row.deleted = not row.deleted
                self.dataChanged.emit(self.index(index, 0), self.index(index, self.columnCount() - 1))
        self.changes_changed.emit()

    # -- Qt model interface -----------------------------------------------------
    def rowCount(self, parent: Index = QModelIndex()) -> int:
        """Return the number of rows on this page, including new rows.

        Args:
            parent: Parent index; only the invalid root index has rows.

        Returns:
            The row count.
        """
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: Index = QModelIndex()) -> int:
        """Return the number of table columns.

        Args:
            parent: Parent index; only the invalid root index has columns.

        Returns:
            The column count.
        """
        return 0 if parent.isValid() or self.table is None else len(self.table.columns)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        """Return the column name, with its declared type as tooltip.

        Args:
            section: Column index.
            orientation: Header orientation.
            role: Qt item data role.

        Returns:
            The header text or tooltip, otherwise None.
        """
        if orientation != Qt.Orientation.Horizontal or self.table is None:
            return None
        info = self.table.columns[section]
        if role == Qt.ItemDataRole.DisplayRole:
            return info.name + (" 🔑" if info.primary_key else "")
        if role == Qt.ItemDataRole.ToolTipRole:
            flags = [info.declared_type or "no type"]
            if info.not_null:
                flags.append("NOT NULL")
            if info.default is not None:
                flags.append(f"DEFAULT {info.default}")
            return " · ".join(flags)
        return None

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole):
        """Return cell text, colours and fonts that show pending changes.

        Args:
            index: Cell index.
            role: Qt item data role.

        Returns:
            The value for the role, or None.
        """
        if not index.isValid() or self.table is None:
            return None
        row = self.rows[index.row()]
        column = index.column()
        edited = column in row.edits
        value = row.edits[column] if edited else row.values[column]
        unset_new = row.is_new and not edited
        if role == Qt.ItemDataRole.DisplayRole:
            if unset_new:
                return "(default)" if self.table.columns[column].default is not None else "NULL"
            if value is None:
                return "NULL"
            if isinstance(value, (bytes, memoryview)):
                raw = bytes(value)
                return f"<{len(raw)} B> {format_hex(raw[:32])}{' …' if len(raw) > 32 else ''}"
            return str(value)
        if role == Qt.ItemDataRole.EditRole:
            return TableData.format_value(value)
        if role == Qt.ItemDataRole.ForegroundRole:
            if row.deleted:
                return QColor(theme.ERROR)
            if value is None or unset_new:
                return QColor(theme.TEXT_DIM)
            return None
        if role == Qt.ItemDataRole.BackgroundRole:
            if row.deleted:
                return _tinted(theme.ERROR, 40)
            if row.is_new:
                return _tinted(theme.OK, 45)
            if edited:
                return _tinted(theme.WARN)
            return None
        if role == Qt.ItemDataRole.FontRole and row.deleted:
            font = QFont()
            font.setStrikeOut(True)
            return font
        return None

    def flags(self, index: Index) -> Qt.ItemFlag:
        """Allow editing cells of rows that are not marked for deletion.

        Args:
            index: Cell index.

        Returns:
            The item flags.
        """
        base = super().flags(index)
        if index.isValid() and self.can_edit and not self.rows[index.row()].deleted:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def setData(self, index: Index, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        """Record an edited cell after converting the text to the column's type.

        Setting a cell back to its stored value removes the pending edit.

        Args:
            index: Cell index.
            value: Text typed by the user.
            role: Qt item data role; only the edit role is handled.

        Returns:
            True if the edit was recorded; conversion errors emit
            ``error_occurred`` and return False.
        """
        if role != Qt.ItemDataRole.EditRole or not index.isValid() or not self.can_edit or self.table is None:
            return False
        row = self.rows[index.row()]
        column = index.column()
        try:
            parsed = self.table.parse_value(self.table.columns[column].name, str(value))
        except (ValueError, TableDataError) as exc:
            self.error_occurred.emit(str(exc))
            return False
        if not row.is_new and parsed == row.values[column]:
            row.edits.pop(column, None)
        else:
            row.edits[column] = parsed
        self.dataChanged.emit(index, index)
        self.changes_changed.emit()
        return True


class TableEditor(QWidget):
    """Table chooser, search, paging, inline editing and Save/Revert for one database."""

    #: rows were written; the argument is the number of rows touched
    saved = Signal(int)

    def __init__(
        self,
        db: Database,
        table: str = "",
        read_only: bool = False,
        page_size: int = 200,
        parent: QWidget | None = None,
    ) -> None:
        """Build the toolbar, grid and pager, and load the first page.

        Args:
            db: Database to show; any SQLite file works.
            table: Table to show. Empty shows a chooser listing the
                database's own tables, with an option to include the
                workbench's tables.
            read_only: Hide the editing actions and refuse edits.
            page_size: Rows per page.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.db = db
        self.fixed_table = table
        self.read_only = read_only
        self.page_size = page_size
        self.offset = 0
        self.table: TableData | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.chooser = QComboBox()
        self.chooser.setMinimumWidth(200)
        self.chooser.currentTextChanged.connect(lambda _t: self.set_table(self.chooser.currentText()))
        self.show_workbench = QCheckBox("Show workbench tables")
        self.show_workbench.setToolTip("Also list the tables this application creates (sessions, packets, …)")
        self.show_workbench.toggled.connect(lambda _on: self.reload_table_list())
        for widget in (self.chooser, self.show_workbench):
            widget.setVisible(not table)
            bar.addWidget(widget)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search all columns")
        self.search.setClearButtonEnabled(True)
        bar.addWidget(self.search, 1)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply_search)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        self.buttons: dict[str, QPushButton] = {}
        for key, text, slot, name in (
            ("refresh", "Refresh", lambda: self.reload(), ""), ("add", "Add row", self.add_row, ""),
            ("delete", "Delete row", self.delete_selected_rows, "Danger"),
            ("revert", "Revert", self.revert_changes, ""), ("save", "Save", self.save_changes, "Primary"),
        ):
            button = QPushButton(text)
            button.setObjectName(name)
            button.clicked.connect(slot)
            bar.addWidget(button)
            self.buttons[key] = button
        for key in ("add", "delete", "revert", "save"):
            self.buttons[key].setVisible(not read_only)
        layout.addLayout(bar)

        self.model = EditableTableModel(read_only, self)
        self.model.error_occurred.connect(self._show_status_error)
        self.model.changes_changed.connect(self._update_actions)
        self.view = QTableView()
        self.view.setModel(self.model)
        configure_table(self.view)
        self.view.setFont(theme.monospace_font(9))
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        layout.addWidget(self.view, 1)

        pager = QHBoxLayout()
        self.prev_button = QPushButton("◀ Previous")
        self.next_button = QPushButton("Next ▶")
        self.prev_button.clicked.connect(lambda: self._change_page(-1))
        self.next_button.clicked.connect(lambda: self._change_page(1))
        self.page_label = QLabel("")
        self.page_label.setObjectName("Dim")
        pager.addWidget(self.prev_button)
        pager.addWidget(self.next_button)
        pager.addWidget(self.page_label, 1)
        layout.addLayout(pager)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.schema = QLabel("")
        self.schema.setObjectName("Dim")
        self.schema.setWordWrap(True)
        self.schema.setFont(theme.monospace_font(8))
        self.schema.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.schema)

        if table:
            self.set_table(table)
        else:
            self.reload_table_list()

    # -- choosing a table -----------------------------------------------------
    def reload_table_list(self) -> None:
        """Refill the table chooser, keeping the current table when it still exists."""
        if self.fixed_table:
            return
        current = self.chooser.currentText()
        names = user_table_names(self.db, include_workbench=self.show_workbench.isChecked())
        self.chooser.blockSignals(True)
        self.chooser.clear()
        self.chooser.addItems(names)
        self.chooser.setCurrentIndex(names.index(current) if current in names else (0 if names else -1))
        self.chooser.blockSignals(False)
        if names:
            self.set_table(self.chooser.currentText(), ask=False)
        else:
            self.table = None
            self.model.set_page(None, [])
            self.page_label.setText("")
            self.schema.setText("")
            self.status.setText("This database has no tables of its own yet."
                                + ("" if self.show_workbench.isChecked() else " Tick 'Show workbench tables' to see the app's tables."))

    def set_table(self, name: str, ask: bool = True) -> None:
        """Show another table from its first page.

        Args:
            name: Table name.
            ask: Ask before discarding unsaved changes.
        """
        if not name or (self.table is not None and name == self.table.table and self.model.rows):
            return
        if ask and not self.confirm_discard():
            if self.table is not None:
                self.chooser.blockSignals(True)
                self.chooser.setCurrentText(self.table.table)   # stay on the current table
                self.chooser.blockSignals(False)
            return
        try:
            self.table = TableData(self.db, name)
        except TableDataError as exc:
            self.table = None
            self.model.set_page(None, [])
            self._show_status_error(str(exc))
            return
        self.offset = 0
        self.reload(ask=False)

    # -- paging and searching --------------------------------------------------
    def reload(self, ask: bool = True) -> None:
        """Reload the current page from the database.

        Args:
            ask: Ask before discarding unsaved changes.
        """
        if self.table is None:
            self.reload_table_list()
            return
        if ask and not self.confirm_discard():
            return
        try:
            self.table = TableData(self.db, self.table.table)    # the schema may have changed
            text = self.search.text()
            total = self.table.count(text)
            self.offset = min(self.offset, max(0, (total - 1) // self.page_size * self.page_size))
            rows = self.table.fetch(self.page_size, self.offset, text)
        except (sqlite3.Error, TableDataError) as exc:
            self._show_status_error(str(exc))
            return
        self.model.set_page(self.table, rows)
        self.view.resizeColumnsToContents()
        end = min(total, self.offset + self.page_size)
        self.page_label.setText(f"{self.table.table}: rows {self.offset + 1 if total else 0}-{end} of {total}"
                                + (f" matching '{text}'" if text.strip() else ""))
        self.prev_button.setEnabled(self.offset > 0)
        self.next_button.setEnabled(end < total)
        self.schema.setText(self.table.schema_sql())
        if self.table.editable or self.read_only:
            self.status.setText("Double-click a cell to edit. Empty = NULL, BLOBs as hex." if self.model.can_edit else "")
            self.status.setStyleSheet(f"color: {theme.TEXT_DIM};")
        else:
            self._show_status_error("This table has no rowid or primary key, so it is shown read-only.")

    def _change_page(self, step: int) -> None:
        """Move one page forward or backward.

        Args:
            step: +1 for the next page, -1 for the previous page.
        """
        if not self.confirm_discard():
            return
        self.offset = max(0, self.offset + step * self.page_size)
        self.reload(ask=False)

    def _apply_search(self) -> None:
        """Show the first page of rows matching the search text."""
        if not self.confirm_discard():
            return
        self.offset = 0
        self.reload(ask=False)

    # -- editing ------------------------------------------------------------------
    def add_row(self) -> None:
        """Add an empty row at the end and start editing its first cell."""
        row = self.model.add_row()
        if row >= 0:
            index = self.model.index(row, 0)
            self.view.scrollTo(index)
            self.view.setCurrentIndex(index)
            self.view.edit(index)

    def delete_selected_rows(self) -> None:
        """Mark the selected rows for deletion; selecting them again unmarks them."""
        rows = sorted({index.row() for index in self.view.selectionModel().selectedIndexes()})
        self.model.toggle_deleted(rows)

    def save_changes(self) -> int:
        """Write every pending change in one transaction and reload.

        Constraint errors are shown and the changes are kept for fixing.

        Returns:
            The number of rows touched, or 0 if nothing was saved.
        """
        if self.table is None or not self.model.has_changes():
            return 0
        try:
            touched = self.table.apply(self.model.pending_changes())
        except (sqlite3.Error, TableDataError) as exc:
            self._show_status_error(f"Not saved, nothing was changed: {exc}")
            return 0
        self.reload(ask=False)
        self.status.setText(f"Saved {touched} row(s).")
        self.status.setStyleSheet(f"color: {theme.OK};")
        self.saved.emit(touched)
        return touched

    def revert_changes(self) -> None:
        """Discard every pending change and reload the page."""
        self.reload(ask=False)

    def confirm_discard(self) -> bool:
        """Ask whether unsaved changes may be discarded.

        Returns:
            True if there are no changes, or the user agreed to discard them.
        """
        if not self.model.has_changes():
            return True
        answer = QMessageBox.question(self, "Unsaved changes", "Discard the changes that are not saved yet?")
        return answer == QMessageBox.StandardButton.Yes

    def _update_actions(self) -> None:
        """Enable the editing buttons that apply to the current state."""
        editable = self.model.can_edit
        pending = self.model.has_changes()
        self.buttons["add"].setEnabled(editable)
        self.buttons["delete"].setEnabled(editable)
        self.buttons["save"].setEnabled(pending)
        self.buttons["revert"].setEnabled(pending)
        if pending:
            changes = self.model.pending_changes()
            self.status.setText(f"Unsaved: {len(changes.updates)} edited, {len(changes.inserts)} new, "
                                f"{len(changes.deletes)} to delete. Save writes them all at once.")
            self.status.setStyleSheet(f"color: {theme.WARN};")

    def _show_status_error(self, message: str) -> None:
        """Show an error under the grid without a dialog.

        Args:
            message: Error description.
        """
        self.status.setText(message)
        self.status.setStyleSheet(f"color: {theme.ERROR};")


def open_database_file(path: str) -> Database:
    """Open an existing SQLite file without running the workbench migrations.

    No tables are added and the journal mode is left as it is, so the file
    keeps only its own tables and settings.

    Args:
        path: Path of an existing database file.

    Returns:
        The database.

    Raises:
        FileNotFoundError: If the file does not exist.
        sqlite3.DatabaseError: If the file is not an SQLite database.
    """
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(f"database file not found: {file}")
    db = Database(file, wal=False)
    try:
        db.table_names()
    except sqlite3.DatabaseError:
        db.close()
        raise
    return db


__all__ = ["EditableRow", "EditableTableModel", "TableEditor", "open_database_file"]
