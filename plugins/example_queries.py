"""Example plugin: a page that runs your named SQL queries.

Shows the two extension points of the template:

* ``register(registry)`` adds a page (and could add a start-up hook),
* ``ctx.db.run("<query name>", **params)`` calls a query from ``sql/*.sql``.

Copy this file (or run ``python tools/new_page.py my_page "My Page"``) to
start your own page.  Remove ``"plugins.example_queries"`` from
``project.json`` to drop it.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.widgets.common import PageHeader, configure_table, show_error

QUERIES = [
    ("example.operation_pass_rate", "Pass rate per operation"),
    ("example.failed_operations", "Operations that did not pass"),
    ("example.session_device_traffic", "Traffic per device (session)"),
]


class ExampleQueriesPage(QWidget):
    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.addWidget(PageHeader("My Queries (example plugin)",
                                    "plugins/example_queries.py running queries from sql/example.sql"))
        row = QHBoxLayout()
        self.query = QComboBox()
        for name, title in QUERIES:
            self.query.addItem(title, name)
        self.session = QSpinBox()
        self.session.setRange(1, 1_000_000)
        self.session.setPrefix("session ")
        run = QPushButton("Run")
        run.setObjectName("Primary")
        run.clicked.connect(self.run)
        row.addWidget(self.query)
        row.addWidget(self.session)
        row.addWidget(run)
        row.addStretch(1)
        layout.addLayout(row)
        self.table = QTableWidget()
        configure_table(self.table)
        layout.addWidget(self.table, 1)
        self.status = QLabel("")
        self.status.setObjectName("Dim")
        layout.addWidget(self.status)

    def run(self) -> None:
        name = self.query.currentData()
        query = self.ctx.db.sql.get(name)
        params = {"session_id": self.session.value()} if "session_id" in query.parameters else {}
        try:
            rows = self.ctx.db.run(name, **params)
        except Exception as exc:
            show_error(self, "Query", exc)
            return
        headers = list(rows[0].keys()) if rows else []
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                self.table.setItem(r, c, QTableWidgetItem("NULL" if value is None else str(value)))
        self.table.resizeColumnsToContents()
        self.status.setText(f"{len(rows)} row(s) from {name} ({query.source})")


def register(registry) -> None:
    registry.add_page("example_queries", "My Queries", ExampleQueriesPage,
                      description="Example plugin page using sql/example.sql")
