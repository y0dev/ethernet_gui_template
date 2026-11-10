"""Project profile: the one file that shapes this template for a project.

``project.json`` (project root, or ``--project PATH`` / ``EWB_PROJECT``)
decides the application name, which pages exist and in what order, which
plugins load, where named SQL lives, and theme colours.  It is meant to be
committed with your project; per-user runtime settings stay in
``data/config.json``.

Keys starting with ``_`` are comments and ignored.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .core.config import PROJECT_ROOT

log = logging.getLogger(__name__)

DEFAULT_PROJECT_FILE = PROJECT_ROOT / "project.json"


class ProjectError(ValueError):
    """project.json is unreadable or invalid."""


@dataclass
class ProjectProfile:
    app_name: str = "Embedded Systems Workbench"
    app_subtitle: str = "comms · capture · porting"
    organization: str = "EmbeddedWorkbench"
    #: page keys in navigation order; ``None`` = every registered page
    pages: list[str] | None = None
    start_page: str = ""
    #: importable modules with a ``register(registry)`` function
    plugins: list[str] = field(default_factory=list)
    #: folders with named-query ``.sql`` files (after the built-ins)
    sql_dirs: list[str] = field(default_factory=lambda: ["sql"])
    #: colour overrides, see app/ui/styles/theme.py THEME_KEYS
    theme: dict[str, str] = field(default_factory=dict)
    source: str = ""

    def validate(self) -> None:
        if not self.app_name.strip():
            raise ProjectError("app_name must not be empty")
        if self.pages is not None:
            if not isinstance(self.pages, list) or not all(isinstance(p, str) for p in self.pages):
                raise ProjectError("pages must be a list of page keys")
            duplicates = {p for p in self.pages if self.pages.count(p) > 1}
            if duplicates:
                raise ProjectError(f"pages lists {sorted(duplicates)} more than once")
        for name in ("plugins", "sql_dirs"):
            value = getattr(self, name)
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ProjectError(f"{name} must be a list of strings")
        if not isinstance(self.theme, dict):
            raise ProjectError("theme must be an object of colour overrides")

    def sql_paths(self) -> list[Path]:
        paths = []
        for value in self.sql_dirs:
            path = Path(value).expanduser()
            paths.append(path if path.is_absolute() else PROJECT_ROOT / path)
        return paths


def project_path(cli_path: str | None = None) -> Path:
    return Path(cli_path or os.environ.get("EWB_PROJECT") or DEFAULT_PROJECT_FILE)


def load_project(path: Path | None = None) -> ProjectProfile:
    """Read ``path``; a missing file gives the defaults (every page)."""
    if path is None or not path.exists():
        profile = ProjectProfile()
        profile.validate()
        return profile
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectError(f"{path} must contain a JSON object")
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    known = {f.name for f in fields(ProjectProfile)} - {"source"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ProjectError(f"{path.name}: unknown keys {unknown} (allowed: {sorted(known)})")
    profile = ProjectProfile(**data, source=str(path))
    profile.validate()
    return profile


def save_project(profile: ProjectProfile, path: Path) -> None:
    profile.validate()
    data: dict[str, Any] = {f.name: getattr(profile, f.name) for f in fields(profile) if f.name != "source"}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
