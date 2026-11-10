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

LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


class LogModel(RecordTableModel):
    """Append-friendly log model with a row cap."""

    def __init__(self, max_rows: int = 20000, parent=None) -> None:
        super().__init__([
            Column("timestamp", "Time", fmt=format_timestamp),
            Column("level", "Level", color=lambda lvl: theme.LOG_LEVEL_COLORS.get(lvl)),
            Column("logger", "Logger"), Column("message", "Message"),
            Column("context", "Context", fmt=lambda c: json.dumps(c, default=str) if c else ""),
        ], parent)
        self.max_rows = max_rows

    def append(self, entry: dict) -> None:
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
        super().__init__(parent)
        self.ctx = ctx
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

        ctx.log_emitter.record_logged.connect(self.add)
        self.level.currentTextChanged.connect(self._rebuild)
        self.search.textChanged.connect(self._rebuild)
        self.view.selectionModel().currentRowChanged.connect(self._show_detail)

    def _accepts(self, entry: dict) -> bool:
        level = entry["level"]
        rank = LEVELS.index(level) if level in LEVELS else len(LEVELS)  # CRITICAL ranks highest
        if rank < LEVELS.index(self.level.currentText()):
            return False
        text = self.search.text().lower()
        return not text or text in f"{entry['logger']} {entry['message']} {entry['context']}".lower()

    def add(self, entry: dict) -> None:
        self._all.append(entry)
        if len(self._all) > self.model.max_rows:
            del self._all[: len(self._all) - self.model.max_rows]
        if self._accepts(entry):
            self.model.append(entry)
            if self.follow.isChecked() and self.isVisible():
                self.view.scrollToBottom()

    def _rebuild(self) -> None:
        self.model.set_records([e for e in self._all if self._accepts(e)])

    def clear(self) -> None:
        self._all.clear()
        self.model.set_records([])
        self.detail.clear()

    def load_history(self) -> None:
        rows = self.ctx.db.run("logs.recent", limit=5000)
        self._all = [
            {"timestamp": r["timestamp"], "level": r["level"], "logger": r["logger"],
             "message": r["message"], "context": json.loads(r["context_json"] or "{}")}
            for r in rows
        ]
        self._rebuild()

    def _show_detail(self, current, _previous) -> None:
        entry = self.model.record(current.row())
        if entry:
            self.detail.setPlainText(json.dumps(entry, indent=2, default=str))
