from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...context import AppContext
from ...core.hexutil import parse_ascii, parse_hex
from ...core.models import ConnectionConfig
from ..dialogs.editors import ConnectionDialog
from ..styles import theme
from ..widgets.common import PageHeader, StatusDot, configure_table, show_error
from ..widgets.table_models import Column, RecordTableModel


def _describe_target(c: ConnectionConfig) -> str:
    if c.transport.value.endswith("client"):
        return f"{c.host}:{c.port}"
    return f"{c.host or '*'}:{c.local_port}"


class ConnectionsPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.manager = ctx.connections
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        header = PageHeader("Network Connections", "TCP/UDP clients and servers. Multiple connections can be open at once.")
        self.buttons = {}
        for key, text, name in (("new", "New", "Primary"), ("edit", "Edit", ""), ("delete", "Delete", "Danger"),
                                ("open", "Open", ""), ("close", "Close", ""), ("reset", "Reset counters", "")):
            button = QPushButton(text)
            button.setObjectName(name)
            header.actions.addWidget(button)
            self.buttons[key] = button
        self.buttons["new"].clicked.connect(self.new)
        self.buttons["edit"].clicked.connect(self.edit)
        self.buttons["delete"].clicked.connect(self.delete)
        self.buttons["open"].clicked.connect(self.open_selected)
        self.buttons["close"].clicked.connect(self.close_selected)
        self.buttons["reset"].clicked.connect(self.reset_counters)
        layout.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.model = RecordTableModel([
            Column("name", "Name"), Column("transport", "Transport"), Column("target", "Endpoint"),
            Column("framing", "Framing"), Column("protocol", "Protocol"),
            Column("state", "State", color=lambda s: theme.STATE_COLORS.get(s)),
            Column("peers", "Peers"), Column("rx_bytes", "RX bytes"), Column("tx_bytes", "TX bytes"),
            Column("rx_frames", "RX msgs"), Column("tx_frames", "TX msgs"), Column("errors", "Errors"),
            Column("last_error", "Last error", color=lambda e: theme.ERROR if e else None),
        ])
        self.view = QTableView()
        self.view.setModel(self.model)
        configure_table(self.view)
        self.view.doubleClicked.connect(lambda _i: self.edit())
        splitter.addWidget(self.view)

        send_box = QGroupBox("Transmit")
        send_layout = QVBoxLayout(send_box)
        row = QHBoxLayout()
        self.selected_label = QLabel("No connection selected")
        self.selected_label.setObjectName("Dim")
        self.state_dot = StatusDot()
        row.addWidget(self.selected_label)
        row.addWidget(self.state_dot)
        row.addStretch(1)
        self.mode_hex = QRadioButton("HEX")
        self.mode_ascii = QRadioButton("ASCII")
        self.mode_hex.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.mode_hex)
        group.addButton(self.mode_ascii)
        row.addWidget(self.mode_hex)
        row.addWidget(self.mode_ascii)
        send_layout.addLayout(row)
        form = QFormLayout()
        self.peer = QComboBox()
        self.peer.setToolTip("Servers: choose a client, or 'all clients' / 'last sender'")
        form.addRow("Peer", self.peer)
        send_layout.addLayout(form)
        self.payload = QPlainTextEdit()
        self.payload.setFont(theme.monospace_font())
        self.payload.setPlaceholderText("HEX: 01 02 0A FF   |   ASCII: AT\\r\\n  (escapes: \\r \\n \\t \\0 \\xNN)")
        self.payload.setFixedHeight(80)
        send_layout.addWidget(self.payload)
        actions = QHBoxLayout()
        self.preview = QLabel("")
        self.preview.setObjectName("Dim")
        actions.addWidget(self.preview, 1)
        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("Primary")
        self.send_button.clicked.connect(self.send)
        actions.addWidget(self.send_button)
        send_layout.addLayout(actions)
        splitter.addWidget(send_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self.payload.textChanged.connect(self._update_preview)
        self.mode_hex.toggled.connect(self._update_preview)
        self.view.selectionModel().selectionChanged.connect(self._on_selection)
        self.manager.configs_changed.connect(self.refresh)
        self.manager.state_changed.connect(lambda *_: self.refresh())
        self.manager.peers_changed.connect(lambda *_: self.refresh())
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh_if_visible)
        self._timer.start()
        self.refresh()

    # -- table ------------------------------------------------------------
    def _refresh_if_visible(self) -> None:
        if self.isVisible():
            self.refresh()

    def refresh(self) -> None:
        selected = self._selected_id()
        rows = []
        for c in self.manager.configs():
            ep = self.manager.endpoint(c.id)
            rows.append({
                "id": c.id, "name": c.name, "transport": c.transport.label, "target": _describe_target(c),
                "framing": c.framing.get("type", "raw") if c.transport.value.startswith("tcp") else "datagram",
                "protocol": c.protocol_name or "raw", "state": self.manager.state(c.id).value,
                "peers": len(ep.peers()) if ep else 0,
                "rx_bytes": ep.rx_bytes if ep else 0, "tx_bytes": ep.tx_bytes if ep else 0,
                "rx_frames": ep.rx_frames if ep else 0, "tx_frames": ep.tx_frames if ep else 0,
                "errors": ep.error_count if ep else 0, "last_error": ep.last_error if ep else "",
            })
        self.model.set_records(rows)
        if selected is not None:
            for i, r in enumerate(rows):
                if r["id"] == selected:
                    self.view.selectRow(i)
                    break
        self._on_selection()

    def _selected_id(self) -> int | None:
        model = self.view.selectionModel()
        rows = model.selectedRows() if model else []
        record = self.model.record(rows[0].row()) if rows else None
        return record["id"] if record else None

    def _on_selection(self, *_args) -> None:
        cid = self._selected_id()
        config = self.manager.config(cid) if cid is not None else None
        if config is None:
            self.selected_label.setText("No connection selected")
            self.state_dot.set_state("Disconnected")
            self.peer.clear()
            return
        state = self.manager.state(cid)
        self.selected_label.setText(f"{config.name}  ·  {config.transport.label}")
        self.state_dot.set_state(state.value)
        self.buttons["open"].setEnabled(not state.is_active)
        self.buttons["close"].setEnabled(self.manager.endpoint(cid) is not None)
        ep = self.manager.endpoint(cid)
        peers = ep.peers() if ep else []
        current = self.peer.currentData()
        self.peer.blockSignals(True)
        self.peer.clear()
        if config.transport.value == "tcp_server":
            self.peer.addItem("All clients", None)
        elif config.transport.value == "udp_server":
            self.peer.addItem("Last sender", None)
        else:
            self.peer.addItem("Remote endpoint", None)
        if config.transport.value.endswith("server"):
            for p in peers:
                self.peer.addItem(p, p)
        idx = self.peer.findData(current)
        self.peer.setCurrentIndex(max(0, idx))
        self.peer.blockSignals(False)

    # -- actions ----------------------------------------------------------
    def new(self) -> None:
        dialog = ConnectionDialog(None, self.ctx.devices.list(), self.ctx.protocols.names(),
                                  (self.ctx.config.default_timeout_ms, self.ctx.config.default_reconnect_interval_ms), self)
        if dialog.exec():
            self._save(dialog.config)

    def edit(self) -> None:
        cid = self._selected_id()
        config = self.manager.config(cid) if cid is not None else None
        if config is None:
            return
        if self.manager.endpoint(cid) is not None and QMessageBox.question(
            self, "Edit connection", "The connection is open and will be closed to apply changes. Continue?"
        ) != QMessageBox.StandardButton.Yes:
            return
        dialog = ConnectionDialog(config, self.ctx.devices.list(), self.ctx.protocols.names(), parent=self)
        if dialog.exec():
            self._save(dialog.config)

    def _save(self, config: ConnectionConfig) -> None:
        try:
            self.manager.save_config(config)
        except Exception as exc:
            show_error(self, "Save connection", exc)

    def delete(self) -> None:
        cid = self._selected_id()
        if cid is None:
            return
        if QMessageBox.question(self, "Delete connection", "Delete the selected connection?") == QMessageBox.StandardButton.Yes:
            self.manager.delete_config(cid)

    def open_selected(self) -> None:
        cid = self._selected_id()
        if cid is not None:
            try:
                self.manager.open(cid)
            except Exception as exc:
                show_error(self, "Open connection", exc)
            self.refresh()

    def close_selected(self) -> None:
        cid = self._selected_id()
        if cid is not None:
            self.manager.close(cid)
            self.refresh()

    def reset_counters(self) -> None:
        cid = self._selected_id()
        ep = self.manager.endpoint(cid) if cid is not None else None
        if ep:
            ep.reset_counters()
            ep.last_error = ""
            self.refresh()

    def _encode(self) -> bytes:
        text = self.payload.toPlainText()
        return parse_hex(text) if self.mode_hex.isChecked() else parse_ascii(text)

    def _update_preview(self) -> None:
        try:
            data = self._encode()
            self.preview.setText(f"{len(data)} bytes")
            self.preview.setStyleSheet("")
        except ValueError as exc:
            self.preview.setText(str(exc))
            self.preview.setStyleSheet(f"color: {theme.ERROR};")

    def send(self) -> None:
        cid = self._selected_id()
        if cid is None:
            show_error(self, "Send", "Select a connection first.")
            return
        try:
            data = self._encode()
        except ValueError as exc:
            show_error(self, "Send", exc)
            return
        if not data:
            return
        if not self.manager.send(cid, data, self.peer.currentData()):
            ep = self.manager.endpoint(cid)
            show_error(self, "Send", ep.last_error if ep else "Connection is not open.")
        self.refresh()
