"""Porting Workspace service: traceability between C sources and Python modules.

Status rules enforced here (section 8/9 of the project brief):

* The scanner never sets a status; a newly imported module is "Not Analyzed".
* "Verified" can only be set when the latest recorded verification run
  - compared against the original C (C harness or golden vectors),
  - ran at least one test, had zero failures, and
  - did not report a binary incompatibility.
  Python code that merely runs is never enough.
* Recording a failing verification moves a "Verified" module back to
  "Verification Required".
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..database import Database, PortingModuleRepository, PortingProjectRepository
from ..models import NoteCategory, PortingStatus, ReferenceKind
from ..porting import discover_sources, render_report, scan_file

log = logging.getLogger(__name__)


class PortingRuleError(ValueError):
    """An action would break a porting workflow rule."""


@dataclass
class ImportSummary:
    added: list[str]
    already_tracked: list[str]
    headers_found: int
    warnings: list[str]


class PortingService:
    def __init__(self, db: Database) -> None:
        """Create the service on top of the porting repositories.

        Args:
            db: Shared database.
        """
        self.db = db
        self.projects = PortingProjectRepository(db)
        self.modules = PortingModuleRepository(db)

    # -- projects ---------------------------------------------------------
    def create_project(
        self, name: str, c_source_root: str = "", python_target_root: str = "", description: str = ""
    ) -> int:
        """Create a porting project.

        Args:
            name: Project name.
            c_source_root: Folder of the original C sources.
            python_target_root: Folder of the Python port.
            description: Free-text description.

        Returns:
            The new project id.

        Raises:
            ValueError: If the name is blank.
        """
        if not name.strip():
            raise ValueError("project name is required")
        return self.projects.insert_row(
            name=name.strip(), description=description,
            c_source_root=c_source_root, python_target_root=python_target_root,
        )

    def list_projects(self) -> list[dict[str, Any]]:
        """List every porting project.

        Returns:
            Project rows ordered by name.
        """
        return self.projects.list_rows(order_by="name")

    def get_project(self, project_id: int) -> dict[str, Any] | None:
        """Fetch one porting project.

        Args:
            project_id: Project id.

        Returns:
            The project row, or None if it does not exist.
        """
        return self.projects.get_row(project_id)

    def delete_project(self, project_id: int) -> None:
        """Delete a porting project.

        Args:
            project_id: Project id.
        """
        self.projects.delete(project_id)

    # -- modules ----------------------------------------------------------
    def add_module(
        self,
        project_id: int,
        c_source_path: str,
        name: str = "",
        header_paths: Sequence[str] = (),
        python_module_path: str = "",
    ) -> int:
        """Register a C source file as a module with status "Not Analyzed".

        Args:
            project_id: Project id.
            c_source_path: Path of the C file; required for traceability.
            name: Module name; defaults to the file stem.
            header_paths: Headers that belong to the module.
            python_module_path: Path of the Python replacement, if known.

        Returns:
            The new module id.

        Raises:
            ValueError: If ``c_source_path`` is blank.
        """
        import json

        if not c_source_path.strip():
            raise ValueError("C source path is required (traceability)")
        return self.modules.insert_row(
            project_id=project_id,
            name=name or Path(c_source_path).stem,
            c_source_path=c_source_path,
            header_paths_json=json.dumps(list(header_paths)),
            python_module_path=python_module_path,
            status=PortingStatus.NOT_ANALYZED.value,
        )

    def update_module(self, module_id: int, **fields: Any) -> None:
        """Update a module's editable fields and refresh ``updated_at``.

        Status changes go through :meth:`set_status` instead.

        Args:
            module_id: Module id.
            **fields: Any of ``name``, ``python_module_path``, ``test_coverage``
                (0-100) and ``header_paths``.

        Raises:
            KeyError: If any other field is given.
            ValueError: If ``test_coverage`` is out of range.
        """
        import json

        allowed = {"name", "python_module_path", "test_coverage", "header_paths"}
        unknown = set(fields) - allowed
        if unknown:
            raise KeyError(f"cannot update {sorted(unknown)} here")
        if "header_paths" in fields:
            fields["header_paths_json"] = json.dumps(list(fields.pop("header_paths")))
        coverage = fields.get("test_coverage")
        if coverage is not None and not 0 <= coverage <= 100:
            raise ValueError("test coverage must be between 0 and 100")
        self.modules.update_row(module_id, **fields)
        self.db.execute("UPDATE porting_modules SET updated_at = datetime('now') WHERE id = ?", (module_id,))

    def list_modules(self, project_id: int) -> list[dict[str, Any]]:
        """List a project's modules.

        Args:
            project_id: Project id.

        Returns:
            Module rows ordered by C source path.
        """
        return self.modules.list_for_project(project_id)

    def get_module(self, module_id: int) -> dict[str, Any] | None:
        """Fetch one module.

        Args:
            module_id: Module id.

        Returns:
            The module row, or None if it does not exist.
        """
        return self.modules.get(module_id)

    def delete_module(self, module_id: int) -> None:
        """Delete a module.

        Args:
            module_id: Module id.
        """
        self.modules.delete(module_id)

    def import_sources(self, project_id: int, root: Path) -> ImportSummary:
        """Register every ``.c`` file under a folder and record its inventory.

        For each new source, local includes become dependencies, matching
        headers are attached, and every top-level symbol becomes an unmapped
        entry in the function mapping table. Statuses stay "Not Analyzed".
        Sources that are already tracked are skipped.

        Args:
            project_id: Project id.
            root: Folder to scan recursively.

        Returns:
            What was added, skipped and warned about.

        Raises:
            FileNotFoundError: If ``root`` is not a folder.
        """
        root = Path(root)
        if not root.is_dir():
            raise FileNotFoundError(f"{root} is not a directory")
        sources, headers = discover_sources(root)
        header_by_name: dict[str, list[str]] = {}
        for h in headers:
            header_by_name.setdefault(h.name, []).append(str(h.relative_to(root)).replace("\\", "/"))
        added: list[str] = []
        existing: list[str] = []
        warnings: list[str] = []
        with self.db.transaction():
            for path in sources:
                inventory = scan_file(path, root)
                rel = inventory.path
                if self.modules.find_by_source(project_id, rel):
                    existing.append(rel)
                    continue
                own_headers = header_by_name.get(Path(rel).with_suffix(".h").name, [])
                module_id = self.add_module(project_id, rel, header_paths=own_headers)
                self.modules.set_dependencies(module_id, inventory.includes_local, "include")
                self.modules.set_dependencies(module_id, inventory.includes_system, "system_include")
                calls = sorted(set().union(*inventory.calls.values())) if inventory.calls else []
                defined = {s.name for s in inventory.symbols}
                self.modules.set_dependencies(
                    module_id, [c for c in calls if c not in defined], "external_call"
                )
                for symbol in inventory.symbols:
                    if symbol.kind == "prototype" and symbol.name in {
                        s.name for s in inventory.by_kind("function")
                    }:
                        continue
                    note = f"line {symbol.line}" + (" static" if symbol.is_static else "")
                    self.modules.upsert_mapping(module_id, symbol.name, "", symbol.kind, note)
                for warning in inventory.warnings:
                    warnings.append(f"{rel}: {warning}")
                    self.modules.add_note(
                        module_id, NoteCategory.COMPATIBILITY_ISSUE, f"Scanner: {warning}"
                    )
                added.append(rel)
        log.info(
            "imported C sources",
            extra={"context": {"project_id": project_id, "added": len(added), "root": str(root)}},
        )
        return ImportSummary(added, existing, len(headers), warnings)

    # -- status -----------------------------------------------------------
    def set_status(self, module_id: int, status: PortingStatus) -> None:
        """Change a module's status, enforcing the porting rules.

        Args:
            module_id: Module id.
            status: New status.

        Raises:
            KeyError: If the module does not exist.
            PortingRuleError: If "Verified" is not justified by the latest
                verification run, or a ported status is set without a Python
                module path.
        """
        module = self.modules.get(module_id)
        if module is None:
            raise KeyError(f"module {module_id} not found")
        if status is PortingStatus.VERIFIED:
            reason = self.verification_blocker(module_id)
            if reason:
                raise PortingRuleError(f"cannot mark as Verified: {reason}")
        if status in (PortingStatus.PORTED, PortingStatus.VERIFICATION_REQUIRED) and not module["python_module_path"]:
            raise PortingRuleError("set the Python replacement module before marking it ported")
        self.modules.set_status(module_id, status)
        log.info(
            "porting status changed",
            extra={"context": {"module": module["c_source_path"], "from": module["status"].value, "to": status.value}},
        )

    def verification_blocker(self, module_id: int) -> str:
        """Explain why a module cannot be marked "Verified".

        Args:
            module_id: Module id.

        Returns:
            The reason, or ``""`` when the latest verification run allows it.
        """
        latest = self.modules.latest_verification(module_id)
        if latest is None:
            return "no verification run has been recorded"
        if latest["reference_kind"] is ReferenceKind.NONE:
            return "the latest run did not compare against the original C implementation"
        if latest["passed"] == 0:
            return "the latest run executed no passing tests"
        if latest["failed"]:
            return f"the latest run had {latest['failed']} failing tests"
        if latest["binary_compatible"] is False:
            return "the latest run reported a binary incompatibility"
        return ""

    # -- notes, mappings, verification -------------------------------------
    def add_note(self, module_id: int, category: NoteCategory, body: str) -> int:
        """Add a note to a module.

        Args:
            module_id: Module id.
            category: Note category.
            body: Note text; surrounding whitespace is removed.

        Returns:
            The new note id.

        Raises:
            ValueError: If the body is blank.
        """
        if not body.strip():
            raise ValueError("note body is empty")
        return self.modules.add_note(module_id, category, body.strip())

    def set_mapping(self, module_id: int, c_symbol: str, python_symbol: str, c_kind: str = "function", notes: str = "") -> None:
        """Set the Python counterpart of a C symbol.

        Args:
            module_id: Module id.
            c_symbol: C symbol name.
            python_symbol: Python replacement name.
            c_kind: Kind of C symbol.
            notes: Free-text porting notes.
        """
        self.modules.upsert_mapping(module_id, c_symbol, python_symbol, c_kind, notes)

    def record_verification(
        self,
        module_id: int,
        reference_kind: ReferenceKind,
        functions_tested: Sequence[str],
        passed: int,
        failed: int,
        binary_compatible: bool | None,
        behavioral_differences: str = "",
        remaining_risks: str = "",
        report_path: str = "",
    ) -> int:
        """Store the results of a verification run that was actually executed.

        A "Verified" module whose new latest run no longer justifies that
        status is moved back to "Verification Required".

        Args:
            module_id: Module id.
            reference_kind: What the port was compared against.
            functions_tested: Names of the functions covered.
            passed: Number of passing checks.
            failed: Number of failing checks.
            binary_compatible: Whether output matched byte for byte, or None.
            behavioral_differences: Known differences from the C code.
            remaining_risks: Open risks.
            report_path: Path of the detailed report.

        Returns:
            The new verification id.

        Raises:
            ValueError: If a test count is negative.
        """
        if passed < 0 or failed < 0:
            raise ValueError("test counts cannot be negative")
        result_id = self.modules.add_verification(
            module_id, reference_kind, functions_tested, passed, failed,
            binary_compatible, behavioral_differences, remaining_risks, report_path,
        )
        module = self.modules.get(module_id)
        if module and module["status"] is PortingStatus.VERIFIED and self.verification_blocker(module_id):
            self.modules.set_status(module_id, PortingStatus.VERIFICATION_REQUIRED)
            log.warning(
                "verified module regressed",
                extra={"context": {"module": module["c_source_path"]}},
            )
        return result_id

    def module_detail(self, module_id: int) -> dict[str, Any]:
        """Fetch a module together with its dependencies, mappings, notes and runs.

        Args:
            module_id: Module id.

        Returns:
            The module row with ``dependencies``, ``mappings``, ``notes``,
            ``verifications`` and the latest ``verification`` added.

        Raises:
            KeyError: If the module does not exist.
        """
        module = self.modules.get(module_id)
        if module is None:
            raise KeyError(f"module {module_id} not found")
        module["dependencies"] = self.modules.dependencies(module_id)
        module["mappings"] = self.modules.mappings(module_id)
        module["notes"] = self.modules.notes(module_id)
        module["verifications"] = self.modules.verifications(module_id)
        module["verification"] = module["verifications"][0] if module["verifications"] else None
        return module

    def build_status_report(self, project_id: int) -> str:
        """Render the Markdown porting status report for a project.

        Args:
            project_id: Project id.

        Returns:
            The Markdown report.

        Raises:
            KeyError: If the project does not exist.
        """
        project = self.projects.get_row(project_id)
        if project is None:
            raise KeyError(f"project {project_id} not found")
        modules = [self.module_detail(m["id"]) for m in self.modules.list_for_project(project_id)]
        return render_report(project, modules)
