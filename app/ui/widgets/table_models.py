"""Qt models for large tables (model/view; no per-row widgets)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor

from ...core.hexutil import format_hex, to_ascii
from ...core.models import Direction, Packet
from ..styles import theme

Index = QModelIndex | QPersistentModelIndex


def format_timestamp(ts: float | None, with_ms: bool = True) -> str:
    if ts is None:
        return ""
    value = dt.datetime.fromtimestamp(ts)
    return value.strftime("%H:%M:%S.%f")[:-3] if with_ms else value.strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    fmt: Callable[[Any], str] | None = None
    color: Callable[[Any], str | None] | None = None


class RecordTableModel(QAbstractTableModel):
    """Read-only model over a list of dicts or objects."""

    def __init__(self, columns: Sequence[Column], parent=None) -> None:
        super().__init__(parent)
        self.columns = list(columns)
        self.records: list[Any] = []

    def set_records(self, records: Sequence[Any]) -> None:
        self.beginResetModel()
        self.records = list(records)
        self.endResetModel()

    def record(self, row: int) -> Any:
        return self.records[row] if 0 <= row < len(self.records) else None

    def _value(self, record: Any, key: str) -> Any:
        return record.get(key) if isinstance(record, dict) else getattr(record, key, None)

    def rowCount(self, parent: Index = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.records)

    def columnCount(self, parent: Index = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section].header
        return None

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        column = self.columns[index.column()]
        value = self._value(self.records[index.row()], column.key)
        if role == Qt.ItemDataRole.DisplayRole:
            if column.fmt:
                return column.fmt(value)
            if isinstance(value, Enum):
                return str(value.value)
            return "" if value is None else str(value)
        if role == Qt.ItemDataRole.ForegroundRole and column.color:
            color = column.color(value)
            return QColor(color) if color else None
        if role == Qt.ItemDataRole.UserRole:
            return value
        return None


class PacketTableModel(QAbstractTableModel):
    """Append-only packet list optimised for live capture."""

    HEADERS = ("#", "Time", "Dir", "Source", "Destination", "Protocol", "Len", "Data (HEX)", "ASCII")
    PREVIEW_BYTES = 48

    def __init__(self, max_rows: int = 100000, parent=None) -> None:
        super().__init__(parent)
        self.packets: list[Packet] = []
        self.max_rows = max_rows
        self._counter = 0
        self._numbers: list[int] = []

    def append(self, packets: Sequence[Packet]) -> None:
        if not packets:
            return
        overflow = len(self.packets) + len(packets) - self.max_rows
        if overflow > 0:
            overflow = min(overflow, len(self.packets))
            self.beginRemoveRows(QModelIndex(), 0, overflow - 1)
            del self.packets[:overflow]
            del self._numbers[:overflow]
            self.endRemoveRows()
            packets = packets[-self.max_rows:]
        start = len(self.packets)
        self.beginInsertRows(QModelIndex(), start, start + len(packets) - 1)
        for packet in packets:
            self._counter += 1
            self._numbers.append(self._counter)
            self.packets.append(packet)
        self.endInsertRows()

    def set_packets(self, packets: Sequence[Packet]) -> None:
        self.beginResetModel()
        self.packets = list(packets)[-self.max_rows:]
        self._numbers = list(range(1, len(self.packets) + 1))
        self._counter = len(self.packets)
        self.endResetModel()

    def clear(self) -> None:
        self.set_packets([])

    def packet(self, row: int) -> Packet | None:
        return self.packets[row] if 0 <= row < len(self.packets) else None

    def rowCount(self, parent: Index = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.packets)

    def columnCount(self, parent: Index = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        p = self.packets[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return str(self._numbers[index.row()])
            if col == 1:
                return format_timestamp(p.timestamp)
            if col == 2:
                return p.direction.value
            if col == 3:
                return p.source
            if col == 4:
                return p.destination
            if col == 5:
                return p.protocol or "raw"
            if col == 6:
                return str(p.length)
            if col == 7:
                text = format_hex(p.data[:self.PREVIEW_BYTES])
                return text + (" …" if p.length > self.PREVIEW_BYTES else "")
            if col == 8:
                return to_ascii(p.data[:self.PREVIEW_BYTES])
        if role == Qt.ItemDataRole.ForegroundRole and col == 2:
            return QColor(theme.RX_COLOR if p.direction is Direction.RX else theme.TX_COLOR)
        if role == Qt.ItemDataRole.UserRole:
            return p
        return None


class PacketFilterProxy(QSortFilterProxyModel):
    """Filters by direction, connection and a text/hex search."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.direction: Direction | None = None
        self.connection_id: int | None = None
        self.text = ""
        self._hex_needle: bytes | None = None

    def set_filter(self, direction: Direction | str | None, connection_id: int | None, text: str) -> None:
        # QComboBox item data may come back as a plain str for str-based Enums
        if hasattr(self, "beginFilterChange"):  # Qt >= 6.9
            self.beginFilterChange()
        self.direction = Direction(direction) if direction else None
        self.connection_id = connection_id
        self.text = text.strip().lower()
        self._hex_needle = None
        cleaned = self.text.replace(" ", "")
        if cleaned and len(cleaned) % 2 == 0 and all(c in "0123456789abcdef" for c in cleaned):
            self._hex_needle = bytes.fromhex(cleaned)
        if hasattr(self, "endFilterChange"):
            self.endFilterChange()
        else:
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: Index) -> bool:
        model = self.sourceModel()
        packet: Packet | None = model.packet(source_row)  # type: ignore[attr-defined]
        if packet is None:
            return False
        if self.direction is not None and packet.direction is not self.direction:
            return False
        if self.connection_id is not None and packet.connection_id != self.connection_id:
            return False
        if not self.text:
            return True
        if self._hex_needle is not None and self._hex_needle in packet.data:
            return True
        haystack = f"{packet.source} {packet.destination} {packet.protocol} {to_ascii(packet.data)}".lower()
        return self.text in haystack
