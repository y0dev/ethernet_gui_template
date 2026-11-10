from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QPushButton, QTableView, QVBoxLayout, QWidget

from ...context import AppContext
from ..dialogs.editors import DeviceDialog
from ..widgets.common import PageHeader, configure_table, show_error
from ..widgets.table_models import Column, RecordTableModel


class DevicesPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        header = PageHeader("Device Manager", "Inventory of target devices and their default connection settings.")
        for text, slot, name in (("Add device", self.add, "Primary"), ("Edit", self.edit, ""),
                                 ("Delete", self.delete, "Danger")):
            button = QPushButton(text)
            button.setObjectName(name)
            button.clicked.connect(slot)
            header.actions.addWidget(button)
        layout.addWidget(header)
        self.model = RecordTableModel([
            Column("name", "Name"), Column("host", "Host"), Column("port", "Port"),
            Column("transport", "Transport", fmt=lambda t: t.label),
            Column("protocol_name", "Protocol", fmt=lambda p: p or "-"),
            Column("description", "Description"),
        ])
        self.view = QTableView()
        self.view.setModel(self.model)
        configure_table(self.view)
        self.view.doubleClicked.connect(lambda _i: self.edit())
        layout.addWidget(self.view, 1)
        self.refresh()

    def refresh(self) -> None:
        self.model.set_records(self.ctx.devices.list())
        self.view.resizeColumnsToContents()

    def _selected(self):
        rows = self.view.selectionModel().selectedRows()
        return self.model.record(rows[0].row()) if rows else None

    def add(self) -> None:
        dialog = DeviceDialog(None, self.ctx.protocols.names(), self)
        if dialog.exec():
            self._save(dialog.device)

    def edit(self) -> None:
        device = self._selected()
        if device is None:
            return
        dialog = DeviceDialog(device, self.ctx.protocols.names(), self)
        if dialog.exec():
            self._save(dialog.device)

    def _save(self, device) -> None:
        try:
            self.ctx.devices.save(device)
        except Exception as exc:
            show_error(self, "Save device", exc)
        self.refresh()

    def delete(self) -> None:
        device = self._selected()
        if device is None:
            return
        if QMessageBox.question(self, "Delete device", f"Delete '{device.name}'?") == QMessageBox.StandardButton.Yes:
            self.ctx.devices.delete(device.id)
            self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh()
