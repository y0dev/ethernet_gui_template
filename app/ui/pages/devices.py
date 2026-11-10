from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QPushButton, QTableView, QVBoxLayout, QWidget

from ...context import AppContext
from ..dialogs.editors import DeviceDialog
from ..widgets.common import PageHeader, configure_table, show_error
from ..widgets.table_models import Column, RecordTableModel


class DevicesPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        """Build the device table and its add/edit/delete actions.

        Args:
            ctx: Application context.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.ctx = ctx
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        header = PageHeader("Device Manager", "Inventory of target devices and their default connection settings.")
        for text, slot, name in (("Add device", self.add_device, "Primary"), ("Edit", self.edit_selected, ""),
                                 ("Delete", self.delete_selected, "Danger")):
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
        self.view.doubleClicked.connect(lambda _i: self.edit_selected())
        layout.addWidget(self.view, 1)
        self.refresh()

    def refresh(self) -> None:
        """Reload the device table from the device service."""
        self.model.set_records(self.ctx.devices.list())
        self.view.resizeColumnsToContents()

    def _selected_device(self):
        """Return the device in the first selected row.

        Returns:
            The selected device, or None if nothing is selected.
        """
        rows = self.view.selectionModel().selectedRows()
        return self.model.record(rows[0].row()) if rows else None

    def add_device(self) -> None:
        """Open the device dialog for a new device and save it if accepted."""
        dialog = DeviceDialog(None, self.ctx.protocols.names(), self)
        if dialog.exec():
            self._save_device(dialog.device)

    def edit_selected(self) -> None:
        """Open the device dialog for the selected device and save it if accepted."""
        device = self._selected_device()
        if device is None:
            return
        dialog = DeviceDialog(device, self.ctx.protocols.names(), self)
        if dialog.exec():
            self._save_device(dialog.device)

    def _save_device(self, device) -> None:
        """Save a device, showing any error, and reload the table.

        Args:
            device: Device edited in the dialog.
        """
        try:
            self.ctx.devices.save(device)
        except Exception as exc:
            show_error(self, "Save device", exc)
        self.refresh()

    def delete_selected(self) -> None:
        """Delete the selected device after confirmation."""
        device = self._selected_device()
        if device is None:
            return
        if QMessageBox.question(self, "Delete device", f"Delete '{device.name}'?") == QMessageBox.StandardButton.Yes:
            self.ctx.devices.delete(device.id)
            self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802
        """Reload the table whenever the page becomes visible.

        Args:
            event: Qt show event.
        """
        super().showEvent(event)
        self.refresh()
