from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...context import AppContext
from ...core.models import NoteCategory, PortingStatus, ReferenceKind
from ...core.services import PortingRuleError
from ..styles import theme
from ..widgets.common import HexView, PageHeader, configure_table, show_error
from ..widgets.table_models import Column, RecordTableModel, format_timestamp


def _porting_status_color(status) -> str | None:
    """Look up the theme colour for a porting status.

    Args:
        status: ``PortingStatus`` or its string value.

    Returns:
        The colour, or None for an unknown status.
    """
    value = status.value if hasattr(status, "value") else status
    return theme.PORTING_STATUS_COLORS.get(value)


class VerificationDialog(QDialog):
    """Records the outcome of a verification run that was actually executed."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the verification result form with its confirmation checkbox.

        Args:
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.setWindowTitle("Record verification run")
        form = QFormLayout()
        self.reference = QComboBox()
        for kind in ReferenceKind:
            self.reference.addItem(kind.value, kind)
        self.functions = QLineEdit()
        self.functions.setPlaceholderText("comma separated, e.g. crc16_update, frame_encode")
        self.passed = QSpinBox()
        self.passed.setRange(0, 1_000_000)
        self.failed = QSpinBox()
        self.failed.setRange(0, 1_000_000)
        self.binary = QComboBox()
        self.binary.addItem("Not checked", None)
        self.binary.addItem("Byte-for-byte identical", True)
        self.binary.addItem("Differences found", False)
        self.differences = QPlainTextEdit()
        self.risks = QPlainTextEdit()
        self.report_path = QLineEdit()
        for box in (self.differences, self.risks):
            box.setFixedHeight(60)
        form.addRow("Compared against", self.reference)
        form.addRow("Functions tested", self.functions)
        form.addRow("Test cases passed", self.passed)
        form.addRow("Test cases failed", self.failed)
        form.addRow("Binary compatibility", self.binary)
        form.addRow("Behavioral differences", self.differences)
        form.addRow("Remaining risks", self.risks)
        form.addRow("Report / log path", self.report_path)
        self.confirm = QCheckBox("These numbers come from a test run that was executed")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.confirm)
        layout.addWidget(buttons)
        self.resize(520, 0)

    def _validate_and_accept(self) -> None:
        """Close only if the user confirmed that the results come from a real run."""
        if not self.confirm.isChecked():
            show_error(self, "Verification", "Only record results from tests that were actually run.")
            return
        self.accept()


class PortingPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        """Build the project bar, module table and module detail panel.

        Args:
            ctx: Application context.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.ctx = ctx
        self.service = ctx.porting
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        header = PageHeader(
            "C-to-Python Porting Workspace",
            "Traceability from each C source to its Python replacement. A module is only Verified "
            "after a recorded comparison against the original C passes.",
        )
        layout.addWidget(header)

        bar = QHBoxLayout()
        self.project = QComboBox()
        self.project.setMinimumWidth(220)
        bar.addWidget(QLabel("Project"))
        bar.addWidget(self.project)
        for text, slot, name in (("New project", self.new_project, ""), ("Import C sources…", self.import_sources, "Primary"),
                                 ("Add module", self.add_module, ""), ("Delete module", self.delete_module, "Danger"),
                                 ("Status report…", self.export_report, "")):
            b = QPushButton(text)
            b.setObjectName(name)
            b.clicked.connect(slot)
            bar.addWidget(b)
        self.project_info = QLabel("")
        self.project_info.setObjectName("Dim")
        self.project_info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        bar.addWidget(self.project_info, 1)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.modules_model = RecordTableModel([
            Column("c_source_path", "C source"), Column("python_module_path", "Python module", fmt=lambda p: p or "-"),
            Column("status", "Status", color=_porting_status_color),
            Column("test_coverage", "Coverage", fmt=lambda v: "-" if v is None else f"{v:.0f}%"),
        ])
        self.modules_view = QTableView()
        self.modules_view.setModel(self.modules_model)
        configure_table(self.modules_view)
        self.modules_view.setColumnWidth(0, 220)
        self.modules_view.setColumnWidth(1, 200)
        self.modules_view.setColumnWidth(2, 140)
        splitter.addWidget(self.modules_view)
        splitter.addWidget(self._build_detail_panel())
        splitter.setSizes([620, 620])
        layout.addWidget(splitter, 1)

        self.project.currentIndexChanged.connect(self.refresh_modules)
        self.modules_view.selectionModel().currentRowChanged.connect(lambda *_: self.show_module())
        self.reload_projects()

    # -- detail panel -----------------------------------------------------
    def _build_detail_panel(self) -> QWidget:
        """Build the module detail tabs: overview, dependencies, mapping, notes, verification and report.

        Returns:
            The panel widget, disabled until a module is selected.
        """
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 0, 0, 0)
        self.detail_title = QLabel("Select a module")
        self.detail_title.setObjectName("PageTitle")
        layout.addWidget(self.detail_title)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        overview = QWidget()
        form = QFormLayout(overview)
        self.c_path = QLabel("-")
        self.c_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.headers = QLineEdit()
        self.headers.setPlaceholderText("comma separated header paths")
        self.py_path = QLineEdit()
        self.py_path.setPlaceholderText("e.g. app/core/compatibility/crc.py")
        self.coverage = QDoubleSpinBox()
        self.coverage.setRange(-1, 100)
        self.coverage.setSpecialValueText("unknown")
        self.coverage.setSuffix(" %")
        save = QPushButton("Save")
        save.clicked.connect(self.save_module)
        self.status = QComboBox()
        for s in PortingStatus:
            self.status.addItem(s.value, s)
        apply_status = QPushButton("Set status")
        apply_status.setObjectName("Primary")
        apply_status.clicked.connect(self.apply_status)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        status_row.addWidget(apply_status)
        self.verify_hint = QLabel("")
        self.verify_hint.setWordWrap(True)
        form.addRow("C source", self.c_path)
        form.addRow("Header files", self.headers)
        form.addRow("Python replacement", self.py_path)
        form.addRow("Test coverage", self.coverage)
        form.addRow("", save)
        form.addRow("Status", status_row)
        form.addRow("", self.verify_hint)
        self.tabs.addTab(overview, "Overview")

        self.deps_model = RecordTableModel([Column("kind", "Kind"), Column("depends_on", "Depends on")])
        self.tabs.addTab(self._build_table_tab(self.deps_model, "Includes and calls to symbols not defined in this file "
                                         "(from the source scan; review against the real build)."), "Dependencies")

        mappings = QWidget()
        mv = QVBoxLayout(mappings)
        mv.addWidget(QLabel("C symbols found in the source and their Python replacements."))
        self.map_model = RecordTableModel([
            Column("c_kind", "Kind"), Column("c_symbol", "C symbol"),
            Column("python_symbol", "Python replacement", fmt=lambda s: s or "— unmapped —",
                   color=lambda s: None if s else theme.TEXT_DIM),
            Column("notes", "Notes"),
        ])
        self.map_view = QTableView()
        self.map_view.setModel(self.map_model)
        configure_table(self.map_view)
        self.map_view.setFont(theme.monospace_font(9))
        self.map_view.doubleClicked.connect(lambda _i: self.edit_mapping())
        mv.addWidget(self.map_view, 1)
        mrow = QHBoxLayout()
        for text, slot in (("Map selected…", self.edit_mapping), ("Add symbol…", self.add_mapping)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            mrow.addWidget(b)
        mrow.addStretch(1)
        mv.addLayout(mrow)
        self.tabs.addTab(mappings, "Function mapping")

        notes = QWidget()
        nv = QVBoxLayout(notes)
        self.notes_model = RecordTableModel([
            Column("created_at", "Time", fmt=lambda t: format_timestamp(t, False)),
            Column("category", "Category", color=lambda c: theme.WARN if c != "Note" else None),
            Column("body", "Text"),
        ])
        nview = QTableView()
        nview.setModel(self.notes_model)
        configure_table(nview)
        nv.addWidget(nview, 1)
        nrow = QHBoxLayout()
        self.note_category = QComboBox()
        for c in NoteCategory:
            self.note_category.addItem(c.value, c)
        self.note_text = QLineEdit()
        self.note_text.setPlaceholderText("Porting note, compatibility issue or intentional behavioral difference")
        add_note = QPushButton("Add")
        add_note.clicked.connect(self.add_note)
        self.note_text.returnPressed.connect(self.add_note)
        nrow.addWidget(self.note_category)
        nrow.addWidget(self.note_text, 1)
        nrow.addWidget(add_note)
        nv.addLayout(nrow)
        self.tabs.addTab(notes, "Notes && issues")

        verification = QWidget()
        vv = QVBoxLayout(verification)
        self.verif_model = RecordTableModel([
            Column("executed_at", "Time", fmt=lambda t: format_timestamp(t, False)),
            Column("reference_kind", "Reference"),
            Column("passed", "Passed", color=lambda _v: theme.OK),
            Column("failed", "Failed", color=lambda v: theme.ERROR if v else None),
            Column("binary_compatible", "Binary", fmt=lambda b: {True: "identical", False: "DIFFERENT", None: "not checked"}[b],
                   color=lambda b: {True: theme.OK, False: theme.ERROR}.get(b)),
            Column("functions_tested", "Functions", fmt=lambda f: ", ".join(f)),
            Column("remaining_risks", "Remaining risks"),
        ])
        vview = QTableView()
        vview.setModel(self.verif_model)
        configure_table(vview)
        vv.addWidget(vview, 1)
        record = QPushButton("Record verification run…")
        record.clicked.connect(self.record_verification)
        vv.addWidget(record, 0, Qt.AlignmentFlag.AlignLeft)
        self.tabs.addTab(verification, "Verification")

        self.report_view = HexView()
        self.tabs.addTab(self.report_view, "Report")
        self.tabs.setEnabled(False)
        return panel

    def _build_table_tab(self, model: RecordTableModel, caption: str) -> QWidget:
        """Build a tab with a caption above a read-only table.

        Args:
            model: Model shown in the table.
            caption: Explanatory text above the table.

        Returns:
            The tab widget.
        """
        page = QWidget()
        v = QVBoxLayout(page)
        label = QLabel(caption)
        label.setObjectName("Dim")
        label.setWordWrap(True)
        v.addWidget(label)
        view = QTableView()
        view.setModel(model)
        configure_table(view)
        view.setFont(theme.monospace_font(9))
        v.addWidget(view, 1)
        return page

    # -- projects ---------------------------------------------------------
    def _selected_project_id(self) -> int | None:
        """Return the id of the selected porting project.

        Returns:
            The project id, or None if there is no project.
        """
        return self.project.currentData()

    def reload_projects(self, select: int | None = None) -> None:
        """Refill the project list and reload its modules.

        Args:
            select: Project id to select; defaults to the current one.
        """
        current = select if select is not None else self._selected_project_id()
        self.project.blockSignals(True)
        self.project.clear()
        for p in self.service.list_projects():
            self.project.addItem(p["name"], p["id"])
        self.project.setCurrentIndex(max(0, self.project.findData(current)))
        self.project.blockSignals(False)
        self.refresh_modules()

    def new_project(self) -> None:
        """Ask for a name and an optional C source root, then create a project."""
        name, ok = QInputDialog.getText(self, "New porting project", "Project name:")
        if not ok or not name.strip():
            return
        root = QFileDialog.getExistingDirectory(self, "C source root (optional)")
        try:
            pid = self.service.create_project(name, root or "")
        except Exception as exc:
            show_error(self, "New project", exc)
            return
        self.reload_projects(pid)

    def import_sources(self) -> None:
        """Ask for a C source folder and import every ``.c`` file under it.

        The project's C source root is set if it was empty, and a summary is
        shown.
        """
        pid = self._selected_project_id()
        if pid is None:
            show_error(self, "Import", "Create a project first.")
            return
        project = self.service.get_project(pid)
        root = QFileDialog.getExistingDirectory(self, "Select C source directory", project.get("c_source_root") or "")
        if not root:
            return
        try:
            summary = self.service.import_sources(pid, Path(root))
        except Exception as exc:
            show_error(self, "Import", exc)
            return
        if not project.get("c_source_root"):
            self.service.projects.update_row(pid, c_source_root=root)
        message = (f"Added {len(summary.added)} module(s); {len(summary.already_tracked)} already tracked; "
                   f"{summary.headers_found} header(s) found.\n\nModules start as 'Not Analyzed'. The scan is an "
                   "inventory aid only (no preprocessor); review each module before marking it Analyzed.")
        if summary.warnings:
            message += "\n\nScanner warnings:\n" + "\n".join(summary.warnings[:20])
        QMessageBox.information(self, "Import C sources", message)
        self.refresh_modules()

    def add_module(self) -> None:
        """Ask for a C source path and register it as a module."""
        pid = self._selected_project_id()
        if pid is None:
            show_error(self, "Add module", "Create a project first.")
            return
        path, ok = QInputDialog.getText(self, "Add module", "C source path (relative to the project root):")
        if ok and path.strip():
            try:
                self.service.add_module(pid, path.strip())
            except Exception as exc:
                show_error(self, "Add module", exc)
            self.refresh_modules()

    def delete_module(self) -> None:
        """Delete the selected module after confirmation."""
        module = self._selected_module()
        if module and QMessageBox.question(
            self, "Delete module", f"Remove '{module['c_source_path']}' and all of its porting records?"
        ) == QMessageBox.StandardButton.Yes:
            self.service.delete_module(module["id"])
            self.refresh_modules()

    def export_report(self) -> None:
        """Show the Markdown status report and offer to save it."""
        pid = self._selected_project_id()
        if pid is None:
            return
        text = self.service.build_status_report(pid)
        self.report_view.setPlainText(text)
        path, _ = QFileDialog.getSaveFileName(self, "Save porting status report", "porting_report.md", "Markdown (*.md)")
        if path:
            Path(path).write_text(text, encoding="utf-8")

    # -- modules ----------------------------------------------------------
    def refresh_modules(self) -> None:
        """Reload the module table and project summary, keeping the selection."""
        pid = self._selected_project_id()
        selected = self._selected_module()
        modules = self.service.list_modules(pid) if pid is not None else []
        self.modules_model.set_records(modules)
        if pid is not None:
            project = self.service.get_project(pid)
            counts = {}
            for m in modules:
                counts[m["status"]] = counts.get(m["status"], 0) + 1
            verified = counts.get(PortingStatus.VERIFIED, 0)
            self.project_info.setText(f"{len(modules)} modules · {verified} verified · root: {project['c_source_root'] or '-'}")
        else:
            self.project_info.setText("No project")
        if selected:
            for i, m in enumerate(modules):
                if m["id"] == selected["id"]:
                    self.modules_view.selectRow(i)
                    break
        self.show_module()

    def _selected_module(self) -> dict | None:
        """Return the module in the current row.

        Returns:
            The module row, or None if nothing is selected.
        """
        index = self.modules_view.currentIndex() if hasattr(self, "modules_view") else None
        return self.modules_model.record(index.row()) if index is not None and index.isValid() else None

    def show_module(self) -> None:
        """Fill the detail panel from the selected module, or clear it."""
        module = self._selected_module()
        self.tabs.setEnabled(module is not None)
        if module is None:
            self.detail_title.setText("Select a module")
            for model in (self.deps_model, self.map_model, self.notes_model, self.verif_model):
                model.set_records([])
            return
        detail = self.service.module_detail(module["id"])
        self.detail_title.setText(detail["name"])
        self.c_path.setText(detail["c_source_path"])
        self.headers.setText(", ".join(detail["header_paths"]))
        self.py_path.setText(detail["python_module_path"])
        self.coverage.setValue(-1 if detail["test_coverage"] is None else detail["test_coverage"])
        self.status.setCurrentIndex(list(PortingStatus).index(detail["status"]))
        blocker = self.service.verification_blocker(module["id"])
        self.verify_hint.setText(f"Cannot be marked Verified: {blocker}." if blocker else "Eligible for Verified.")
        self.verify_hint.setStyleSheet(f"color: {theme.WARN if blocker else theme.OK};")
        self.deps_model.set_records(detail["dependencies"])
        self.map_model.set_records(detail["mappings"])
        self.notes_model.set_records(detail["notes"])
        self.verif_model.set_records(detail["verifications"])
        self.map_view.resizeColumnsToContents()

    def save_module(self) -> None:
        """Save the edited header paths, Python module path and coverage."""
        module = self._selected_module()
        if module is None:
            return
        headers = [h.strip() for h in self.headers.text().split(",") if h.strip()]
        coverage = None if self.coverage.value() < 0 else self.coverage.value()
        try:
            self.service.update_module(module["id"], python_module_path=self.py_path.text().strip(),
                                       header_paths=headers, test_coverage=coverage)
        except Exception as exc:
            show_error(self, "Save module", exc)
        self.refresh_modules()

    def apply_status(self) -> None:
        """Apply the chosen status; porting rule violations are shown as errors."""
        module = self._selected_module()
        if module is None:
            return
        try:
            self.service.set_status(module["id"], PortingStatus(self.status.currentData()))
        except PortingRuleError as exc:
            show_error(self, "Porting status", exc)
        self.refresh_modules()

    def _selected_mapping(self) -> dict | None:
        """Return the function mapping in the first selected row.

        Returns:
            The mapping row, or None if nothing is selected.
        """
        rows = self.map_view.selectionModel().selectedRows()
        return self.map_model.record(rows[0].row()) if rows else None

    def edit_mapping(self) -> None:
        """Ask for the Python replacement of the selected C symbol and save it."""
        module = self._selected_module()
        mapping = self._selected_mapping()
        if module is None or mapping is None:
            return
        name, ok = QInputDialog.getText(self, "Map symbol", f"Python replacement for {mapping['c_kind']} "
                                        f"{mapping['c_symbol']}:", text=mapping["python_symbol"])
        if ok:
            self.service.set_mapping(module["id"], mapping["c_symbol"], name.strip(), mapping["c_kind"], mapping["notes"])
            self.show_module()

    def add_mapping(self) -> None:
        """Ask for a C symbol name and kind and add it as an unmapped entry."""
        module = self._selected_module()
        if module is None:
            return
        symbol, ok = QInputDialog.getText(self, "Add symbol", "C symbol name:")
        if not ok or not symbol.strip():
            return
        kind, ok = QInputDialog.getItem(self, "Add symbol", "Kind:",
                                        ["function", "struct", "union", "enum", "typedef", "macro",
                                         "function_macro", "global"], 0, False)
        if ok:
            self.service.set_mapping(module["id"], symbol.strip(), "", kind, "added manually")
            self.show_module()

    def add_note(self) -> None:
        """Add the typed note to the selected module."""
        module = self._selected_module()
        if module is None or not self.note_text.text().strip():
            return
        self.service.add_note(module["id"], NoteCategory(self.note_category.currentData()), self.note_text.text())
        self.note_text.clear()
        self.show_module()

    def record_verification(self) -> None:
        """Record a verification run entered in the verification dialog."""
        module = self._selected_module()
        if module is None:
            return
        dialog = VerificationDialog(self)
        if dialog.exec():
            functions = [f.strip() for f in dialog.functions.text().split(",") if f.strip()]
            self.service.record_verification(
                module["id"], ReferenceKind(dialog.reference.currentData()), functions, dialog.passed.value(),
                dialog.failed.value(), dialog.binary.currentData(), dialog.differences.toPlainText(),
                dialog.risks.toPlainText(), dialog.report_path.text(),
            )
            self.refresh_modules()

    def showEvent(self, event) -> None:  # noqa: N802
        """Reload the modules whenever the page becomes visible.

        Args:
            event: Qt show event.
        """
        super().showEvent(event)
        self.refresh_modules()
