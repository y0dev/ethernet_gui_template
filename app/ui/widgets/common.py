"""Small reusable widgets."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..styles import theme


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 6)
        text = QVBoxLayout()
        text.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("PageTitle")
        text.addWidget(title_label)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("PageSubtitle")
            sub.setWordWrap(True)
            text.addWidget(sub)
        layout.addLayout(text, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(6)
        layout.addLayout(self.actions)


class StatCard(QFrame):
    def __init__(self, label: str, value: str = "0", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        self.value = QLabel(value)
        self.value.setObjectName("CardValue")
        caption = QLabel(label.upper())
        caption.setObjectName("CardLabel")
        layout.addWidget(self.value)
        layout.addWidget(caption)

    def set_value(self, value: str, color: str | None = None) -> None:
        self.value.setText(value)
        self.value.setStyleSheet(f"color: {color};" if color else "")


class StatusDot(QLabel):
    """Coloured status text such as ● Connected."""

    def set_state(self, state: str) -> None:
        color = theme.STATE_COLORS.get(state, theme.TEXT_DIM)
        self.setText(f"● {state}")
        self.setStyleSheet(f"color: {color}; background: transparent;")


class HexView(QPlainTextEdit):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(theme.monospace_font())
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)


def configure_table(view: QTableView, stretch_last: bool = True) -> None:
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setAlternatingRowColors(True)
    view.setShowGrid(False)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(24)
    view.horizontalHeader().setStretchLastSection(stretch_last)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    view.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    view.setWordWrap(False)


def show_error(parent: QWidget, title: str, error: Exception | str) -> None:
    QMessageBox.critical(parent, title, str(error))


def section_label(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("SectionTitle")
    return label
