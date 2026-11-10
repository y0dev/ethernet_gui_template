"""Main window: sidebar navigation, page stack, dockable diagnostics, status bar."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..context import AppContext
from ..project import ProjectProfile
from .registry import PageRegistry, PageSpec
from .styles import theme
from .widgets.common import configure_table
from .widgets.table_models import Column, RecordTableModel


def default_pages() -> list[PageSpec]:
    """Resolve every built-in page in default order.

    Used when no project profile is given.

    Returns:
        The page specs that could be imported.
    """
    return PageRegistry().select(None)[0]


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext, pages: list[PageSpec] | None = None,
                 profile: ProjectProfile | None = None) -> None:
        """Build the sidebar, page stack, docks, menus and status bar.

        The saved window layout and page are restored afterwards.

        Args:
            ctx: Application context passed to every page.
            pages: Pages to show, in navigation order; defaults to every
                built-in page.
            profile: Project profile for titles and the start page; defaults to
                the built-in profile.
        """
        super().__init__()
        self.ctx = ctx
        self.profile = profile or ProjectProfile()
        self.page_specs = pages if pages is not None else default_pages()
        self.setWindowTitle(self.profile.app_name)
        self.resize(1440, 900)
        self.setDockOptions(QMainWindow.DockOption.AllowTabbedDocks | QMainWindow.DockOption.AnimatedDocks)

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(210)
        sv = QVBoxLayout(sidebar)
        sv.setContentsMargins(0, 0, 0, 0)
        title = QLabel(self.profile.app_name)
        title.setWordWrap(True)
        title.setObjectName("AppTitle")
        subtitle = QLabel(self.profile.app_subtitle)
        subtitle.setObjectName("AppSubtitle")
        sv.addWidget(title)
        sv.addWidget(subtitle)
        self.nav = QListWidget()
        self.nav.setObjectName("NavList")
        sv.addWidget(self.nav, 1)
        root.addWidget(sidebar)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.pages: dict[str, QWidget] = {}          # by navigation title
        self.pages_by_key: dict[str, QWidget] = {}
        for index, spec in enumerate(self.page_specs, start=1):
            page = spec.resolve()(ctx)
            self.pages[spec.title] = page
            self.pages_by_key[spec.key] = page
            self.stack.addWidget(page)
            item = QListWidgetItem(f"{index}  {spec.title}")
            item.setToolTip(spec.description or f"{spec.key} ({spec.origin})")
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)

        self._build_docks()
        self._build_menu()
        self._build_status_bar()
        self._restore_layout()
        if self.nav.currentRow() < 0:
            self.nav.setCurrentRow(0)

    # -- docks ------------------------------------------------------------
    def _build_docks(self) -> None:
        """Create the connection status and diagnostics docks and start their refresh timer."""
        self.status_model = RecordTableModel([
            Column("name", "Connection"),
            Column("state", "State", color=lambda s: theme.STATE_COLORS.get(s)),
            Column("rx", "RX"), Column("tx", "TX"),
        ])
        status_view = QTableView()
        status_view.setModel(self.status_model)
        configure_table(status_view)
        self.status_dock = QDockWidget("Connection Status", self)
        self.status_dock.setObjectName("ConnectionStatusDock")
        self.status_dock.setWidget(status_view)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.status_dock)

        self.log_console = QPlainTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setFont(theme.monospace_font(9))
        self.log_console.setMaximumBlockCount(2000)
        self.log_dock = QDockWidget("Diagnostics", self)
        self.log_dock.setObjectName("DiagnosticsDock")
        self.log_dock.setWidget(self.log_console)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.ctx.log_emitter.record_logged.connect(self._append_log)
        self.ctx.connections.error_occurred.connect(self._on_connection_error)

        self._dock_timer = QTimer(self)
        self._dock_timer.setInterval(500)
        self._dock_timer.timeout.connect(self._refresh_status_dock)
        self._dock_timer.start()

    def _append_log(self, entry: dict) -> None:
        """Append one log record to the diagnostics console, coloured by level.

        Args:
            entry: Record dict from the Qt log handler.
        """
        color = theme.LOG_LEVEL_COLORS.get(entry["level"], theme.TEXT)
        context = f" {json.dumps(entry['context'], default=str)}" if entry["context"] else ""
        from .widgets.table_models import format_timestamp
        line = f"{format_timestamp(entry['timestamp'])} {entry['level']:<7} {entry['message']}{context}"
        self.log_console.appendHtml(
            f'<span style="color:{color}; white-space:pre">{_escape_html(line)}</span>'
        )

    def _on_connection_error(self, connection_id: int, message: str) -> None:
        """Show a connection error in the status bar.

        Args:
            connection_id: Connection that failed.
            message: Error description.
        """
        config = self.ctx.connections.config(connection_id)
        name = config.name if config else str(connection_id)
        self.statusBar().showMessage(f"{name}: {message}", 6000)

    def _refresh_status_dock(self) -> None:
        """Refresh the connection status table and the status bar indicators.

        Skipped while the status dock is hidden.
        """
        if not self.status_dock.isVisible():
            return
        rows = []
        for c in self.ctx.connections.runtime_configs() + self.ctx.connections.configs():
            ep = self.ctx.connections.endpoint(c.id)
            rows.append({"name": c.name, "state": self.ctx.connections.state(c.id).value,
                         "rx": ep.rx_bytes if ep else 0, "tx": ep.tx_bytes if ep else 0})
        self.status_model.set_records(rows)
        active = sum(1 for r in rows if r["state"] in ("Connected", "Listening"))
        self.conn_label.setText(f"● {active}/{len(rows)} active")
        self.conn_label.setStyleSheet(f"color: {theme.OK if active else theme.TEXT_DIM};")
        sid = self.ctx.capture.session_id
        self.rec_label.setText(f"● REC #{sid}" if sid else "")
        self.rec_label.setStyleSheet(f"color: {theme.ERROR};")

    # -- menu / status ----------------------------------------------------
    def _build_menu(self) -> None:
        """Build the File, View and Help menus.

        The View menu gets a ``Ctrl+N`` shortcut for each of the first nine
        pages.
        """
        file_menu = self.menuBar().addMenu("&File")
        backup = QAction("Backup database", self)
        backup.triggered.connect(self._backup_database)
        file_menu.addAction(backup)
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = self.menuBar().addMenu("&View")
        for i, spec in enumerate(self.page_specs):
            action = QAction(spec.title, self)
            if i < 9:
                action.setShortcut(QKeySequence(f"Ctrl+{i + 1}"))
            action.triggered.connect(lambda _c=False, row=i: self.nav.setCurrentRow(row))
            view_menu.addAction(action)
        view_menu.addSeparator()
        view_menu.addAction(self.status_dock.toggleViewAction())
        view_menu.addAction(self.log_dock.toggleViewAction())

        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("About", self)
        about.triggered.connect(lambda: QMessageBox.about(
            self, self.profile.app_name,
            f"{self.profile.app_name}\n{self.profile.app_subtitle}\n\n"
            f"Pages: {', '.join(s.key for s in self.page_specs)}\n"
            f"Profile: {self.profile.source or 'built-in defaults'}",
        ))
        help_menu.addAction(about)

    def _build_status_bar(self) -> None:
        """Add the recording, connection and database indicators to the status bar."""
        self.conn_label = QLabel("")
        self.rec_label = QLabel("")
        db_label = QLabel(f"DB: {self.ctx.db.path.name}")
        db_label.setToolTip(str(self.ctx.db.path))
        workflow = getattr(self.ctx, "workflow", None)  # optional feature
        if workflow is not None:
            workflow.database_changed.connect(
                lambda path: (db_label.setText(f"DB: {Path(path).name}"), db_label.setToolTip(path))
            )
        for w in (self.rec_label, self.conn_label, db_label):
            self.statusBar().addPermanentWidget(w)
        self.statusBar().showMessage("Ready", 3000)

    def _backup_database(self) -> None:
        """Back up the database and report the result."""
        try:
            path = self.ctx.backup_database()
            self.statusBar().showMessage(f"Backup written to {path}", 6000)
        except Exception as exc:
            QMessageBox.critical(self, "Backup", str(exc))

    # -- persistence of window layout -------------------------------------
    def _layout_settings(self) -> QSettings:
        """Return the settings store for the window layout.

        Returns:
            Settings keyed by the profile's organization and application name.
        """
        return QSettings(self.profile.organization, self.profile.app_name)

    def _restore_layout(self) -> None:
        """Restore the saved geometry and dock state, then select the start page.

        The profile's ``start_page`` wins over the last page shown.
        """
        settings = self._layout_settings()
        geometry = settings.value("geometry")
        state = settings.value("windowState")
        if geometry:
            self.restoreGeometry(geometry)
        if state:
            self.restoreState(state)
        else:  # first run: sensible dock sizes once the window has a size
            QTimer.singleShot(0, self._apply_default_dock_sizes)
        start = self.profile.start_page
        keys = [s.key for s in self.page_specs]
        if start in keys:
            self.nav.setCurrentRow(keys.index(start))
        else:
            row = settings.value("page", 0, int)
            self.nav.setCurrentRow(row if 0 <= row < self.nav.count() else 0)

    def _apply_default_dock_sizes(self) -> None:
        """Give the docks sensible sizes on first run."""
        self.resizeDocks([self.status_dock], [260], Qt.Orientation.Horizontal)
        self.resizeDocks([self.log_dock], [150], Qt.Orientation.Vertical)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Save the window geometry, dock state and current page before closing.

        Args:
            event: Qt close event.
        """
        settings = self._layout_settings()
        settings.setValue("geometry", self.saveGeometry())
        settings.setValue("windowState", self.saveState())
        settings.setValue("page", self.nav.currentRow())
        self._dock_timer.stop()
        super().closeEvent(event)


def _escape_html(text: str) -> str:
    """Escape text for inclusion in HTML.

    Args:
        text: Plain text.

    Returns:
        The text with ``&``, ``<`` and ``>`` escaped.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
