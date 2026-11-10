"""Editor dialogs for devices and connections."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.compatibility.framing import create_framer
from ...core.models import ConnectionConfig, Device, Transport
from ..widgets.common import show_error


def _port_box(value: int) -> QSpinBox:
    box = QSpinBox()
    box.setRange(0, 65535)
    box.setValue(value)
    return box


class DeviceDialog(QDialog):
    def __init__(self, device: Device | None, protocols: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Device" if device else "Add Device")
        self.device = device or Device(name="")
        form = QFormLayout()
        self.name = QLineEdit(self.device.name)
        self.host = QLineEdit(self.device.host)
        self.port = _port_box(self.device.port)
        self.transport = QComboBox()
        for t in Transport:
            self.transport.addItem(t.label, t)
        self.transport.setCurrentIndex(list(Transport).index(self.device.transport))
        self.protocol = QComboBox()
        self.protocol.addItem("(none)", "")
        for name in protocols:
            self.protocol.addItem(name, name)
        idx = self.protocol.findData(self.device.protocol_name)
        self.protocol.setCurrentIndex(max(0, idx))
        self.description = QLineEdit(self.device.description)
        self.notes = QPlainTextEdit(self.device.notes)
        self.notes.setFixedHeight(80)
        form.addRow("Name", self.name)
        form.addRow("Host / IP", self.host)
        form.addRow("Port", self.port)
        form.addRow("Transport", self.transport)
        form.addRow("Protocol", self.protocol)
        form.addRow("Description", self.description)
        form.addRow("Notes", self.notes)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.resize(420, 0)

    def _accept(self) -> None:
        if not self.name.text().strip():
            show_error(self, "Device", "Name is required.")
            return
        d = self.device
        d.name = self.name.text().strip()
        d.host = self.host.text().strip()
        d.port = self.port.value()
        d.transport = Transport(self.transport.currentData())
        d.protocol_name = self.protocol.currentData()
        d.description = self.description.text()
        d.notes = self.notes.toPlainText()
        self.accept()


class FramingEditor(QWidget):
    """Edits the framing configuration dict used by ``create_framer``."""

    TYPES = [("raw", "Raw (no framing)"), ("delimiter", "Delimiter"),
             ("fixed", "Fixed length"), ("length_prefixed", "Length prefixed")]

    def __init__(self, framing: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = QComboBox()
        for key, label in self.TYPES:
            self.kind.addItem(label, key)
        self.stack = QStackedWidget()
        self.stack.addWidget(QWidget())

        delim = QWidget()
        f = QFormLayout(delim)
        self.delimiter = QLineEdit(framing.get("delimiter_hex", "0d0a"))
        self.delimiter.setPlaceholderText("hex, e.g. 0d0a")
        self.delim_max = QSpinBox()
        self.delim_max.setRange(1, 16 * 1024 * 1024)
        self.delim_max.setValue(int(framing.get("max_frame_length", 65536)))
        f.addRow("Delimiter (hex)", self.delimiter)
        f.addRow("Max frame", self.delim_max)
        self.stack.addWidget(delim)

        fixed = QWidget()
        f = QFormLayout(fixed)
        self.fixed_len = QSpinBox()
        self.fixed_len.setRange(1, 16 * 1024 * 1024)
        self.fixed_len.setValue(int(framing.get("length", 16)))
        f.addRow("Frame length", self.fixed_len)
        self.stack.addWidget(fixed)

        lp = QWidget()
        f = QFormLayout(lp)
        self.len_size = QComboBox()
        for size in (1, 2, 4):
            self.len_size.addItem(f"{size} byte(s)", size)
        self.len_size.setCurrentIndex(max(0, self.len_size.findData(int(framing.get("length_size", 2)))))
        self.len_order = QComboBox()
        self.len_order.addItem("Big endian", "big")
        self.len_order.addItem("Little endian", "little")
        self.len_order.setCurrentIndex(max(0, self.len_order.findData(framing.get("byte_order", "big"))))
        self.len_offset = QSpinBox()
        self.len_offset.setRange(0, 1024)
        self.len_offset.setValue(int(framing.get("length_offset", 0)))
        self.len_adjust = QSpinBox()
        self.len_adjust.setRange(-1024, 1024)
        self.len_adjust.setValue(int(framing.get("length_adjust", 0)))
        self.len_adjust.setToolTip(
            "Frame size = header + length value + adjust.\n"
            "Field counts the whole frame: adjust = -(offset + size).\n"
            "Uncounted trailer of N bytes (e.g. CRC): adjust = +N."
        )
        self.lp_max = QSpinBox()
        self.lp_max.setRange(1, 16 * 1024 * 1024)
        self.lp_max.setValue(int(framing.get("max_frame_length", 65536)))
        f.addRow("Length field size", self.len_size)
        f.addRow("Byte order", self.len_order)
        f.addRow("Field offset", self.len_offset)
        f.addRow("Length adjust", self.len_adjust)
        f.addRow("Max frame", self.lp_max)
        self.stack.addWidget(lp)

        self.kind.currentIndexChanged.connect(self.stack.setCurrentIndex)
        self.kind.setCurrentIndex(max(0, self.kind.findData(framing.get("type", "raw"))))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.kind)
        layout.addWidget(self.stack)

    def value(self) -> dict:
        kind = self.kind.currentData()
        if kind == "delimiter":
            return {"type": kind, "delimiter_hex": self.delimiter.text().replace(" ", ""),
                    "max_frame_length": self.delim_max.value()}
        if kind == "fixed":
            return {"type": kind, "length": self.fixed_len.value()}
        if kind == "length_prefixed":
            return {"type": kind, "length_size": self.len_size.currentData(),
                    "byte_order": self.len_order.currentData(), "length_offset": self.len_offset.value(),
                    "length_adjust": self.len_adjust.value(), "max_frame_length": self.lp_max.value()}
        return {"type": "raw"}


class ConnectionDialog(QDialog):
    def __init__(
        self,
        config: ConnectionConfig | None,
        devices: list[Device],
        protocols: list[str],
        defaults: tuple[int, int] = (5000, 3000),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Connection" if config else "New Connection")
        self.config = config or ConnectionConfig(
            name="", transport=Transport.TCP_CLIENT, host="127.0.0.1",
            timeout_ms=defaults[0], reconnect_interval_ms=defaults[1],
        )
        c = self.config
        form = QFormLayout()
        self.name = QLineEdit(c.name)
        self.transport = QComboBox()
        for t in Transport:
            self.transport.addItem(t.label, t)
        self.transport.setCurrentIndex(list(Transport).index(c.transport))
        self.host = QLineEdit(c.host)
        self.port = _port_box(c.port)
        self.local_port = _port_box(c.local_port)
        self.device = QComboBox()
        self.device.addItem("(none)", None)
        for d in devices:
            self.device.addItem(d.name, d.id)
        self.device.setCurrentIndex(max(0, self.device.findData(c.device_id)))
        self.protocol = QComboBox()
        self.protocol.addItem("(raw)", "")
        for name in protocols:
            self.protocol.addItem(name, name)
        self.protocol.setCurrentIndex(max(0, self.protocol.findData(c.protocol_name)))
        self.framing = FramingEditor(c.framing)
        self.timeout = QSpinBox()
        self.timeout.setRange(100, 600000)
        self.timeout.setSuffix(" ms")
        self.timeout.setValue(c.timeout_ms)
        self.reconnect = QCheckBox("Reconnect automatically")
        self.reconnect.setChecked(c.auto_reconnect)
        self.reconnect_interval = QSpinBox()
        self.reconnect_interval.setRange(100, 600000)
        self.reconnect_interval.setSuffix(" ms")
        self.reconnect_interval.setValue(c.reconnect_interval_ms)

        form.addRow("Name", self.name)
        form.addRow("Transport", self.transport)
        form.addRow("Device", self.device)
        self.host_label = "Remote host / IP"
        form.addRow(self.host_label, self.host)
        form.addRow("Remote port", self.port)
        form.addRow("Local port", self.local_port)
        form.addRow("Protocol decoder", self.protocol)
        form.addRow("Stream framing", self.framing)
        form.addRow("Connect timeout", self.timeout)
        form.addRow("", self.reconnect)
        form.addRow("Reconnect interval", self.reconnect_interval)
        self._form = form
        self.transport.currentIndexChanged.connect(self._update_fields)
        self.device.currentIndexChanged.connect(self._apply_device)
        self._devices = {d.id: d for d in devices}
        self._update_fields()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.resize(460, 0)

    def _apply_device(self) -> None:
        device = self._devices.get(self.device.currentData())
        if device:
            self.host.setText(device.host)
            self.port.setValue(device.port)
            self.transport.setCurrentIndex(list(Transport).index(device.transport))
            idx = self.protocol.findData(device.protocol_name)
            if idx >= 0:
                self.protocol.setCurrentIndex(idx)

    def _update_fields(self) -> None:
        t = Transport(self.transport.currentData())
        is_client = t in (Transport.TCP_CLIENT, Transport.UDP_CLIENT)
        is_tcp = t in (Transport.TCP_CLIENT, Transport.TCP_SERVER)
        label = self._form.labelForField(self.host)
        if label is not None:
            label.setText("Remote host / IP" if is_client else "Bind address (blank = any)")
        for widget, visible in (
            (self.port, is_client), (self.local_port, t is not Transport.TCP_CLIENT),
            (self.framing, is_tcp), (self.timeout, t is Transport.TCP_CLIENT),
            (self.reconnect, t is Transport.TCP_CLIENT), (self.reconnect_interval, t is Transport.TCP_CLIENT),
        ):
            widget.setVisible(visible)
            lbl = self._form.labelForField(widget)
            if lbl is not None:
                lbl.setVisible(visible)

    def _accept(self) -> None:
        framing = self.framing.value()
        try:
            create_framer(framing)
        except ValueError as exc:
            show_error(self, "Framing", exc)
            return
        c = self.config
        c.name = self.name.text().strip()
        c.transport = Transport(self.transport.currentData())
        c.host = self.host.text().strip()
        c.port = self.port.value()
        c.local_port = self.local_port.value()
        c.device_id = self.device.currentData()
        c.protocol_name = self.protocol.currentData()
        c.framing = framing if c.transport in (Transport.TCP_CLIENT, Transport.TCP_SERVER) else {"type": "raw"}
        c.timeout_ms = self.timeout.value()
        c.auto_reconnect = self.reconnect.isChecked()
        c.reconnect_interval_ms = self.reconnect_interval.value()
        if not c.name:
            show_error(self, "Connection", "Name is required.")
            return
        self.accept()
