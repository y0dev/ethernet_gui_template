"""Application Logs page: every log record the application writes, live.

Records arrive through ``ctx.log_emitter`` (a Qt logging handler) as dicts with
timestamp, level, logger, message and a structured ``context``. The page keeps
up to 20,000 of them in memory, shows the ones that pass the level and text
filters, and shows the selected record as JSON underneath. *Load history*
replaces the view with the most recent records stored in the database.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...context import AppContext
from ..styles import theme
from ..widgets.common import HexView, PageHeader, configure_table
from ..widgets.table_models import Column, RecordTableModel, format_timestamp

# Order matters: the "Minimum level" filter compares positions in this list.
LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


class LogModel(RecordTableModel):
    """Append-friendly log model with a row cap."""

    def __init__(self, max_rows: int = 20000, parent=None) -> None:
        """Create an empty log model.

        Args:
            max_rows: Oldest rows are dropped beyond this count.
            parent: Optional Qt parent.
        """
        super().__init__([
            Column("timestamp", "Time", fmt=format_timestamp),
            Column("level", "Level", color=lambda lvl: theme.LOG_LEVEL_COLORS.get(lvl)),
            Column("logger", "Logger"), Column("message", "Message"),
            Column("context", "Context", fmt=lambda c: json.dumps(c, default=str) if c else ""),
        ], parent)
        self.max_rows = max_rows

    def append(self, entry: dict) -> None:
        """Append one record, dropping the oldest row when full.

        Args:
            entry: Log record dict.
        """
        # Full: drop the oldest row first. begin/endRemoveRows tell the view exactly what changed,
        # so it does not redraw everything.
        if len(self.records) >= self.max_rows:
            self.beginRemoveRows(QModelIndex(), 0, 0)
            self.records.pop(0)
            self.endRemoveRows()
        row = len(self.records)
        self.beginInsertRows(QModelIndex(), row, row)
        self.records.append(entry)
        self.endInsertRows()


class LogsPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        """Build the filter bar, log table and detail view, and subscribe to new records.

        Args:
            ctx: Application context.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.ctx = ctx
        # Every kept record, filtered or not. The table model holds only the visible ones, so
        # changing the filter can rebuild it from here.
        self._all: list[dict] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        header = PageHeader("Application Logs", "Structured log records (also written as JSON lines and to the database).")
        load = QPushButton("Load history")
        load.clicked.connect(self.load_history)
        clear = QPushButton("Clear view")
        clear.clicked.connect(self.clear)
        header.actions.addWidget(load)
        header.actions.addWidget(clear)
        layout.addWidget(header)

        # Filter bar: minimum level, free-text search, and whether to scroll to new records.
        bar = QHBoxLayout()
        self.level = QComboBox()
        self.level.addItems(LEVELS)
        self.level.setCurrentText("INFO")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by text")
        self.follow = QCheckBox("Follow")
        self.follow.setChecked(True)
        bar.addWidget(QLabel("Minimum level"))
        bar.addWidget(self.level)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.follow)
        layout.addLayout(bar)

        # Log table on top, JSON detail of the selected record below; the user can drag the divider.
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.model = LogModel(parent=self)
        self.view = QTableView()
        self.view.setModel(self.model)
        configure_table(self.view)
        self.view.setFont(theme.monospace_font(9))
        for col, width in enumerate((100, 70, 220, 420)):
            self.view.setColumnWidth(col, width)
        splitter.addWidget(self.view)
        self.detail = HexView()
        splitter.addWidget(self.detail)
        splitter.setSizes([500, 140])
        layout.addWidget(splitter, 1)

        # New records arrive here from any thread via the log handler; filters rebuild the table
        # when changed.
        ctx.log_emitter.record_logged.connect(self.add_record)
        self.level.currentTextChanged.connect(self._apply_filter)
        self.search.textChanged.connect(self._apply_filter)
        self.view.selectionModel().currentRowChanged.connect(self._show_detail)

    def _passes_filter(self, entry: dict) -> bool:
        """Check a record against the minimum level and the search text.

        Args:
            entry: Log record dict.

        Returns:
            True if the record should be shown.
        """
        level = entry["level"]
        rank = LEVELS.index(level) if level in LEVELS else len(LEVELS)  # CRITICAL ranks highest
        if rank < LEVELS.index(self.level.currentText()):
            return False
        text = self.search.text().lower()
        return not text or text in f"{entry['logger']} {entry['message']} {entry['context']}".lower()

    def add_record(self, entry: dict) -> None:
        """Keep a new record and show it if it passes the filter.

        Args:
            entry: Log record dict from the Qt log handler.
        """
        # Keep the record even if it is filtered out now, but cap the list at the same size as the
        # model.
        self._all.append(entry)
        if len(self._all) > self.model.max_rows:
            del self._all[: len(self._all) - self.model.max_rows]
        if self._passes_filter(entry):
            self.model.append(entry)
            # Scrolling a hidden table is wasted work, so only follow while the page is shown.
            if self.follow.isChecked() and self.isVisible():
                self.view.scrollToBottom()

    def _apply_filter(self) -> None:
        """Rebuild the table from every kept record that passes the filter."""
        self.model.set_records([e for e in self._all if self._passes_filter(e)])

    def clear(self) -> None:
        """Forget every kept record and empty the view."""
        self._all.clear()
        self.model.set_records([])
        self.detail.clear()

    def load_history(self) -> None:
        """Replace the view with the 5000 most recent records from the database."""
        rows = self.ctx.db.run("logs.recent", limit=5000)
        self._all = [
            {"timestamp": r["timestamp"], "level": r["level"], "logger": r["logger"],
             "message": r["message"], "context": json.loads(r["context_json"] or "{}")}
            for r in rows
        ]
        self._apply_filter()

    def _show_detail(self, current, _previous) -> None:
        """Show the selected record as formatted JSON.

        Args:
            current: Newly selected index.
            _previous: Previously selected index (unused).
        """
        entry = self.model.record(current.row())
        if entry:
            self.detail.setPlainText(json.dumps(entry, indent=2, default=str))
