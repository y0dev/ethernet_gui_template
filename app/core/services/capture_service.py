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
        """Create the service and start the UI batching timer.

        Args:
            db: Shared database for sessions and packets.
            connections: Source of live packets.
            max_live_packets: Size of the in-memory live buffer.
            flush_interval_ms: Interval between database writes while recording.
            refresh_interval_ms: Interval between ``packets_added`` batches.
            parent: Optional Qt parent.
        """
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
        self._ui_timer.timeout.connect(self._emit_pending_ui_batch)
        self._ui_timer.start()

        self._db_timer = QTimer(self)
        self._db_timer.setInterval(flush_interval_ms)
        self._db_timer.timeout.connect(self.flush)

        connections.packet.connect(self.add_packet)

    # -- intake -----------------------------------------------------------
    def add_packet(self, packet: Packet) -> None:
        """Accept one live packet, queueing it for recording and for the UI.

        Packets are still recorded while the view is paused.

        Args:
            packet: Packet from the connection manager.
        """
        if self._session_id is not None:
            packet.session_id = self._session_id
            self._db_pending.append(packet)
        if not self.paused:
            self.live.append(packet)
            self._ui_pending.append(packet)

    def _emit_pending_ui_batch(self) -> None:
        """Emit the packets collected since the last UI refresh as one batch."""
        if self._ui_pending:
            batch, self._ui_pending = self._ui_pending, []
            self.packets_added.emit(batch)

    def clear(self) -> None:
        """Empty the live buffer and emit ``cleared``. Recorded packets are kept."""
        self.live.clear()
        self._ui_pending.clear()
        self.cleared.emit()

    # -- recording --------------------------------------------------------
    @property
    def session_id(self) -> int | None:
        """Id of the session being recorded, or None when not recording."""
        return self._session_id

    def start_recording(self, name: str, config: dict | None = None) -> int:
        """Start recording packets into a new session.

        Any recording in progress is stopped first.

        Args:
            name: Session name; defaults to ``"Session"`` when empty.
            config: Settings snapshot stored with the session.

        Returns:
            The new session id.
        """
        if self._session_id is not None:
            self.stop_recording()
        self._session_id = self.sessions.start(name or "Session", config=config)
        self._db_timer.start()
        log.info("recording started", extra={"context": {"session_id": self._session_id}})
        self.recording_changed.emit(self._session_id)
        return self._session_id

    def stop_recording(self) -> None:
        """Write pending packets and close the current session, if any."""
        if self._session_id is None:
            return
        self.flush()
        self.sessions.end(self._session_id)
        log.info("recording stopped", extra={"context": {"session_id": self._session_id}})
        self._session_id = None
        self._db_timer.stop()
        self.recording_changed.emit(None)

    def flush(self) -> None:
        """Write pending recorded packets to the database in one batch.

        Failures are logged rather than raised, so capture keeps running.
        """
        if not self._db_pending:
            return
        batch, self._db_pending = self._db_pending, []
        try:
            self.packets.add_many(batch)
        except Exception:
            log.exception("failed to persist captured packets", extra={"context": {"count": len(batch)}})

    def load_session(self, session_id: int) -> list[Packet]:
        """Load every packet recorded in a session.

        Args:
            session_id: Session id.

        Returns:
            The packets in time order.
        """
        return self.packets.list_for_session(session_id)

    # -- replay -----------------------------------------------------------
    def replay(self, packets: Iterable[Packet], connection_id: int) -> int:
        """Re-send the payload of each packet on a connection.

        Frames are sent as captured, which means already framed, so the
        connection's framer wraps them again. Replay is intended for raw-framed
        connections.

        Args:
            packets: Packets to replay, in order.
            connection_id: Open connection to send on.

        Returns:
            The number of packets sent successfully.
        """
        sent = 0
        for packet in packets:
            if self.connections.send(connection_id, packet.data):
                sent += 1
        return sent

    # -- export -----------------------------------------------------------
    @staticmethod
    def export(packets: Iterable[Packet], path: Path) -> int:
        """Export packets to CSV, or to JSON when the path ends in ``.json``.

        Args:
            packets: Packets to export.
            path: Destination file.

        Returns:
            The number of packets written.
        """
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
        """Stop recording and the UI refresh timer."""
        self.stop_recording()
        self._ui_timer.stop()
