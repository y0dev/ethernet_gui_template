"""Test Bench page: two ways of testing, in two tabs.

*Device tests*: saved request/expected-response cases. Each case sends bytes
on an open connection and passes only if the reply frame is byte-for-byte
equal to the expected one (or, with no expected bytes, if nothing needs to
come back). Running is asynchronous: ``ctx.tests`` sends, waits with a timer
and reports through ``case_finished``, so the GUI never blocks.

*Automated suites*: runs the project's pytest suites in a separate process
and streams the output here; results are stored with the device test results.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...context import AppContext
from ...core.hexutil import format_hex, parse_hex
from ..styles import theme
from ..widgets.common import HexView, PageHeader, configure_table, show_error
from ..widgets.table_models import Column, RecordTableModel, format_timestamp

# Colour of the Status column in the results table.
STATUS_COLORS = {"passed": theme.OK, "failed": theme.ERROR, "error": theme.ERROR, "skipped": theme.WARN}


# Dialog for creating or editing one device test case. On Save, the parsed values are left in
# self.values.
class TestCaseDialog(QDialog):
    __test__ = False

    def __init__(self, ctx: AppContext, case: dict | None, parent: QWidget | None = None) -> None:
        """Build the test case form.

        Args:
            ctx: Application context, used to list the connections.
            case: Test case row to edit, or None for a new case.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.setWindowTitle("Edit test case" if case else "New test case")
        self.case = case or {}
        form = QFormLayout()
        self.name = QLineEdit(self.case.get("name", ""))
        self.description = QLineEdit(self.case.get("description", ""))
        self.connection = QComboBox()
        for c in ctx.connections.configs():
            self.connection.addItem(c.name, c.id)
        # Preselect the case's connection, or the first one for a new case.
        self.connection.setCurrentIndex(max(0, self.connection.findData(self.case.get("connection_id"))))
        self.request = QPlainTextEdit(format_hex(bytes(self.case.get("request") or b"")))
        # Stored bytes are shown as hex; None (no expected reply) is shown as an empty box.
        self.expected = QPlainTextEdit(
            format_hex(bytes(self.case["expected_response"])) if self.case.get("expected_response") is not None else ""
        )
        for box in (self.request, self.expected):
            box.setFont(theme.monospace_font())
            box.setFixedHeight(70)
        self.expected.setPlaceholderText("Leave empty when no response is expected")
        self.timeout = QSpinBox()
        self.timeout.setRange(10, 600000)
        self.timeout.setSuffix(" ms")
        self.timeout.setValue(self.case.get("timeout_ms", 2000))
        form.addRow("Name", self.name)
        form.addRow("Description", self.description)
        form.addRow("Connection", self.connection)
        form.addRow("Request (HEX)", self.request)
        form.addRow("Expected response (HEX, exact frame)", self.expected)
        form.addRow("Timeout", self.timeout)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.resize(520, 0)

    def _validate_and_accept(self) -> None:
        """Parse the hex fields into ``self.values`` and close, showing any parse error."""
        try:
            self.values = dict(
                name=self.name.text(), description=self.description.text(),
                connection_id=self.connection.currentData(),
                # Parse here so a typo is reported while the dialog is still open.
                request=parse_hex(self.request.toPlainText()),
                expected=parse_hex(self.expected.toPlainText()) if self.expected.toPlainText().strip() else None,
                timeout_ms=self.timeout.value(),
            )
        except ValueError as exc:
            show_error(self, "Test case", exc)
            return
        self.accept()


