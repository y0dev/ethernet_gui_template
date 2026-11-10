"""Live packet capture, session recording, replay and export.

Packets arrive one by one from the ConnectionManager.  The UI is notified in
batches (``monitor_refresh_interval_ms``) and recorded packets are written to
SQLite in batches (``capture_flush_interval_ms``), so a fast device cannot
flood the event loop with per-packet repaints or per-packet transactions.
"""

from __future__ import annotations

import csv
import json
import logging
from collections import deque
from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from ..database import Database, PacketRepository, SessionRepository
from ..hexutil import format_hex, to_ascii
from ..models import Packet
from .connection_manager import ConnectionManager

log = logging.getLogger(__name__)


class CaptureService(QObject):
    packets_added = Signal(list)          # list[Packet], batched
    cleared = Signal()
    recording_changed = Signal(object)    # session id or None

    def __init__(
        self,
        db: Database,
        connections: ConnectionManager,
        max_live_packets: int = 100000,
        flush_interval_ms: int = 250,
        refresh_interval_ms: int = 100,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.sessions = SessionRepository(db)
        self.packets = PacketRepository(db)
        self.connections = connections
        self.live: deque[Packet] = deque(maxlen=max_live_packets)
        self.paused = False
        self._ui_pending: list[Packet] = []
        self._db_pending: list[Packet] = []
        self._session_id: int | None = None

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(refresh_interval_ms)
        self._ui_timer.timeout.connect(self._flush_ui)
        self._ui_timer.start()

        self._db_timer = QTimer(self)
        self._db_timer.setInterval(flush_interval_ms)
        self._db_timer.timeout.connect(self.flush)

        connections.packet.connect(self.add_packet)

    # -- intake -----------------------------------------------------------
    def add_packet(self, packet: Packet) -> None:
        if self._session_id is not None:
            packet.session_id = self._session_id
            self._db_pending.append(packet)
        if not self.paused:
            self.live.append(packet)
            self._ui_pending.append(packet)

    def _flush_ui(self) -> None:
        if self._ui_pending:
            batch, self._ui_pending = self._ui_pending, []
            self.packets_added.emit(batch)

    def clear(self) -> None:
        self.live.clear()
        self._ui_pending.clear()
        self.cleared.emit()

    # -- recording --------------------------------------------------------
    @property
    def session_id(self) -> int | None:
        return self._session_id

    def start_recording(self, name: str, config: dict | None = None) -> int:
        if self._session_id is not None:
            self.stop_recording()
        self._session_id = self.sessions.start(name or "Session", config=config)
        self._db_timer.start()
        log.info("recording started", extra={"context": {"session_id": self._session_id}})
        self.recording_changed.emit(self._session_id)
        return self._session_id

    def stop_recording(self) -> None:
        if self._session_id is None:
            return
        self.flush()
        self.sessions.end(self._session_id)
        log.info("recording stopped", extra={"context": {"session_id": self._session_id}})
        self._session_id = None
        self._db_timer.stop()
        self.recording_changed.emit(None)

    def flush(self) -> None:
        if not self._db_pending:
            return
        batch, self._db_pending = self._db_pending, []
        try:
            self.packets.add_many(batch)
        except Exception:
            log.exception("failed to persist captured packets", extra={"context": {"count": len(batch)}})

    def load_session(self, session_id: int) -> list[Packet]:
        return self.packets.list_for_session(session_id)

    # -- replay -----------------------------------------------------------
    def replay(self, packets: Iterable[Packet], connection_id: int) -> int:
        """Re-send the payload of each packet on ``connection_id``.

        Frames are sent as captured (already framed), so the connection's
        framer re-wraps them; replay is intended for raw-framed connections.
        Returns the number of packets sent successfully.
        """
        sent = 0
        for packet in packets:
            if self.connections.send(connection_id, packet.data):
                sent += 1
        return sent

    # -- export -----------------------------------------------------------
    @staticmethod
    def export(packets: Iterable[Packet], path: Path) -> int:
        path = Path(path)
        rows = [
            {
                "timestamp": p.timestamp, "direction": p.direction.value,
                "source": p.source, "destination": p.destination,
                "protocol": p.protocol, "length": p.length,
                "hex": format_hex(p.data, ""), "ascii": to_ascii(p.data),
            }
            for p in packets
        ]
        if path.suffix.lower() == ".json":
            path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        else:
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["timestamp", "direction", "source", "destination", "protocol", "length", "hex", "ascii"],
                )
                writer.writeheader()
                writer.writerows(rows)
        log.info("packets exported", extra={"context": {"path": str(path), "count": len(rows)}})
        return len(rows)

    def shutdown(self) -> None:
        self.stop_recording()
        self._ui_timer.stop()
