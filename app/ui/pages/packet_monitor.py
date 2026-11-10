from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...context import AppContext
from ...core.hexutil import hexdump
from ...core.models import Direction, Packet
from ..styles import theme
from ..widgets.common import HexView, PageHeader, configure_table, section_label, show_error
from ..widgets.table_models import PacketFilterProxy, PacketTableModel, format_timestamp


class PacketMonitorPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        """Build the toolbar, filters, packet table and detail panes, and load live packets.

        Args:
            ctx: Application context.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.ctx = ctx
        self.capture = ctx.capture
        self._viewing_session: int | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        header = PageHeader("Packet Monitor", "Every frame sent or received on any connection, as it went over the wire.")
        self.record_button = QPushButton("● Record")
        self.record_button.setCheckable(True)
        self.record_button.toggled.connect(self._toggle_recording)
        self.pause_button = QPushButton("Pause")
        self.pause_button.setCheckable(True)
        self.pause_button.toggled.connect(self._toggle_pause)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear_packets)
        load = QPushButton("Open session…")
        load.clicked.connect(self.open_session)
        live = QPushButton("Live")
        live.clicked.connect(self.show_live)
        export = QPushButton("Export…")
        export.clicked.connect(self.export_packets)
        replay = QPushButton("Replay selected")
        replay.clicked.connect(self.replay_selected)
        for b in (self.record_button, self.pause_button, clear, load, live, export, replay):
            header.actions.addWidget(b)
        layout.addWidget(header)

        filters = QHBoxLayout()
        self.direction = QComboBox()
        self.direction.addItem("RX + TX", None)
        self.direction.addItem("RX only", Direction.RX)
        self.direction.addItem("TX only", Direction.TX)
        self.connection = QComboBox()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search address, protocol, ASCII, or hex bytes (e.g. AA 55)")
        self.autoscroll = QCheckBox("Auto-scroll")
        self.autoscroll.setChecked(True)
        self.decoder = QComboBox()
        self.decoder.setToolTip("Decoder used for the detail pane (default: the connection's protocol)")
        self.source_label = QLabel("Live")
        self.source_label.setObjectName("Dim")
        for w in (QLabel("Direction"), self.direction, QLabel("Connection"), self.connection):
            filters.addWidget(w)
        filters.addWidget(self.search, 1)
        filters.addWidget(QLabel("Decode as"))
        filters.addWidget(self.decoder)
        filters.addWidget(self.autoscroll)
        filters.addWidget(self.source_label)
        layout.addLayout(filters)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.model = PacketTableModel(ctx.config.max_live_packets, self)
        self.proxy = PacketFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.view = QTableView()
        self.view.setModel(self.proxy)
        configure_table(self.view)
        self.view.setFont(theme.monospace_font(9))
        for col, width in enumerate((60, 100, 40, 150, 150, 90, 50, 380)):
            self.view.setColumnWidth(col, width)
        splitter.addWidget(self.view)

        detail = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 6, 0, 0)
        lv.addWidget(section_label("Raw bytes"))
        self.hex = HexView()
        lv.addWidget(self.hex)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 6, 0, 0)
        rv.addWidget(section_label("Decoded fields"))
        self.fields = QTreeWidget()
        self.fields.setHeaderLabels(["Field", "Value", "Offset", "Length", "Note"])
        self.fields.setRootIsDecorated(False)
        self.fields.setFont(theme.monospace_font(9))
        rv.addWidget(self.fields)
        detail.addWidget(left)
        detail.addWidget(right)
        detail.setSizes([500, 400])
        splitter.addWidget(detail)
        splitter.setSizes([450, 260])
        layout.addWidget(splitter, 1)

        self.status = QLabel("")
        self.status.setObjectName("Dim")
        layout.addWidget(self.status)

        self.capture.packets_added.connect(self._append_live_packets)
        self.capture.cleared.connect(self.model.clear)
        self.capture.recording_changed.connect(self._on_recording_changed)
        self.ctx.connections.configs_changed.connect(self._reload_connections)
        self.direction.currentIndexChanged.connect(self._apply_filter)
        self.connection.currentIndexChanged.connect(self._apply_filter)
        self.search.textChanged.connect(self._apply_filter)
        self.decoder.currentIndexChanged.connect(self._show_selected_packet)
        self.view.selectionModel().currentRowChanged.connect(lambda *_: self._show_selected_packet())
        self._reload_connections()
        self._reload_decoders()
        self.model.set_packets(list(self.capture.live))
        self._update_status()

    # -- data flow --------------------------------------------------------
    def _append_live_packets(self, packets: list[Packet]) -> None:
        """Append a batch of live packets unless a recorded session is being viewed.

        Args:
            packets: New packets from the capture service.
        """
        if self._viewing_session is not None:
            return
        self.model.append(packets)
        if self.autoscroll.isChecked() and self.isVisible():
            self.view.scrollToBottom()
        self._update_status()

    def _update_status(self) -> None:
        """Show the shown/total packet counts and the recording state."""
        shown = self.proxy.rowCount()
        total = self.model.rowCount()
        rec = f"  ·  recording session #{self.capture.session_id}" if self.capture.session_id else ""
        self.status.setText(f"{shown:,} shown / {total:,} packets{rec}")

    def _apply_filter(self) -> None:
        """Apply the direction, connection and search filters to the table."""
        self.proxy.set_filter(self.direction.currentData(), self.connection.currentData(), self.search.text())
        self._update_status()

    def _reload_connections(self) -> None:
        """Refill the connection filter from the stored connections, keeping the choice."""
        current = self.connection.currentData()
        self.connection.blockSignals(True)
        self.connection.clear()
        self.connection.addItem("All", None)
        for c in self.ctx.connections.configs():
            self.connection.addItem(c.name, c.id)
        self.connection.setCurrentIndex(max(0, self.connection.findData(current)))
        self.connection.blockSignals(False)

    def _reload_decoders(self) -> None:
        """Refill the decoder choice from the registered protocols, keeping the choice."""
        current = self.decoder.currentData()
        self.decoder.blockSignals(True)
        self.decoder.clear()
        self.decoder.addItem("(connection default)", None)
        self.decoder.addItem("(none)", "")
        for name in self.ctx.protocols.names():
            self.decoder.addItem(name, name)
        self.decoder.setCurrentIndex(max(0, self.decoder.findData(current)))
        self.decoder.blockSignals(False)

    def _selected_packets(self) -> list[Packet]:
        """Return the packets in the selected rows.

        Returns:
            The selected packets in table order.
        """
        rows = sorted({i.row() for i in self.view.selectionModel().selectedRows()})
        return [self.proxy.index(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows]

    def _show_selected_packet(self) -> None:
        """Show the current packet's hex dump and decoded fields.

        The decoder chosen in "Decode as" is used, or the packet's own protocol
        by default. Invalid fields and decode errors are shown in red.
        """
        index = self.view.currentIndex()
        packet: Packet | None = index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None
        self.fields.clear()
        if packet is None:
            self.hex.clear()
            return
        head = (f"{format_timestamp(packet.timestamp)}  {packet.direction.value}  "
                f"{packet.source} -> {packet.destination}  {packet.length} bytes\n\n")
        self.hex.setPlainText(head + hexdump(packet.data))
        choice = self.decoder.currentData()
        protocol = packet.protocol if choice is None else choice
        if not protocol:
            QTreeWidgetItem(self.fields, ["(no decoder)", "Select a decoder or assign one to the connection", "", "", ""])
            return
        message = self.ctx.protocols.decode(packet, protocol)
        if message is None:
            QTreeWidgetItem(self.fields, ["(decoder missing)", protocol, "", "", ""])
            return
        for f in message.fields:
            item = QTreeWidgetItem(self.fields, [f.name, f.text, str(f.offset), str(f.length), f.note])
            if not f.valid:
                for col in range(5):
                    item.setForeground(col, QColor(theme.ERROR))
        for error in message.errors:
            item = QTreeWidgetItem(self.fields, ["error", error, "", "", ""])
            item.setForeground(0, QColor(theme.ERROR))
            item.setForeground(1, QColor(theme.ERROR))
        for col in range(4):
            self.fields.resizeColumnToContents(col)

    # -- actions ----------------------------------------------------------
    def _toggle_recording(self, on: bool) -> None:
        """Start or stop recording when the record button is toggled.

        Starting asks for a session name and un-toggles the button if cancelled.

        Args:
            on: New checked state of the record button.
        """
        if on and self.capture.session_id is None:
            name, ok = QInputDialog.getText(self, "Record session", "Session name:", text="Capture")
            if not ok:
                self.record_button.setChecked(False)
                return
            self.capture.start_recording(name)
        elif not on and self.capture.session_id is not None:
            self.capture.stop_recording()

    def _on_recording_changed(self, session_id) -> None:
        """Sync the record button and status with the recording state.

        Args:
            session_id: Session being recorded, or None when stopped.
        """
        self.record_button.blockSignals(True)
        self.record_button.setChecked(session_id is not None)
        self.record_button.setText("■ Stop recording" if session_id else "● Record")
        self.record_button.setStyleSheet(f"color: {theme.ERROR};" if session_id else "")
        self.record_button.blockSignals(False)
        self._update_status()

    def _toggle_pause(self, on: bool) -> None:
        """Pause or resume adding live packets to the view.

        Args:
            on: True to pause.
        """
        self.capture.paused = on
        self.pause_button.setText("Resume" if on else "Pause")

    def clear_packets(self) -> None:
        """Clear the live buffer, or only the table when viewing a recorded session."""
        if self._viewing_session is None:
            self.capture.clear()
        else:
            self.model.clear()
        self._update_status()

    def open_session(self) -> None:
        """Ask for a recorded session and show its packets instead of live traffic."""
        sessions = self.capture.sessions.list_with_counts()
        if not sessions:
            show_error(self, "Open session", "No recorded sessions yet.")
            return
        labels = [f"#{s['id']}  {s['name']}  ({s['packet_count']} packets, {format_timestamp(s['started_at'], False)})"
                  for s in sessions]
        choice, ok = QInputDialog.getItem(self, "Open session", "Recorded session:", labels, 0, False)
        if not ok:
            return
        session = sessions[labels.index(choice)]
        self._viewing_session = session["id"]
        self.model.set_packets(self.capture.load_session(session["id"]))
        self.source_label.setText(f"Session #{session['id']}")
        self._update_status()

    def show_live(self) -> None:
        """Return the table to live traffic."""
        self._viewing_session = None
        self.model.set_packets(list(self.capture.live))
        self.source_label.setText("Live")
        self._update_status()

    def export_packets(self) -> None:
        """Export the selected packets, or every shown packet, to CSV or JSON."""
        packets = self._selected_packets() or [
            self.proxy.index(r, 0).data(Qt.ItemDataRole.UserRole) for r in range(self.proxy.rowCount())
        ]
        if not packets:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export packets", "packets.csv", "CSV (*.csv);;JSON (*.json)")
        if path:
            try:
                count = self.capture.export(packets, Path(path))
                self.status.setText(f"Exported {count} packets to {path}")
            except OSError as exc:
                show_error(self, "Export", exc)

    def replay_selected(self) -> None:
        """Re-send the selected packets on an open connection chosen by the user."""
        packets = self._selected_packets()
        if not packets:
            show_error(self, "Replay", "Select one or more packets to replay.")
            return
        open_ids = [c for c in self.ctx.connections.configs() if self.ctx.connections.endpoint(c.id)]
        if not open_ids:
            show_error(self, "Replay", "Open a connection first.")
            return
        names = [c.name for c in open_ids]
        choice, ok = QInputDialog.getItem(
            self, "Replay", "Send the selected frames on (frames are re-sent as captured;\n"
            "use a raw-framed connection to avoid framing them twice):", names, 0, False)
        if ok:
            sent = self.capture.replay(packets, open_ids[names.index(choice)].id)
            self.status.setText(f"Replayed {sent} of {len(packets)} packets")

    def showEvent(self, event) -> None:  # noqa: N802
        """Reload the decoder list whenever the page becomes visible.

        Args:
            event: Qt show event.
        """
        super().showEvent(event)
        self._reload_decoders()
