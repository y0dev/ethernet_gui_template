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
    #: window / taskbar icon (.svg, .png or .ico), relative to the project root
    icon: str = "resources/icons/app.svg"
    source: str = ""

    def validate(self) -> None:
        """Check that the profile's fields have the expected types and values.

        Raises:
            ProjectError: If any field is empty, mistyped or duplicated.
        """
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
        if not isinstance(self.icon, str):
            raise ProjectError("icon must be a file path (string)")
        if not isinstance(self.theme, dict):
            raise ProjectError("theme must be an object of colour overrides")

    def icon_path(self) -> Path | None:
        """Resolve ``icon`` to an absolute path.

        Relative paths are taken relative to the project root.

        Returns:
            The icon file, or None when ``icon`` is empty (use Qt's default).
        """
        if not self.icon.strip():
            return None
        path = Path(self.icon).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path

    def sql_paths(self) -> list[Path]:
        """Resolve ``sql_dirs`` into absolute paths.

        Relative entries are taken relative to the project root.

        Returns:
            The absolute folder paths, in the configured order.
        """
        paths = []
        for value in self.sql_dirs:
            path = Path(value).expanduser()
            paths.append(path if path.is_absolute() else PROJECT_ROOT / path)
        return paths


def project_path(cli_path: str | None = None) -> Path:
    """Choose the project profile path.

    Args:
        cli_path: Path given on the command line, if any.

    Returns:
        ``cli_path``, else ``$EWB_PROJECT``, else the default ``project.json``.
    """
    return Path(cli_path or os.environ.get("EWB_PROJECT") or DEFAULT_PROJECT_FILE)


def load_project(path: Path | None = None) -> ProjectProfile:
    """Load and validate a project profile.

    A missing file yields the default profile, which shows every page.

    Args:
        path: Location of ``project.json``, or None for the defaults.

    Returns:
        The validated project profile.

    Raises:
        ProjectError: If the file is unreadable, not a JSON object, contains
            unknown keys or fails validation.
    """
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
    """Validate a profile and write it to disk as JSON.

    Args:
        profile: Profile to save; ``source`` is not written.
        path: Destination file.

    Raises:
        ProjectError: If the profile fails validation.
    """
    profile.validate()
    data: dict[str, Any] = {f.name: getattr(profile, f.name) for f in fields(profile) if f.name != "source"}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
