"""Session Workflow: a guided five-step process.

1 Server      open a TCP/UDP server so devices can connect (IP, port, framing)
2 Database    use, open or create the SQLite file the session is stored in
3 Devices     see who connected, name them, start the recorded session
4 Operations  pick operations from the menu and run them on selected devices
5 Report      preview and export the session report (HTML / Markdown / CSV)

All logic lives in WorkflowService; this module only presents it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtCore import QModelIndex, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableView,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...context import AppContext
from ...core.hexutil import format_hex, to_ascii
from ...core.models import DeviceOperation, MatchMode, PayloadFormat, Transport
from ...core.reporting import collect, render_csv, render_html, render_markdown
from ...core.services import WorkflowError
from ..dialogs.editors import FramingEditor
from ..styles import theme
from ..widgets.common import PageHeader, StatusDot, configure_table, section_label, show_error
from ..widgets.table_models import Column, RecordTableModel, format_timestamp

STEPS = ("Server", "Database", "Devices", "Operations", "Report")
RESULT_COLORS = {"passed": theme.OK, "failed": theme.ERROR, "error": theme.ERROR,
                 "timeout": theme.WARN, "sent": theme.TEXT_DIM}


def local_ipv4_addresses() -> list[str]:
    addresses = []
    for address in QNetworkInterface.allAddresses():
        if address.protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol and not address.isLoopback():
            addresses.append(address.toString())
    return sorted(set(addresses))


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Dim")
    label.setWordWrap(True)
    return label


# =============================================================================
# Step 1: server
# =============================================================================
class ServerStep(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.wf = ctx.workflow
        cfg = ctx.config
        layout = QVBoxLayout(self)
        layout.addWidget(_hint("Open a server that your devices connect to. The address and port are "
                               "remembered for next time."))
        box = QGroupBox("Server configuration")
        form = QFormLayout(box)
        self.transport = QComboBox()
        self.transport.addItem("TCP server", Transport.TCP_SERVER.value)
        self.transport.addItem("UDP server", Transport.UDP_SERVER.value)
        self.transport.setCurrentIndex(max(0, self.transport.findData(cfg.workflow_server_transport)))
        self.host = QComboBox()
        self.host.setEditable(True)
        self.host.addItem("0.0.0.0", "0.0.0.0")
        self.host.setItemData(0, "All network interfaces", Qt.ItemDataRole.ToolTipRole)
        self.host.addItem("127.0.0.1", "127.0.0.1")
        for address in local_ipv4_addresses():
            self.host.addItem(address, address)
        self.host.setEditText(cfg.workflow_server_host)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(cfg.workflow_server_port)
        self.framing = FramingEditor(cfg.workflow_server_framing)
        form.addRow("Transport", self.transport)
        form.addRow("Bind IP address", self.host)
        form.addRow("Port", self.port)
        form.addRow("Message framing (TCP)", self.framing)
        form.addRow("", _hint("0.0.0.0 accepts devices on every network interface; 127.0.0.1 only on this "
                              "computer. Framing says where one message ends in the TCP stream "
                              "(e.g. Delimiter 0a for newline-terminated messages)."))
        layout.addWidget(box)

        row = QHBoxLayout()
        self.start_button = QPushButton("Start server")
        self.start_button.setObjectName("Primary")
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton("Stop server")
        self.stop_button.clicked.connect(self.stop)
        self.state = StatusDot()
        row.addWidget(self.start_button)
        row.addWidget(self.stop_button)
        row.addSpacing(12)
        row.addWidget(self.state)
        row.addStretch(1)
        layout.addLayout(row)
        self.connect_hint = QLabel("")
        self.connect_hint.setWordWrap(True)
        self.connect_hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.connect_hint)
        layout.addStretch(1)

        self.transport.currentIndexChanged.connect(self._transport_changed)
        self.wf.server_state_changed.connect(lambda _s: self.refresh())
        self._transport_changed()
        self.refresh()

    def _transport_changed(self) -> None:
        self.framing.setEnabled(self.transport.currentData() == Transport.TCP_SERVER.value)

    def start(self) -> None:
        host = self.host.currentText().strip() or "0.0.0.0"
        try:
            self.wf.start_server(host, self.port.value(), Transport(self.transport.currentData()),
                                 self.framing.value())
        except (WorkflowError, ValueError) as exc:
            show_error(self, "Start server", exc)
        self.refresh()

    def stop(self) -> None:
        if self.wf.session_id is not None and QMessageBox.question(
            self, "Stop server", "A session is recording. Stop the server anyway? Devices will be disconnected."
        ) != QMessageBox.StandardButton.Yes:
            return
        self.wf.stop_server()
        self.refresh()

    def refresh(self) -> None:
        running = self.wf.server_running
        self.state.set_state(self.wf.server_state.value)
        self.start_button.setText("Restart server" if running else "Start server")
        self.stop_button.setEnabled(running)
        for w in (self.transport, self.host, self.port):
            w.setEnabled(not running)
        self.framing.setEnabled(not running and self.transport.currentData() == Transport.TCP_SERVER.value)
        if running:
            address = self.wf.server_address()
            host, _, port = address.rpartition(":")
            targets = local_ipv4_addresses() if host in ("0.0.0.0", "::", "") else [host]
            shown = ", ".join(f"{t}:{port}" for t in targets) or address
            self.connect_hint.setText(f"Listening on {address}. Point your devices at: {shown}")
            self.connect_hint.setStyleSheet(f"color: {theme.OK};")
        else:
            self.connect_hint.setText("")


# =============================================================================
# Step 2: database
# =============================================================================
class DatabaseStep(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.wf = ctx.workflow
        layout = QVBoxLayout(self)
        layout.addWidget(_hint("Choose where this session is stored. Use the current database, open an "
                               "existing one or create a new file (for example one per test campaign)."))
        box = QGroupBox("Selected database")
        grid = QGridLayout(box)
        self.labels: dict[str, QLabel] = {}
        for i, (key, title) in enumerate((("path", "File"), ("schema_version", "Schema version"),
                                          ("sessions", "Recorded sessions"), ("operations", "Operations defined"),
                                          ("devices", "Devices in inventory"))):
            grid.addWidget(QLabel(title), i, 0)
            value = QLabel("-")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(value, i, 1)
            self.labels[key] = value
        grid.setColumnStretch(1, 1)
        layout.addWidget(box)

        row = QHBoxLayout()
        use = QPushButton("Use this database")
        use.setObjectName("Primary")
        use.clicked.connect(self.use_current)
        open_button = QPushButton("Open existing…")
        open_button.clicked.connect(self.open_existing)
        new_button = QPushButton("Create new…")
        new_button.clicked.connect(self.create_new)
        self.make_default = QCheckBox("Open this database automatically at startup")
        for w in (use, open_button, new_button):
            row.addWidget(w)
        row.addSpacing(12)
        row.addWidget(self.make_default)
        row.addStretch(1)
        layout.addLayout(row)
        self.status = QLabel("")
        layout.addWidget(self.status)

        layout.addWidget(section_label("Recent databases"))
        self.recent = QListWidget()
        self.recent.itemDoubleClicked.connect(lambda item: self._open(item.text()))
        layout.addWidget(self.recent, 1)
        layout.addWidget(_hint("Double-click a recent database to open it."))
        self.wf.database_changed.connect(lambda _p: self.refresh())
        self.wf.session_changed.connect(lambda _s: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        info = self.wf.database_info()
        for key, label in self.labels.items():
            label.setText(str(info[key]))
        self.recent.clear()
        for path in self.ctx.config.recent_databases:
            item = QListWidgetItem(path)
            if not Path(path).exists():
                item.setForeground(QColor(theme.TEXT_DIM))
                item.setToolTip("file not found")
            self.recent.addItem(item)
        if self.wf.session_id is not None:
            self.status.setText("A session is recording; finish it (step 5) before switching databases.")
            self.status.setStyleSheet(f"color: {theme.WARN};")
        elif self.wf.database_confirmed:
            self.status.setText(f"✓ Session data will be stored in {info['path']}")
            self.status.setStyleSheet(f"color: {theme.OK};")
        else:
            self.status.setText("Confirm a database to continue.")
            self.status.setStyleSheet("")

    def use_current(self) -> None:
        self.wf.use_current_database()
        if self.make_default.isChecked():
            self.ctx.config.database_path = str(self.ctx.db.path)
            self.ctx.save_config()
        self.refresh()

    def open_existing(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open database", str(self.ctx.db.path.parent),
                                              "SQLite database (*.db *.sqlite *.sqlite3)")
        if path:
            self._open(path)

    def create_new(self) -> None:
        stamp = time.strftime("%Y%m%d_%H%M")
        path, _ = QFileDialog.getSaveFileName(self, "Create database", str(self.ctx.db.path.parent / f"session_{stamp}.db"),
                                              "SQLite database (*.db)")
        if path:
            if not path.lower().endswith((".db", ".sqlite", ".sqlite3")):
                path += ".db"
            self._open(path)

    def _open(self, path: str) -> None:
        try:
            self.wf.open_database(path, make_default=self.make_default.isChecked())
        except (WorkflowError, OSError) as exc:
            show_error(self, "Open database", exc)
        self.refresh()


# =============================================================================
# Step 3: devices
# =============================================================================
class DevicesStep(QWidget):
    def __init__(self, ctx: AppContext, page: WorkflowPage, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.wf = ctx.workflow
        self.page = page
        layout = QVBoxLayout(self)
        session = QGroupBox("Session")
        srow = QHBoxLayout(session)
        self.session_name = QLineEdit(f"Session {time.strftime('%Y-%m-%d %H:%M')}")
        self.start_button = QPushButton("Start session")
        self.start_button.setObjectName("Primary")
        self.start_button.clicked.connect(self.start_session)
        self.session_status = QLabel("")
        srow.addWidget(QLabel("Name"))
        srow.addWidget(self.session_name, 1)
        srow.addWidget(self.start_button)
        srow.addWidget(self.session_status)
        layout.addWidget(session)
        layout.addWidget(_hint("Everything sent and received is recorded while the session runs. "
                               "Right-click a device for its operations menu."))

        bar = QHBoxLayout()
        bar.addWidget(section_label("Connected devices"))
        bar.addStretch(1)
        for text, slot in (("Name device…", self.rename), ("Disconnect", self.disconnect),
                           ("Clear disconnected", self.wf.forget_disconnected)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            bar.addWidget(b)
        layout.addLayout(bar)
        self.model = RecordTableModel([
            Column("label", "Device"), Column("peer", "Address"),
            Column("status", "Status", color=lambda s: theme.OK if s == "Connected" else theme.TEXT_DIM),
            Column("since", "Connected at", fmt=lambda t: format_timestamp(t)[:8]),
            Column("rx", "RX msgs"), Column("tx", "TX msgs"), Column("rx_bytes", "RX bytes"),
            Column("tx_bytes", "TX bytes"), Column("pending", "Queued ops"),
        ])
        self.view = QTableView()
        self.view.setModel(self.model)
        configure_table(self.view)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.doubleClicked.connect(lambda _i: self._go_operations())
        layout.addWidget(self.view, 1)
        self.waiting = _hint("")
        layout.addWidget(self.waiting)

        self.wf.devices_changed.connect(self.refresh)
        self.wf.session_changed.connect(lambda _s: self.refresh())
        ctx.operations.queue_changed.connect(lambda _n: self.refresh())
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(lambda: self.isVisible() and self.refresh())
        self._timer.start()
        self.refresh()

    def selected_peers(self) -> list[str]:
        rows = sorted({i.row() for i in self.view.selectionModel().selectedRows()})
        return [self.model.record(r)["peer"] for r in rows if self.model.record(r)]

    def refresh(self) -> None:
        selected = set(self.selected_peers())
        rows = []
        sid = self.wf.server_id
        for d in sorted(self.wf.devices.values(), key=lambda d: (not d.connected, d.connected_at)):
            rows.append({
                "label": d.label, "peer": d.peer, "status": "Connected" if d.connected else "Disconnected",
                "since": d.connected_at, "rx": d.rx_frames, "tx": d.tx_frames, "rx_bytes": d.rx_bytes,
                "tx_bytes": d.tx_bytes, "pending": self.ctx.operations.pending(sid, d.peer) if sid is not None else 0,
            })
        self.model.set_records(rows)
        for i, r in enumerate(rows):
            if r["peer"] in selected:
                self.view.selectionModel().select(
                    self.model.index(i, 0),
                    self.view.selectionModel().SelectionFlag.Select | self.view.selectionModel().SelectionFlag.Rows,
                )
        sid_session = self.wf.session_id
        self.start_button.setText("Session running" if sid_session else "Start session")
        self.start_button.setEnabled(sid_session is None)
        self.session_name.setEnabled(sid_session is None)
        if sid_session:
            self.session_status.setText(f"● Recording session #{sid_session}")
            self.session_status.setStyleSheet(f"color: {theme.ERROR};")
        else:
            self.session_status.setText("Not recording")
            self.session_status.setStyleSheet(f"color: {theme.TEXT_DIM};")
        connected = len(self.wf.connected_devices())
        if not self.wf.server_running:
            self.waiting.setText("The server is not running (step 1).")
        elif connected == 0:
            self.waiting.setText(f"Waiting for devices to connect to {self.wf.server_address()} …")
        else:
            self.waiting.setText(f"{connected} device(s) connected.")

    def start_session(self) -> None:
        try:
            self.wf.start_session(self.session_name.text().strip())
        except WorkflowError as exc:
            show_error(self, "Start session", exc)
        self.refresh()
        self.page.update_steps()

    def rename(self) -> None:
        peers = self.selected_peers()
        if len(peers) != 1:
            show_error(self, "Name device", "Select one device.")
            return
        device = self.wf.devices[peers[0]]
        name, ok = QInputDialog.getText(self, "Name device", f"Name for {device.peer}:", text=device.name)
        if not ok:
            return
        save = QMessageBox.question(
            self, "Name device", "Also save this device in the Device Manager inventory?\n"
            "(Devices from the same IP address will then get this name automatically.)",
        ) == QMessageBox.StandardButton.Yes
        try:
            self.wf.rename_device(device.peer, name, save_to_inventory=save)
        except Exception as exc:
            show_error(self, "Name device", exc)

    def disconnect(self) -> None:
        for peer in self.selected_peers():
            try:
                self.wf.disconnect_device(peer)
            except WorkflowError as exc:
                show_error(self, "Disconnect", exc)
                return

    def _go_operations(self) -> None:
        self.page.operations_step.set_targets(self.selected_peers())
        self.page.go_to(3)

    def _context_menu(self, pos) -> None:
        index = self.view.indexAt(pos)
        if not index.isValid():
            return
        if not self.view.selectionModel().isRowSelected(index.row(), QModelIndex()):
            self.view.selectRow(index.row())
        peers = self.selected_peers()
        connected = [p for p in peers if self.wf.devices.get(p) and self.wf.devices[p].connected]
        menu = QMenu(self)
        title = menu.addAction(f"{len(peers)} device(s) selected")
        title.setEnabled(False)
        ops_menu = menu.addMenu("Run operation")
        ops = self.wf.list_operations()
        if not ops:
            empty = ops_menu.addAction("(no operations defined, see step 4)")
            empty.setEnabled(False)
        categories: dict[str, QMenu] = {}
        for op in ops:
            sub = categories.get(op.category)
            if sub is None:
                sub = ops_menu.addMenu(op.category)
                categories[op.category] = sub
            action = QAction(op.name, sub)
            action.setToolTip(op.description)
            action.triggered.connect(lambda _c=False, o=op: self.page.operations_step.run(connected, [o]))
            sub.addAction(action)
        ops_menu.setEnabled(bool(connected) and self.wf.session_id is not None)
        menu.addAction("Open operations panel…", self._go_operations)
        menu.addSeparator()
        menu.addAction("Name device…", self.rename)
        menu.addAction("Disconnect", self.disconnect)
        menu.exec(self.view.viewport().mapToGlobal(pos))


# =============================================================================
# Step 4: operations
# =============================================================================
class OperationDialog(QDialog):
    def __init__(self, op: DeviceOperation | None, categories: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit operation" if op else "New operation")
        self.op = op or DeviceOperation(name="", payload_text="")
        o = self.op
        form = QFormLayout()
        self.name = QLineEdit(o.name)
        self.category = QComboBox()
        self.category.setEditable(True)
        self.category.addItems(sorted(set(categories) | {o.category}))
        self.category.setCurrentText(o.category)
        self.description = QLineEdit(o.description)
        self.format = QComboBox()
        self.format.addItem("ASCII (escapes \\r \\n \\t \\0 \\xNN)", PayloadFormat.ASCII.value)
        self.format.addItem("HEX (01 02 0A …)", PayloadFormat.HEX.value)
        self.format.setCurrentIndex(max(0, self.format.findData(o.payload_format.value)))
        self.payload = QPlainTextEdit(o.payload_text)
        self.expected = QPlainTextEdit(o.expected_text)
        for box in (self.payload, self.expected):
            box.setFont(theme.monospace_font())
            box.setFixedHeight(56)
        self.match = QComboBox()
        for mode in MatchMode:
            self.match.addItem(mode.label, mode.value)
        self.match.setCurrentIndex(max(0, self.match.findData(o.match_mode.value)))
        self.timeout = QSpinBox()
        self.timeout.setRange(10, 600000)
        self.timeout.setSuffix(" ms")
        self.timeout.setValue(o.timeout_ms)
        self.preview = QLabel("")
        self.preview.setFont(theme.monospace_font(9))
        self.preview.setWordWrap(True)
        form.addRow("Name", self.name)
        form.addRow("Menu category", self.category)
        form.addRow("Description", self.description)
        form.addRow("Format", self.format)
        form.addRow("Send", self.payload)
        form.addRow("Response rule", self.match)
        form.addRow("Expected response", self.expected)
        form.addRow("Timeout", self.timeout)
        form.addRow("", self.preview)
        form.addRow("", _hint("Enter the message itself. The server's framing is added when sending "
                              "(e.g. the delimiter) and removed from the reply before it is compared."))
        for signal in (self.payload.textChanged, self.expected.textChanged):
            signal.connect(self._update)
        self.format.currentIndexChanged.connect(self._update)
        self.match.currentIndexChanged.connect(self._update)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.resize(560, 0)
        self._update()

    def _build(self) -> DeviceOperation:
        return DeviceOperation(
            id=self.op.id, name=self.name.text().strip(), category=self.category.currentText().strip() or "General",
            description=self.description.text().strip(), payload_format=PayloadFormat(self.format.currentData()),
            payload_text=self.payload.toPlainText(), match_mode=MatchMode(self.match.currentData()),
            expected_text=self.expected.toPlainText(), timeout_ms=self.timeout.value(), sort_order=self.op.sort_order,
        )

    def _update(self) -> None:
        mode = MatchMode(self.match.currentData())
        self.expected.setEnabled(mode not in (MatchMode.NONE, MatchMode.ANY))
        try:
            op = self._build()
            text = f"send {len(op.payload)} B: {format_hex(op.payload[:24])}"
            if op.expected:
                text += f"\nexpect: {format_hex(op.expected[:24])}"
            self.preview.setText(text)
            self.preview.setStyleSheet(f"color: {theme.TEXT_DIM};")
        except ValueError as exc:
            self.preview.setText(str(exc))
            self.preview.setStyleSheet(f"color: {theme.ERROR};")

    def _accept(self) -> None:
        try:
            op = self._build()
            op.validate()
        except ValueError as exc:
            show_error(self, "Operation", exc)
            return
        self.op = op
        self.accept()


class OperationsStep(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.wf = ctx.workflow
        layout = QVBoxLayout(self)
        top = QSplitter(Qt.Orientation.Horizontal)

        # targets
        targets = QWidget()
        tv = QVBoxLayout(targets)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.addWidget(section_label("Target devices"))
        self.targets = QListWidget()
        self.targets.setMinimumWidth(150)
        tv.addWidget(self.targets, 1)
        trow = QHBoxLayout()
        for text, state in (("All", Qt.CheckState.Checked), ("None", Qt.CheckState.Unchecked)):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, s=state: self._check_all(s))
            trow.addWidget(b)
        tv.addLayout(trow)
        top.addWidget(targets)

        # operations menu
        menu = QWidget()
        mv = QVBoxLayout(menu)
        mv.setContentsMargins(0, 0, 0, 0)
        mbar = QHBoxLayout()
        mbar.addWidget(section_label("Operations menu"))
        mbar.addStretch(1)
        for text, slot in (("New", self.new_op), ("Edit", self.edit_op), ("Delete", self.delete_op),
                           ("Import…", self.import_ops), ("Export…", self.export_ops)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            mbar.addWidget(b)
        mv.addLayout(mbar)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Operation", "Sends", "Response rule", "Timeout"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemDoubleClicked.connect(lambda item, _c: self._run_items([item]))
        mv.addWidget(self.tree, 1)
        self.op_detail = _hint("Select operations, then run them. Several selected operations run in order "
                               "on every target device. Double-click runs one immediately.")
        mv.addWidget(self.op_detail)
        rrow = QHBoxLayout()
        self.run_button = QPushButton("▶ Run selected")
        self.run_button.setObjectName("Primary")
        self.run_button.clicked.connect(lambda: self._run_items(self.tree.selectedItems()))
        run_all = QPushButton("Run all in order")
        run_all.clicked.connect(self.run_all)
        self.pending = QLabel("")
        cancel = QPushButton("Cancel queued")
        cancel.clicked.connect(lambda: self.wf.server_id is not None and self.ctx.operations.cancel(self.wf.server_id))
        rrow.addWidget(self.run_button)
        rrow.addWidget(run_all)
        rrow.addStretch(1)
        rrow.addWidget(self.pending)
        rrow.addWidget(cancel)
        mv.addLayout(rrow)
        custom = QHBoxLayout()
        self.custom_format = QComboBox()
        self.custom_format.addItem("ASCII", PayloadFormat.ASCII.value)
        self.custom_format.addItem("HEX", PayloadFormat.HEX.value)
        self.custom_text = QLineEdit()
        self.custom_text.setFont(theme.monospace_font())
        self.custom_text.setPlaceholderText("Custom message (recorded as 'Custom', any reply passes)")
        self.custom_text.returnPressed.connect(self.send_custom)
        send = QPushButton("Send")
        send.clicked.connect(self.send_custom)
        custom.addWidget(self.custom_format)
        custom.addWidget(self.custom_text, 1)
        custom.addWidget(send)
        mv.addLayout(custom)
        top.addWidget(menu)
        top.setSizes([260, 760])
        layout.addWidget(top, 3)

        layout.addWidget(section_label("Results (this session)"))
        self.results_model = RecordTableModel([
            Column("executed_at", "Time", fmt=format_timestamp), Column("device_name", "Device"),
            Column("operation_name", "Operation"),
            Column("status", "Result", fmt=lambda s: s.upper(), color=lambda s: RESULT_COLORS.get(s)),
            Column("latency_ms", "Latency", fmt=lambda v: "-" if v is None else f"{v:.1f} ms"),
            Column("response", "Response (ASCII)", fmt=lambda r: "-" if r is None else to_ascii(bytes(r))),
            Column("response", "Response (HEX)", fmt=lambda r: "-" if r is None else format_hex(bytes(r))),
            Column("message", "Note"),
        ])
        self.results_view = QTableView()
        self.results_view.setModel(self.results_model)
        configure_table(self.results_view)
        self.results_view.setFont(theme.monospace_font(9))
        for col, width in enumerate((100, 140, 170, 80, 80, 220, 260)):
            self.results_view.setColumnWidth(col, width)
        layout.addWidget(self.results_view, 2)

        self.tree.itemSelectionChanged.connect(self._show_detail)
        ctx.operations.result_ready.connect(self._on_result)
        ctx.operations.queue_changed.connect(self._on_queue)
        self.wf.devices_changed.connect(self.refresh_targets)
        self.wf.session_changed.connect(lambda _s: self.reload_results())
        self.wf.database_changed.connect(lambda _p: (self.refresh_ops(), self.reload_results()))
        self._checked: set[str] = set()
        self.refresh_ops()
        self.refresh_targets()

    # -- targets ----------------------------------------------------------
    def refresh_targets(self) -> None:
        if self.targets.count():
            self._checked = set(self.target_peers())
        self.targets.blockSignals(True)
        self.targets.clear()
        for d in self.wf.connected_devices():
            item = QListWidgetItem(f"{d.label}" + ("" if d.label == d.peer else f"   {d.peer}"))
            item.setData(Qt.ItemDataRole.UserRole, d.peer)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if d.peer in self._checked else Qt.CheckState.Unchecked)
            self.targets.addItem(item)
        self.targets.blockSignals(False)
        if self.targets.count() and not self.target_peers() and len(self.wf.connected_devices()) == 1:
            self.targets.item(0).setCheckState(Qt.CheckState.Checked)

    def set_targets(self, peers: list[str]) -> None:
        self._checked = set(peers)
        self.targets.clear()
        self.refresh_targets()

    def target_peers(self) -> list[str]:
        return [self.targets.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.targets.count())
                if self.targets.item(i).checkState() == Qt.CheckState.Checked]

    def _check_all(self, state: Qt.CheckState) -> None:
        for i in range(self.targets.count()):
            self.targets.item(i).setCheckState(state)

    # -- operations menu --------------------------------------------------
    def refresh_ops(self) -> None:
        self.tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        for op in self.wf.list_operations():
            group = groups.get(op.category)
            if group is None:
                group = QTreeWidgetItem(self.tree, [op.category])
                group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                group.setForeground(0, QColor(theme.TEXT_DIM))
                groups[op.category] = group
            try:
                sends = (op.payload_text if op.payload_format is PayloadFormat.ASCII else format_hex(op.payload))
            except ValueError:
                sends = op.payload_text
            rule = op.match_mode.label + (f" “{op.expected_text}”" if op.expected_text and op.expected is not None else "")
            item = QTreeWidgetItem(group, [op.name, sends, rule, f"{op.timeout_ms} ms"])
            item.setData(0, Qt.ItemDataRole.UserRole, op.id)
            item.setToolTip(0, op.description)
        self.tree.expandAll()
        for col in range(4):
            self.tree.resizeColumnToContents(col)
        if not groups:
            hint = QTreeWidgetItem(self.tree, ["No operations yet: click New, or Import… a command set "
                                               "(resources/command_sets/demo_simulator.json for the simulator)"])
            hint.setFlags(Qt.ItemFlag.NoItemFlags)

    def _ops_for(self, items: list[QTreeWidgetItem]) -> list[DeviceOperation]:
        ops = []
        order = {id(it): i for i, it in enumerate(self._all_items())}
        for item in sorted(items, key=lambda it: order.get(id(it), 0)):
            op_id = item.data(0, Qt.ItemDataRole.UserRole)
            op = self.wf.operations.get(op_id) if op_id is not None else None
            if op:
                ops.append(op)
        return ops

    def _all_items(self) -> list[QTreeWidgetItem]:
        items = []
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            items.extend(group.child(j) for j in range(group.childCount()))
        return items

    def _show_detail(self) -> None:
        ops = self._ops_for(self.tree.selectedItems())
        if not ops:
            return
        if len(ops) == 1:
            op = ops[0]
            self.op_detail.setText(f"{op.name}: {op.description or 'no description'} · sends "
                                   f"{format_hex(op.payload[:24])} · {op.match_mode.label.lower()}"
                                   + (f" {format_hex((op.expected or b'')[:24])}" if op.expected else ""))
        else:
            self.op_detail.setText("Runs in this order on each device: " + " → ".join(o.name for o in ops))

    def _run_items(self, items: list[QTreeWidgetItem]) -> None:
        self.run(self.target_peers(), self._ops_for(items))

    def run(self, peers: list[str], ops: list[DeviceOperation]) -> None:
        try:
            self.wf.run_operations(peers, ops)
        except (WorkflowError, ValueError) as exc:
            show_error(self, "Run operations", exc)

    def run_all(self) -> None:
        self.run(self.target_peers(), self.wf.list_operations())

    def send_custom(self) -> None:
        text = self.custom_text.text()
        if not text:
            return
        op = DeviceOperation("Custom", text, PayloadFormat(self.custom_format.currentData()), MatchMode.ANY,
                             category="Custom", timeout_ms=2000)
        try:
            op.validate()
        except ValueError as exc:
            show_error(self, "Custom message", exc)
            return
        self.run(self.target_peers(), [op])

    def new_op(self) -> None:
        self._edit(None)

    def edit_op(self) -> None:
        ops = self._ops_for(self.tree.selectedItems())
        if ops:
            self._edit(ops[0])

    def _edit(self, op: DeviceOperation | None) -> None:
        categories = sorted({o.category for o in self.wf.list_operations()} | {"General"})
        dialog = OperationDialog(op, categories, self)
        if dialog.exec():
            try:
                self.wf.save_operation(dialog.op)
            except (WorkflowError, ValueError) as exc:
                show_error(self, "Save operation", exc)
            self.refresh_ops()

    def delete_op(self) -> None:
        ops = self._ops_for(self.tree.selectedItems())
        if ops and QMessageBox.question(self, "Delete operations", f"Delete {len(ops)} operation(s)?") == QMessageBox.StandardButton.Yes:
            for op in ops:
                self.wf.delete_operation(op.id)
            self.refresh_ops()

    def import_ops(self) -> None:
        from ...core.config import PROJECT_ROOT

        path, _ = QFileDialog.getOpenFileName(self, "Import command set", str(PROJECT_ROOT / "resources" / "command_sets"),
                                              "Command set (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            items = data["operations"] if isinstance(data, dict) else data
            replace = bool(self.wf.list_operations()) and QMessageBox.question(
                self, "Import command set", "Replace the current operations menu?\n(No = merge; same names are updated)"
            ) == QMessageBox.StandardButton.Yes
            count = self.wf.import_operations(items, replace=replace)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            show_error(self, "Import command set", f"{type(exc).__name__}: {exc}")
            return
        self.refresh_ops()
        QMessageBox.information(self, "Import command set", f"Imported {count} operation(s).")

    def export_ops(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export command set", "command_set.json", "Command set (*.json)")
        if path:
            Path(path).write_text(json.dumps({"operations": self.wf.export_operations()}, indent=2), encoding="utf-8")

    # -- results ----------------------------------------------------------
    def reload_results(self) -> None:
        sid = self.wf.session_id
        rows = self.ctx.operations.results.for_session(sid) if sid is not None else []
        self.results_model.set_records(rows)
        self.results_view.scrollToBottom()

    def _on_result(self, row: dict) -> None:
        if row.get("session_id") == self.wf.session_id:
            self.results_model.records.append(row)
            self.results_model.set_records(self.results_model.records)
            self.results_view.scrollToBottom()

    def _on_queue(self, count: int) -> None:
        self.pending.setText(f"{count} queued / running" if count else "idle")
        self.pending.setStyleSheet(f"color: {theme.WARN if count else theme.TEXT_DIM};")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh_targets()
        self.reload_results()


# =============================================================================
# Step 5: report
# =============================================================================
class ReportStep(QWidget):
    def __init__(self, ctx: AppContext, page: WorkflowPage, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.wf = ctx.workflow
        self.page = page
        self._data: dict | None = None
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Session"))
        self.session = QComboBox()
        self.session.setMinimumWidth(320)
        row.addWidget(self.session)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.render)
        row.addWidget(refresh)
        row.addStretch(1)
        self.end_button = QPushButton("■ Finish session")
        self.end_button.setObjectName("Danger")
        self.end_button.clicked.connect(self.finish_session)
        row.addWidget(self.end_button)
        layout.addLayout(row)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        layout.addWidget(self.browser, 1)
        actions = QHBoxLayout()
        for text, slot, name in (("Save HTML…", lambda: self.save("html"), "Primary"),
                                 ("Save Markdown…", lambda: self.save("md"), ""),
                                 ("Save results CSV…", lambda: self.save("csv"), ""),
                                 ("Save all to reports folder", self.save_all, ""),
                                 ("Open in browser", self.open_in_browser, "")):
            b = QPushButton(text)
            b.setObjectName(name)
            b.clicked.connect(slot)
            actions.addWidget(b)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.saved = _hint("")
        layout.addWidget(self.saved)
        self.session.currentIndexChanged.connect(self.render)
        self.wf.session_changed.connect(lambda _s: self.reload_sessions())
        self.wf.database_changed.connect(lambda _p: self.reload_sessions())
        self.reload_sessions()

    def reload_sessions(self) -> None:
        current = self.session.currentData()
        self.session.blockSignals(True)
        self.session.clear()
        for s in self.wf.capture.sessions.list_with_counts():
            running = " (recording)" if s["id"] == self.wf.session_id else ""
            self.session.addItem(f"#{s['id']}  {s['name']}  ·  {format_timestamp(s['started_at'], False)}{running}", s["id"])
        index = self.session.findData(self.wf.session_id if self.wf.session_id is not None else current)
        self.session.setCurrentIndex(max(0, index))
        self.session.blockSignals(False)
        self.end_button.setEnabled(self.wf.session_id is not None)
        self.render()

    def render(self) -> None:
        sid = self.session.currentData()
        if sid is None:
            self._data = None
            self.browser.setHtml("<p style='color:#8b97a7'>No sessions in this database yet.</p>")
            return
        self.ctx.capture.flush()
        try:
            self._data = collect(self.ctx.db, sid)
        except Exception as exc:
            self._data = None
            self.browser.setPlainText(f"Cannot build report: {exc}")
            return
        self.browser.setHtml(render_html(self._data))

    def _content(self, kind: str) -> str:
        assert self._data is not None
        return {"html": render_html, "md": render_markdown, "csv": render_csv}[kind](self._data)

    def _default_name(self, kind: str) -> str:
        s = self._data["session"] if self._data else {"id": 0, "name": "session"}
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in s["name"]).strip("_") or "session"
        suffix = {"html": "html", "md": "md", "csv": "csv"}[kind]
        return f"session_{s['id']}_{safe}{'_results' if kind == 'csv' else ''}.{suffix}"

    def save(self, kind: str) -> None:
        if self._data is None:
            return
        folder = self.ctx.config.resolve(self.ctx.config.reports_dir)
        folder.mkdir(parents=True, exist_ok=True)
        filters = {"html": "HTML (*.html)", "md": "Markdown (*.md)", "csv": "CSV (*.csv)"}
        path, _ = QFileDialog.getSaveFileName(self, "Save report", str(folder / self._default_name(kind)), filters[kind])
        if path:
            Path(path).write_text(self._content(kind), encoding="utf-8")
            self.saved.setText(f"Saved {path}")

    def save_all(self) -> list[Path]:
        if self._data is None:
            return []
        self.render()  # latest data
        folder = self.ctx.config.resolve(self.ctx.config.reports_dir)
        folder.mkdir(parents=True, exist_ok=True)
        written = []
        for kind in ("html", "md", "csv"):
            path = folder / self._default_name(kind)
            path.write_text(self._content(kind), encoding="utf-8")
            written.append(path)
        self.saved.setText("Saved " + ", ".join(str(p) for p in written))
        return written

    def open_in_browser(self) -> None:
        paths = self.save_all()
        if paths:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths[0])))

    def finish_session(self) -> None:
        if self.wf.session_id is None:
            return
        stop = QMessageBox.question(self, "Finish session", "Finish the session and save the report.\n\n"
                                    "Also stop the server (disconnect all devices)?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                    | QMessageBox.StandardButton.Cancel)
        if stop == QMessageBox.StandardButton.Cancel:
            return
        sid = self.wf.end_session()
        if stop == QMessageBox.StandardButton.Yes:
            self.wf.stop_server()
        self.reload_sessions()
        index = self.session.findData(sid)
        if index >= 0:
            self.session.setCurrentIndex(index)
        self.save_all()
        self.page.update_steps()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.reload_sessions()


# =============================================================================
# Page
# =============================================================================
class WorkflowPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.wf = ctx.workflow
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.addWidget(PageHeader("Session Workflow", "Server → database → devices → operations → report."))

        stepper = QFrame()
        stepper.setObjectName("Card")
        srow = QHBoxLayout(stepper)
        srow.setContentsMargins(8, 6, 8, 6)
        self.step_buttons: list[QPushButton] = []
        for i, name in enumerate(STEPS):
            b = QPushButton(f"{i + 1}  {name}")
            b.setCheckable(True)
            b.setMinimumWidth(130)
            b.clicked.connect(lambda _c=False, n=i: self.go_to(n))
            srow.addWidget(b)
            self.step_buttons.append(b)
            if i < len(STEPS) - 1:
                arrow = QLabel("›")
                arrow.setObjectName("Dim")
                srow.addWidget(arrow)
        self.summary = QLabel("")
        self.summary.setObjectName("Dim")
        self.summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.summary.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        srow.addWidget(self.summary, 1)
        layout.addWidget(stepper)

        self.stack = QStackedWidget()
        self.server_step = ServerStep(ctx)
        self.database_step = DatabaseStep(ctx)
        self.devices_step = DevicesStep(ctx, self)
        self.operations_step = OperationsStep(ctx)
        self.report_step = ReportStep(ctx, self)
        for w in (self.server_step, self.database_step, self.devices_step, self.operations_step, self.report_step):
            self.stack.addWidget(w)
        layout.addWidget(self.stack, 1)

        nav = QHBoxLayout()
        self.back = QPushButton("‹ Back")
        self.back.clicked.connect(lambda: self.go_to(self.stack.currentIndex() - 1))
        self.next_hint = QLabel("")
        self.next_hint.setObjectName("Dim")
        self.next = QPushButton("Next ›")
        self.next.setObjectName("Primary")
        self.next.clicked.connect(lambda: self.go_to(self.stack.currentIndex() + 1))
        nav.addWidget(self.back)
        nav.addStretch(1)
        nav.addWidget(self.next_hint)
        nav.addWidget(self.next)
        layout.addLayout(nav)

        for signal in (self.wf.server_state_changed, self.wf.database_changed, self.wf.session_changed,
                       self.wf.devices_changed):
            signal.connect(lambda *_: self.update_steps())
        self.go_to(0)

    def step_done(self, index: int) -> bool:
        return [
            self.wf.server_running,
            self.wf.database_confirmed,
            self.wf.session_id is not None,
            self.wf.session_id is not None,
            True,
        ][index]

    def blocker(self, index: int) -> str:
        """Why step ``index`` cannot be opened yet ("" when it can)."""
        reasons = ["Start the server first.", "Confirm a database first.", "Start a session first.",
                   "Start a session first."]
        for i in range(min(index, 3)):
            if not self.step_done(i):
                return reasons[i]
        return ""

    def go_to(self, index: int) -> None:
        index = max(0, min(len(STEPS) - 1, index))
        reason = self.blocker(index)
        if reason and index > self.stack.currentIndex():
            self.next_hint.setText(reason)
            self.next_hint.setStyleSheet(f"color: {theme.WARN};")
            index = self.stack.currentIndex()
        self.stack.setCurrentIndex(index)
        self.update_steps()

    def update_steps(self) -> None:
        current = self.stack.currentIndex()
        for i, b in enumerate(self.step_buttons):
            done = self.step_done(i) and i < 4
            b.setChecked(i == current)
            locked = bool(self.blocker(i))
            b.setEnabled(not locked or i <= current)
            b.setText(f"{'✓' if done else i + 1}  {STEPS[i]}")
            b.setStyleSheet(f"color: {theme.OK};" if done and i != current else "")
        self.back.setEnabled(current > 0)
        self.next.setVisible(current < len(STEPS) - 1)
        reason = self.blocker(current + 1) if current < len(STEPS) - 1 else ""
        self.next.setEnabled(not reason)
        self.next_hint.setText(reason)
        self.next_hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        parts = []
        if self.wf.server_running:
            parts.append(f"server {self.wf.server_address()}")
        parts.append(f"{len(self.wf.connected_devices())} device(s)")
        if self.wf.session_id is not None:
            parts.append(f"session #{self.wf.session_id}")
        self.summary.setText("  ·  ".join(parts))
