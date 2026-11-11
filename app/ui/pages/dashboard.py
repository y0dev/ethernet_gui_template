"""Dashboard page: a live overview of the whole application.

Eight stat cards across the top (devices, connections, traffic, packets,
recording state), then three tables: every connection with its counters,
C-to-Python porting progress by status, and the most recent warnings and
errors from the log. A one-second timer refreshes everything, but only while
the page is visible, so it costs nothing in the background.
"""

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
        """Build the stat cards and tables and start the one-second refresh timer.

        Args:
            ctx: Application context.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.ctx = ctx
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.addWidget(PageHeader("Dashboard", "Live overview of connections, capture and porting progress."))

        # Top row: stat cards in a 4-column grid, keyed so refresh() can update each by name.
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

        # Lower half: connections table on the left (wider), porting status and recent errors on the
        # right.
        lower = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(section_label("Connections"))
        # One row per configured connection; the State column is coloured by connection state.
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
        # Number of C modules in each porting status (Not started, In progress, Verified, ...).
        self.porting_model = RecordTableModel([
            Column("status", "Status", color=lambda s: theme.PORTING_STATUS_COLORS.get(s)),
            Column("count", "Modules"),
        ])
        pview = QTableView()
        pview.setModel(self.porting_model)
        configure_table(pview)
        right.addWidget(pview)
        right.addWidget(section_label("Recent errors"))
        # Filled by _remember_warning() from the application log.
        self.errors_model = RecordTableModel([
            Column("time", "Time", fmt=format_timestamp), Column("message", "Message"),
        ])
        eview = QTableView()
        eview.setModel(self.errors_model)
        configure_table(eview)
        right.addWidget(eview)
        lower.addLayout(right, 2)
        layout.addLayout(lower, 1)

        # Keep the last 50 warnings/errors. The log emitter calls _remember_warning for every log
        # record, even while the page is hidden.
        self._errors: deque[dict] = deque(maxlen=50)
        ctx.log_emitter.record_logged.connect(self._remember_warning)
        # Poll the services once a second. Counters change with every packet, so polling is cheaper
        # than reacting to each one.
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def _remember_warning(self, entry: dict) -> None:
        """Keep warning and error log records for the recent errors table.

        Args:
            entry: Record dict from the Qt log handler.
        """
        if entry["level"] in ("WARNING", "ERROR", "CRITICAL"):
            self._errors.appendleft({"time": entry["timestamp"], "message": entry["message"] +
                                     (f" ({entry['context'].get('error')})" if entry["context"].get("error") else "")})

    def refresh(self) -> None:
        """Update the cards and tables from the services. Skipped while hidden."""
        # Nothing to draw while another page is shown.
        if not self.isVisible():
            return
        ctx = self.ctx
        configs = ctx.connections.configs()
        rows = []
        # Build the connection rows and add up the totals for the cards in the same pass.
        rx = tx = errors = active = 0
        for c in configs:
            # The endpoint exists only while a connection is open; closed ones show zeros.
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

        # Start every status at 0 so statuses with no modules still appear. Unknown status values in
        # old databases are skipped.
        counts = {s: 0 for s in PortingStatus}
        for row in ctx.db.run("porting.status_counts"):
            try:
                counts[PortingStatus(row["status"])] = row["n"]
            except ValueError:
                pass
        self.porting_model.set_records([{"status": s.value, "count": n} for s, n in counts.items()])
        self.errors_model.set_records(list(self._errors))

    def showEvent(self, event) -> None:  # noqa: N802
        """Refresh the dashboard whenever the page becomes visible.

        Args:
            event: Qt show event.
        """
        super().showEvent(event)
        self.refresh()
