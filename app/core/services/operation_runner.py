"""Runs device operations (menu entries) against connected peers.

Each device (connection id + peer address) has its own FIFO queue, so a
sequence of operations runs in order per device while several devices are
served in parallel.  For each operation:

1. the payload is sent to that peer only,
2. the first complete frame *from that peer* within the timeout is the
   response (frames from other devices are ignored),
3. the connection's framing is removed (delimiter / length header) and the
   remaining payload is judged by the operation's match rule and the result is
   stored in ``operation_results`` with the request and response bytes.

Nothing here knows a protocol; the bytes and rules come from the user's
operation definitions.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from ..compatibility.framing import Framer, create_framer
from ..database import Database, OperationResultRepository
from ..hexutil import format_hex
from ..models import DeviceOperation, Direction, MatchMode, Packet
from .connection_manager import ConnectionManager

log = logging.getLogger(__name__)

DeviceKey = tuple[int, str]  # (connection id, peer "host:port")


@dataclass
class _Job:
    operation: DeviceOperation
    device_name: str
    session_id: int
    started: float = 0.0


@dataclass
class _DeviceQueue:
    jobs: deque[_Job] = field(default_factory=deque)
    active: _Job | None = None
    timer: QTimer | None = None


class OperationRunner(QObject):
    #: result row (dict, as stored)
    result_ready = Signal(dict)
    #: number of queued + running operations changed
    queue_changed = Signal(int)

    def __init__(self, db: Database, connections: ConnectionManager, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.results = OperationResultRepository(db)
        self.connections = connections
        self._queues: dict[DeviceKey, _DeviceQueue] = {}
        self._framers: dict[int, Framer] = {}
        connections.packet.connect(self._on_packet)

    # -- public -----------------------------------------------------------
    def enqueue(
        self,
        connection_id: int,
        peer: str,
        operations: list[DeviceOperation],
        session_id: int,
        device_name: str = "",
    ) -> None:
        """Queue ``operations`` (in order) for one device."""
        for op in operations:
            op.validate()
        if not self.pending(connection_id):
            self._framers.pop(connection_id, None)  # pick up framing changes
        queue = self._queues.setdefault((connection_id, peer), _DeviceQueue())
        for op in operations:
            queue.jobs.append(_Job(op, device_name or peer, session_id))
        self._emit_queue()
        self._advance((connection_id, peer))

    def cancel(self, connection_id: int | None = None, peer: str | None = None) -> None:
        """Drop queued work (all devices, one connection, or one device)."""
        for key, queue in list(self._queues.items()):
            if connection_id is not None and key[0] != connection_id:
                continue
            if peer is not None and key[1] != peer:
                continue
            queue.jobs.clear()
            if queue.active is not None:
                self._finish(key, "error", None, "cancelled")
        self._emit_queue()

    def peer_disconnected(self, connection_id: int, peer: str) -> None:
        key = (connection_id, peer)
        queue = self._queues.get(key)
        if queue is None:
            return
        dropped = len(queue.jobs)
        queue.jobs.clear()
        if queue.active is not None:
            self._finish(key, "error", None, "device disconnected")
        if dropped:
            log.warning("queued operations dropped", extra={"context": {"peer": peer, "count": dropped}})
        self._emit_queue()

    def pending(self, connection_id: int | None = None, peer: str | None = None) -> int:
        total = 0
        for key, queue in self._queues.items():
            if (connection_id is None or key[0] == connection_id) and (peer is None or key[1] == peer):
                total += len(queue.jobs) + (queue.active is not None)
        return total

    # -- internals --------------------------------------------------------
    def _emit_queue(self) -> None:
        self.queue_changed.emit(self.pending())

    def _advance(self, key: DeviceKey) -> None:
        queue = self._queues.get(key)
        if queue is None or queue.active is not None or not queue.jobs:
            return
        job = queue.jobs.popleft()
        queue.active = job
        job.started = time.perf_counter()
        connection_id, peer = key
        if not self.connections.send(connection_id, job.operation.payload, peer):
            endpoint = self.connections.endpoint(connection_id)
            reason = endpoint.last_error if endpoint else "connection is not open"
            self._finish(key, "error", None, f"send failed: {reason}")
            return
        if job.operation.match_mode is MatchMode.NONE:
            self._finish(key, "sent", None, "sent (no response expected)")
            return
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda k=key: self._on_timeout(k))
        timer.start(job.operation.timeout_ms)
        queue.timer = timer

    def _on_packet(self, packet: Packet) -> None:
        if packet.direction is not Direction.RX or packet.connection_id is None:
            return
        key = (packet.connection_id, packet.source)
        queue = self._queues.get(key)
        if queue is None or queue.active is None or queue.active.operation.match_mode is MatchMode.NONE:
            return
        op = queue.active.operation
        payload = self._unwrap(packet.connection_id, packet.data)
        if op.judge(payload):
            self._finish(key, "passed", packet.data, "response matches" if op.expected else "response received")
        else:
            self._finish(
                key, "failed", packet.data,
                f"expected {op.match_mode.label.lower()} {format_hex(op.expected or b'')}",
            )

    def _unwrap(self, connection_id: int, frame: bytes) -> bytes:
        """Judge the application payload, not the framing bytes around it."""
        framer = self._framers.get(connection_id)
        if framer is None:
            config = self.connections.config(connection_id)
            try:
                framer = create_framer(config.framing if config else None)
            except ValueError:
                framer = create_framer(None)
            self._framers[connection_id] = framer
        return framer.unwrap(frame)

    def _on_timeout(self, key: DeviceKey) -> None:
        queue = self._queues.get(key)
        if queue is not None and queue.active is not None:
            self._finish(key, "timeout", None, f"no response within {queue.active.operation.timeout_ms} ms")

    def _finish(self, key: DeviceKey, status: str, response: bytes | None, message: str) -> None:
        queue = self._queues[key]
        job = queue.active
        if job is None:
            return
        if queue.timer is not None:
            queue.timer.stop()
            queue.timer.deleteLater()
            queue.timer = None
        queue.active = None
        latency = (time.perf_counter() - job.started) * 1000.0 if status in ("passed", "failed") else None
        row: dict[str, Any] = {
            "session_id": job.session_id, "executed_at": time.time(), "peer": key[1],
            "device_name": job.device_name, "operation_name": job.operation.name,
            "match_mode": job.operation.match_mode.value, "request": job.operation.payload,
            "expected": job.operation.expected, "response": response, "status": status,
            "latency_ms": latency, "message": message,
        }
        try:
            row["id"] = self.results.insert_row(**row)
        except Exception:
            log.exception("failed to store operation result")
        log.info(
            "operation finished",
            extra={"context": {"device": job.device_name, "operation": job.operation.name, "status": status}},
        )
        self.result_ready.emit(row)
        self._emit_queue()
        # Next operation for this device after the current signal unwinds.
        QTimer.singleShot(0, lambda k=key: self._advance(k))
