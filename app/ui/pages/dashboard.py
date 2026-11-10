from __future__ import annotations

from collections import deque

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QTableView, QVBoxLayout, QWidget

from ...context import AppContext
from ...core.models import PortingStatus
from ..styles import theme
from ..widgets.common import PageHeader, StatCard, configure_table, section_label
from ..widgets.table_models import Column, RecordTableModel, format_timestamp


class DashboardPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.addWidget(PageHeader("Dashboard", "Live overview of connections, capture and porting progress."))

        grid = QGridLayout()
        grid.setSpacing(10)
        self.cards = {
            key: StatCard(label)
            for key, label in [
                ("devices", "Devices"), ("connections", "Connections"), ("active", "Active"),
                ("errors", "Connection errors"), ("rx", "RX bytes"), ("tx", "TX bytes"),
                ("packets", "Packets in monitor"), ("recording", "Recording"),
            ]
        }
        for i, card in enumerate(self.cards.values()):
            grid.addWidget(card, i // 4, i % 4)
        layout.addLayout(grid)

        lower = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(section_label("Connections"))
        self.conn_model = RecordTableModel([
            Column("name", "Name"), Column("transport", "Transport"),
            Column("state", "State", color=lambda s: theme.STATE_COLORS.get(s)),
            Column("rx", "RX bytes"), Column("tx", "TX bytes"), Column("errors", "Errors"),
        ])
        view = QTableView()
        view.setModel(self.conn_model)
        configure_table(view)
        left.addWidget(view)
        lower.addLayout(left, 3)

        right = QVBoxLayout()
        right.addWidget(section_label("Porting status"))
        self.porting_model = RecordTableModel([
            Column("status", "Status", color=lambda s: theme.PORTING_STATUS_COLORS.get(s)),
            Column("count", "Modules"),
        ])
        pview = QTableView()
        pview.setModel(self.porting_model)
        configure_table(pview)
        right.addWidget(pview)
        right.addWidget(section_label("Recent errors"))
        self.errors_model = RecordTableModel([
            Column("time", "Time", fmt=format_timestamp), Column("message", "Message"),
        ])
        eview = QTableView()
        eview.setModel(self.errors_model)
        configure_table(eview)
        right.addWidget(eview)
        lower.addLayout(right, 2)
        layout.addLayout(lower, 1)

        self._errors: deque[dict] = deque(maxlen=50)
        ctx.log_emitter.record_logged.connect(self._on_log)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def _on_log(self, entry: dict) -> None:
        if entry["level"] in ("WARNING", "ERROR", "CRITICAL"):
            self._errors.appendleft({"time": entry["timestamp"], "message": entry["message"] +
                                     (f" ({entry['context'].get('error')})" if entry["context"].get("error") else "")})

    def refresh(self) -> None:
        if not self.isVisible():
            return
        ctx = self.ctx
        configs = ctx.connections.configs()
        rows = []
        rx = tx = errors = active = 0
        for c in configs:
            ep = ctx.connections.endpoint(c.id)
            state = ctx.connections.state(c.id)
            active += state.is_active
            row = {"name": c.name, "transport": c.transport.label, "state": state.value,
                   "rx": ep.rx_bytes if ep else 0, "tx": ep.tx_bytes if ep else 0,
                   "errors": ep.error_count if ep else 0}
            rx += row["rx"]
            tx += row["tx"]
            errors += row["errors"]
            rows.append(row)
        self.conn_model.set_records(rows)
        self.cards["devices"].set_value(str(len(ctx.devices.list())))
        self.cards["connections"].set_value(str(len(configs)))
        self.cards["active"].set_value(str(active), theme.OK if active else None)
        self.cards["errors"].set_value(str(errors), theme.ERROR if errors else None)
        self.cards["rx"].set_value(f"{rx:,}")
        self.cards["tx"].set_value(f"{tx:,}")
        self.cards["packets"].set_value(f"{len(ctx.capture.live):,}")
        sid = ctx.capture.session_id
        self.cards["recording"].set_value(f"#{sid}" if sid else "Off", theme.ERROR if sid else None)

        counts = {s: 0 for s in PortingStatus}
        for row in ctx.db.run("porting.status_counts"):
            try:
                counts[PortingStatus(row["status"])] = row["n"]
            except ValueError:
                pass
        self.porting_model.set_records([{"status": s.value, "count": n} for s, n in counts.items()])
        self.errors_model.set_records(list(self._errors))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh()
