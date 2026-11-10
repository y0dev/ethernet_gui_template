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
        self.db = db
        self.projects = PortingProjectRepository(db)
        self.modules = PortingModuleRepository(db)

    # -- projects ---------------------------------------------------------
    def create_project(
        self, name: str, c_source_root: str = "", python_target_root: str = "", description: str = ""
    ) -> int:
        if not name.strip():
            raise ValueError("project name is required")
        return self.projects.insert_row(
            name=name.strip(), description=description,
            c_source_root=c_source_root, python_target_root=python_target_root,
        )

    def list_projects(self) -> list[dict[str, Any]]:
        return self.projects.list_rows(order_by="name")

    def get_project(self, project_id: int) -> dict[str, Any] | None:
        return self.projects.get_row(project_id)

    def delete_project(self, project_id: int) -> None:
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
        return self.modules.list_for_project(project_id)

    def get_module(self, module_id: int) -> dict[str, Any] | None:
        return self.modules.get(module_id)

    def delete_module(self, module_id: int) -> None:
        self.modules.delete(module_id)

    def import_sources(self, project_id: int, root: Path) -> ImportSummary:
        """Register every ``.c`` file under ``root`` and record its inventory.

        For each source: local includes become dependencies, matching headers
        are attached, and every top-level symbol becomes an (unmapped) entry
        in the function mapping table.  Statuses stay "Not Analyzed".
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
        """Why the module cannot be marked Verified ("" when it can)."""
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
        if not body.strip():
            raise ValueError("note body is empty")
        return self.modules.add_note(module_id, category, body.strip())

    def set_mapping(self, module_id: int, c_symbol: str, python_symbol: str, c_kind: str = "function", notes: str = "") -> None:
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
        """Store results of a verification run that was actually executed."""
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
        module = self.modules.get(module_id)
        if module is None:
            raise KeyError(f"module {module_id} not found")
        module["dependencies"] = self.modules.dependencies(module_id)
        module["mappings"] = self.modules.mappings(module_id)
        module["notes"] = self.modules.notes(module_id)
        module["verifications"] = self.modules.verifications(module_id)
        module["verification"] = module["verifications"][0] if module["verifications"] else None
        return module

    def report(self, project_id: int) -> str:
        project = self.projects.get_row(project_id)
        if project is None:
            raise KeyError(f"project {project_id} not found")
        modules = [self.module_detail(m["id"]) for m in self.modules.list_for_project(project_id)]
        return render_report(project, modules)
