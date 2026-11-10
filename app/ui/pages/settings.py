from __future__ import annotations

from dataclasses import fields

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...context import AppContext
from ...core.config import LOG_LEVELS, AppConfig, ConfigError
from ..widgets.common import PageHeader, show_error

LABELS = {
    "database_path": "Database file",
    "backup_dir": "Backup directory",
    "log_dir": "Log directory",
    "log_level": "Log level",
    "log_to_database": "Write logs to the database",
    "max_database_log_rows": "Max log rows kept in database",
    "max_live_packets": "Max packets kept in the live monitor",
    "capture_flush_interval_ms": "Capture write interval (ms)",
    "monitor_refresh_interval_ms": "Monitor refresh interval (ms)",
    "default_timeout_ms": "Default connect timeout (ms)",
    "default_reconnect_interval_ms": "Default reconnect interval (ms)",
    "reports_dir": "Report directory",
}


class SettingsPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        """Build one editor per scalar configuration field.

        Workflow settings and list or dict values are skipped, because the
        Session Workflow page edits them.

        Args:
            ctx: Application context holding the configuration.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.ctx = ctx
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.addWidget(PageHeader("Settings", f"Stored in {ctx.config_file}. Most changes apply after a restart."))
        group = QGroupBox("Application")
        form = QFormLayout(group)
        self.editors: dict[str, QWidget] = {}
        for f in fields(AppConfig):
            value = getattr(ctx.config, f.name)
            if isinstance(value, (dict, list)) or f.name.startswith("workflow_"):
                continue  # edited in the Session Workflow page
            if f.name == "log_level":
                editor = QComboBox()
                editor.addItems(LOG_LEVELS)
                editor.setCurrentText(value)
            elif isinstance(value, bool):
                editor = QCheckBox()
                editor.setChecked(value)
            elif isinstance(value, int):
                editor = QSpinBox()
                editor.setRange(1, 100_000_000)
                editor.setValue(value)
            else:
                editor = QLineEdit(str(value))
            self.editors[f.name] = editor
            form.addRow(LABELS.get(f.name, f.name), editor)
        layout.addWidget(group)
        row = QHBoxLayout()
        save = QPushButton("Save settings")
        save.setObjectName("Primary")
        save.clicked.connect(self.save)
        row.addWidget(save)
        row.addStretch(1)
        layout.addLayout(row)
        info = QLabel(f"Database: {ctx.db.path}")
        info.setObjectName("Dim")
        layout.addWidget(info)
        layout.addStretch(1)

    def save(self) -> None:
        """Validate the edited values, apply them to the configuration and save it.

        Validation and save errors are shown in a dialog and nothing is applied.
        """
        values = {}
        for name, editor in self.editors.items():
            if isinstance(editor, QComboBox):
                values[name] = editor.currentText()
            elif isinstance(editor, QCheckBox):
                values[name] = editor.isChecked()
            elif isinstance(editor, QSpinBox):
                values[name] = editor.value()
            else:
                values[name] = editor.text().strip()
        current = {f.name: getattr(self.ctx.config, f.name) for f in fields(AppConfig)}
        candidate = AppConfig(**{**current, **values})
        try:
            candidate.validate()
        except ConfigError as exc:
            show_error(self, "Settings", exc)
            return
        for name, value in values.items():
            setattr(self.ctx.config, name, value)
        try:
            self.ctx.save_config()
        except OSError as exc:
            show_error(self, "Settings", exc)
            return
        QMessageBox.information(self, "Settings", "Settings saved. Restart to apply path and capture changes.")
