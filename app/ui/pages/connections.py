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
    """Describe where a connection points.

    Args:
        c: Connection configuration.

    Returns:
        ``host:port`` for clients, or ``bind:local_port`` for servers, with
        ``*`` for any interface.
    """
    if c.transport.value.endswith("client"):
        return f"{c.host}:{c.port}"
    return f"{c.host or '*'}:{c.local_port}"


class ConnectionsPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        """Build the connection table, action buttons and transmit panel.

        The table refreshes on manager signals and every 500 ms while visible.

        Args:
            ctx: Application context.
            parent: Optional parent widget.
        """
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
        self.buttons["new"].clicked.connect(self.new_connection)
        self.buttons["edit"].clicked.connect(self.edit_selected)
        self.buttons["delete"].clicked.connect(self.delete_selected)
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
        self.view.doubleClicked.connect(lambda _i: self.edit_selected())
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
        self.send_button.clicked.connect(self.send_payload)
        actions.addWidget(self.send_button)
        send_layout.addLayout(actions)
        splitter.addWidget(send_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self.payload.textChanged.connect(self._update_preview)
        self.mode_hex.toggled.connect(self._update_preview)
        self.view.selectionModel().selectionChanged.connect(self._update_selection_panel)
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
        """Refresh the table if the page is visible."""
        if self.isVisible():
            self.refresh()

    def refresh(self) -> None:
        """Rebuild the connection table, keeping the current selection."""
        selected = self._selected_connection_id()
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
        self._update_selection_panel()

    def _selected_connection_id(self) -> int | None:
        """Return the id of the selected connection.

        Returns:
            The connection id, or None if nothing is selected.
        """
        model = self.view.selectionModel()
        rows = model.selectedRows() if model else []
        record = self.model.record(rows[0].row()) if rows else None
        return record["id"] if record else None

    def _update_selection_panel(self, *_args) -> None:
        """Update the transmit panel for the selected connection.

        Refreshes the label, state, button enablement and the peer choices.

        Args:
            *_args: Selection signal arguments (unused).
        """
        cid = self._selected_connection_id()
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
    def new_connection(self) -> None:
        """Open the connection dialog for a new connection and save it if accepted."""
        dialog = ConnectionDialog(None, self.ctx.devices.list(), self.ctx.protocols.names(),
                                  (self.ctx.config.default_timeout_ms, self.ctx.config.default_reconnect_interval_ms), self)
        if dialog.exec():
            self._save_connection(dialog.config)

    def edit_selected(self) -> None:
        """Edit the selected connection; an open connection is closed after confirmation."""
        cid = self._selected_connection_id()
        config = self.manager.config(cid) if cid is not None else None
        if config is None:
            return
        if self.manager.endpoint(cid) is not None and QMessageBox.question(
            self, "Edit connection", "The connection is open and will be closed to apply changes. Continue?"
        ) != QMessageBox.StandardButton.Yes:
            return
        dialog = ConnectionDialog(config, self.ctx.devices.list(), self.ctx.protocols.names(), parent=self)
        if dialog.exec():
            self._save_connection(dialog.config)

    def _save_connection(self, config: ConnectionConfig) -> None:
        """Save a connection configuration, showing any error.

        Args:
            config: Configuration edited in the dialog.
        """
        try:
            self.manager.save_config(config)
        except Exception as exc:
            show_error(self, "Save connection", exc)

    def delete_selected(self) -> None:
        """Delete the selected connection after confirmation."""
        cid = self._selected_connection_id()
        if cid is None:
            return
        if QMessageBox.question(self, "Delete connection", "Delete the selected connection?") == QMessageBox.StandardButton.Yes:
            self.manager.delete_config(cid)

    def open_selected(self) -> None:
        """Open the selected connection, showing any error."""
        cid = self._selected_connection_id()
        if cid is not None:
            try:
                self.manager.open(cid)
            except Exception as exc:
                show_error(self, "Open connection", exc)
            self.refresh()

    def close_selected(self) -> None:
        """Close the selected connection."""
        cid = self._selected_connection_id()
        if cid is not None:
            self.manager.close(cid)
            self.refresh()

    def reset_counters(self) -> None:
        """Zero the selected connection's counters and clear its last error."""
        cid = self._selected_connection_id()
        ep = self.manager.endpoint(cid) if cid is not None else None
        if ep:
            ep.reset_counters()
            ep.last_error = ""
            self.refresh()

    def _parse_payload(self) -> bytes:
        """Parse the payload text in the selected HEX or ASCII mode.

        Returns:
            The payload bytes.

        Raises:
            ValueError: If the text is invalid for the mode.
        """
        text = self.payload.toPlainText()
        return parse_hex(text) if self.mode_hex.isChecked() else parse_ascii(text)

    def _update_preview(self) -> None:
        """Show the payload length, or the parse error, under the payload editor."""
        try:
            data = self._parse_payload()
            self.preview.setText(f"{len(data)} bytes")
            self.preview.setStyleSheet("")
        except ValueError as exc:
            self.preview.setText(str(exc))
            self.preview.setStyleSheet(f"color: {theme.ERROR};")

    def send_payload(self) -> None:
        """Send the payload on the selected connection to the chosen peer.

        Errors are shown in a dialog.
        """
        cid = self._selected_connection_id()
        if cid is None:
            show_error(self, "Send", "Select a connection first.")
            return
        try:
            data = self._parse_payload()
        except ValueError as exc:
            show_error(self, "Send", exc)
            return
        if not data:
            return
        if not self.manager.send(cid, data, self.peer.currentData()):
            ep = self.manager.endpoint(cid)
            show_error(self, "Send", ep.last_error if ep else "Connection is not open.")
        self.refresh()