class TestBenchPage(QWidget):
    __test__ = False

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        """Build the device-test and pytest tabs and subscribe to runner results.

        Args:
            ctx: Application context.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.ctx = ctx
        # The TestRunner service does the sending, timing, comparing and storing; this page only
        # shows it.
        self.runner = ctx.tests
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.addWidget(PageHeader(
            "Test Bench",
            "Request/response tests against live devices, and the project's automated pytest suites.",
        ))
        tabs = QTabWidget()
        tabs.addTab(self._build_device_tests_tab(), "Device tests")
        tabs.addTab(self._build_suite_tab(), "Automated suites")
        layout.addWidget(tabs, 1)
        # Every finished case or suite refreshes the results table; suite output is appended as it
        # arrives.
        self.runner.case_finished.connect(lambda *_: self.refresh_results())
        self.runner.suite_output.connect(self._append_suite_output)
        self.runner.suite_finished.connect(self._on_suite_finished)
        self.refresh_cases()
        self.refresh_results()

    # -- device tests -----------------------------------------------------
    def _build_device_tests_tab(self) -> QWidget:
        """Build the device tests tab with its actions, case table and result table.

        Returns:
            The tab widget.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        # Toolbar: manage cases, then run the selected ones or all of them.
        for text, slot, name in (("New", self.new_case, "Primary"), ("Edit", self.edit_case, ""),
                                 ("Delete", self.delete_case, "Danger"), ("Run selected", self.run_selected_cases, ""),
                                 ("Run all", self.run_all_cases, "")):
            b = QPushButton(text)
            b.setObjectName(name)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        layout.addLayout(bar)
        # Saved cases on top, recent results (device tests and pytest runs) below.
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.cases_model = RecordTableModel([
            Column("name", "Name"), Column("connection", "Connection"),
            # Long payloads are cut to 80 characters in the table; the dialog shows them in full.
            Column("request", "Request", fmt=lambda b: format_hex(bytes(b or b""))[:80]),
            Column("expected_response", "Expected", fmt=lambda b: "(none)" if b is None else format_hex(bytes(b))[:80]),
            Column("timeout_ms", "Timeout ms"), Column("description", "Description"),
        ])
        self.cases_view = QTableView()
        self.cases_view.setModel(self.cases_model)
        configure_table(self.cases_view)
        self.cases_view.setFont(theme.monospace_font(9))
        self.cases_view.doubleClicked.connect(lambda _i: self.edit_case())
        splitter.addWidget(self.cases_view)
        self.results_model = RecordTableModel([
            Column("executed_at", "Time", fmt=lambda t: format_timestamp(t, False)),
            Column("suite", "Suite"), Column("name", "Test"),
            Column("status", "Status", color=lambda s: STATUS_COLORS.get(s)),
            Column("duration_ms", "ms", fmt=lambda v: f"{v:.1f}"), Column("message", "Message"),
        ])
        self.results_view = QTableView()
        self.results_view.setModel(self.results_model)
        configure_table(self.results_view)
        splitter.addWidget(self.results_view)
        layout.addWidget(splitter, 1)
        return page

    def refresh_cases(self) -> None:
        """Reload the test case table with connection names."""
        # Cases store a connection id; show its name instead.
        names = {c.id: c.name for c in self.ctx.connections.configs()}
        rows = self.runner.cases.list_rows(order_by="name")
        for r in rows:
            r["connection"] = names.get(r["connection_id"], "-")
        self.cases_model.set_records(rows)

    def refresh_results(self) -> None:
        """Reload the 500 most recent results, naming device cases."""
        names = {r["id"]: r["name"] for r in self.runner.cases.list_rows()}
        rows = self.runner.results.recent(500)
        for r in rows:
            # Device test results have no name of their own; use the case name (pytest results
            # already have one).
            if not r["name"] and r["test_case_id"]:
                r["name"] = names.get(r["test_case_id"], f"case {r['test_case_id']}")
        self.results_model.set_records(rows)

    def _selected_case(self) -> dict | None:
        """Return the test case in the first selected row.

        Returns:
            The case row, or None if nothing is selected.
        """
        rows = self.cases_view.selectionModel().selectedRows()
        return self.cases_model.record(rows[0].row()) if rows else None

    def new_case(self) -> None:
        """Open the dialog for a new test case."""
        self._open_case_dialog(None)

    def edit_case(self) -> None:
        """Open the dialog for the selected test case."""
        case = self._selected_case()
        if case:
            self._open_case_dialog(case)

    def _open_case_dialog(self, case: dict | None) -> None:
        """Show the test case dialog and save the case if accepted.

        Args:
            case: Case row to edit, or None for a new case.
        """
        dialog = TestCaseDialog(self.ctx, case, self)
        if dialog.exec():
            v = dialog.values
            try:
                self.runner.save_case(v["name"], v["request"], v["expected"], v["connection_id"],
                                      v["timeout_ms"], v["description"], case["id"] if case else None)
            except Exception as exc:
                show_error(self, "Save test case", exc)
            self.refresh_cases()

    def delete_case(self) -> None:
        """Delete the selected test case."""
        case = self._selected_case()
        if case:
            self.runner.cases.delete(case["id"])
            self.refresh_cases()

    def run_selected_cases(self) -> None:
        """Run every selected test case."""
        for index in self.cases_view.selectionModel().selectedRows():
            # run_case() returns immediately; results arrive later via case_finished. A case that is
            # already running is skipped.
            self.runner.run_case(self.cases_model.record(index.row())["id"])

    def run_all_cases(self) -> None:
        """Run every test case in the table."""
        for record in self.cases_model.records:
            self.runner.run_case(record["id"])

    # -- pytest -----------------------------------------------------------
    def _build_suite_tab(self) -> QWidget:
        """Build the automated suites tab with its target choice and output view.

        Returns:
            The tab widget.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        self.suite_target = QComboBox()
        # Which pytest folder to run.
        for label, target in (("All tests", "tests"), ("Unit", "tests/unit"),
                              ("Integration", "tests/integration"), ("Compatibility (C reference)", "tests/compatibility")):
            self.suite_target.addItem(label, target)
        self.run_suite_button = QPushButton("Run")
        self.run_suite_button.setObjectName("Primary")
        self.run_suite_button.clicked.connect(self.run_suite)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.runner.cancel_suite)
        self.suite_status = QLabel("")
        bar.addWidget(self.suite_target)
        bar.addWidget(self.run_suite_button)
        bar.addWidget(cancel)
        bar.addWidget(self.suite_status, 1)
        layout.addLayout(bar)
        self.output = HexView()
        layout.addWidget(self.output, 1)
        return page

    def run_suite(self) -> None:
        """Start pytest on the chosen target, showing an error if a run is active."""
        self.output.clear()
        try:
            # Starts pytest in a QProcess. Raises RuntimeError if a run is already in progress.
            self.runner.run_pytest(self.suite_target.currentData())
        except RuntimeError as exc:
            show_error(self, "Run tests", exc)
            return
        self.run_suite_button.setEnabled(False)
        self.suite_status.setText("Running…")
        self.suite_status.setStyleSheet("")

    def _append_suite_output(self, text: str) -> None:
        """Append pytest output to the output view.

        Args:
            text: New output text.
        """
        # Append at the end even if the user clicked somewhere else in the output.
        self.output.moveCursor(self.output.textCursor().MoveOperation.End)
        self.output.insertPlainText(text)

    def _on_suite_finished(self, summary) -> None:
        """Show the suite summary, re-enable the run button and reload results.

        Args:
            summary: ``SuiteSummary`` of the finished run.
        """
        self.run_suite_button.setEnabled(True)
        # Green only if nothing failed and pytest itself exited cleanly.
        ok = summary.failed == 0 and summary.errors == 0 and summary.exit_code == 0
        self.suite_status.setText(
            f"exit {summary.exit_code}: {summary.passed} passed, {summary.failed} failed, "
            f"{summary.errors} errors, {summary.skipped} skipped"
        )
        self.suite_status.setStyleSheet(f"color: {theme.OK if ok else theme.ERROR};")
        self.refresh_results()

    def showEvent(self, event) -> None:  # noqa: N802
        """Reload the test cases whenever the page becomes visible.

        Args:
            event: Qt show event.
        """
        super().showEvent(event)
        self.refresh_cases()
