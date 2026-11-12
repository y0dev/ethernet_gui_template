"""Application configuration, kept outside the source code as JSON.

Resolution order for the config file:
1. ``--config PATH`` on the command line
2. environment variable ``EWB_CONFIG``
3. ``data/config.json`` in the project directory

Missing keys fall back to ``resources/default_config.json`` and then to the
dataclass defaults.  Relative paths inside the file are relative to the
project directory.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: True when running as a packaged executable (see tools/build_exe.py).
FROZEN = bool(getattr(sys, "frozen", False))

# The project root holds project.json, plugins/, sql/, resources/ and data/.
# From source it is the repository; in a packaged build it is the folder that
# contains the executable, so those files stay editable next to it.
PROJECT_ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parents[2]
DEFAULTS_FILE = PROJECT_ROOT / "resources" / "default_config.json"
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "data" / "config.json"

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(ValueError):
    """The configuration file is unreadable or contains invalid values."""


@dataclass
class AppConfig:
    database_path: str = "data/workbench.db"
    backup_dir: str = "data/backups"
    log_dir: str = "data/logs"
    log_level: str = "INFO"
    log_to_database: bool = True
    max_database_log_rows: int = 50000
    max_live_packets: int = 100000
    capture_flush_interval_ms: int = 250
    monitor_refresh_interval_ms: int = 100
    default_timeout_ms: int = 5000
    default_reconnect_interval_ms: int = 3000
    reports_dir: str = "data/reports"
    # Session workflow (step 1 remembers the last server settings here)
    workflow_server_host: str = "0.0.0.0"
    workflow_server_port: int = 5000
    workflow_server_transport: str = "tcp_server"
    workflow_server_framing: dict[str, Any] = field(default_factory=lambda: {"type": "raw"})
    recent_databases: list[str] = field(default_factory=list)

    def validate(self) -> None:
        """Check numeric limits, the log level and workflow server settings.

        Raises:
            ConfigError: If any value is out of range or has the wrong type.
        """
        if self.log_level not in LOG_LEVELS:
            raise ConfigError(f"log_level must be one of {LOG_LEVELS}")
        for name in (
            "max_database_log_rows", "max_live_packets", "capture_flush_interval_ms",
            "monitor_refresh_interval_ms", "default_timeout_ms", "default_reconnect_interval_ms",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigError(f"{name} must be a positive integer")

        if not 1 <= self.workflow_server_port <= 65535:
            raise ConfigError("workflow_server_port must be between 1 and 65535")
        if self.workflow_server_transport not in ("tcp_server", "udp_server"):
            raise ConfigError("workflow_server_transport must be 'tcp_server' or 'udp_server'")
        if not isinstance(self.workflow_server_framing, dict):
            raise ConfigError("workflow_server_framing must be an object")
        if not isinstance(self.recent_databases, list):
            raise ConfigError("recent_databases must be a list")

    def remember_database(self, path: str, keep: int = 8) -> None:
        """Move a database path to the front of the recent-databases list.

        Args:
            path: Database file that was just opened.
            keep: Maximum number of entries to retain.
        """
        self.recent_databases = [path] + [p for p in self.recent_databases if p != path]
        del self.recent_databases[keep:]

    def resolve(self, value: str) -> Path:
        """Resolve a configured path to an absolute path.

        Args:
            value: Absolute path, or a path relative to the project root.
                ``~`` is expanded.

        Returns:
            The absolute path.
        """
        path = Path(value).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path


def _read_json(path: Path) -> dict[str, Any]:
    """Read a file that must contain a JSON object.

    Args:
        path: JSON file to read.

    Returns:
        The decoded object.

    Raises:
        ConfigError: If the file cannot be read or parsed, or is not an object.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read configuration {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"configuration {path} must contain a JSON object")
    return data


def config_path(cli_path: str | None = None) -> Path:
    """Choose the runtime configuration path.

    Args:
        cli_path: Path given on the command line, if any.

    Returns:
        ``cli_path``, else ``$EWB_CONFIG``, else ``data/config.json``.
    """
    return Path(cli_path or os.environ.get("EWB_CONFIG") or DEFAULT_CONFIG_FILE)


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration, layering the user file over the shipped defaults.

    A missing user file yields the defaults. Unknown keys are logged and
    ignored.

    Args:
        path: User configuration file, or None to use only the defaults.

    Returns:
        The validated configuration.

    Raises:
        ConfigError: If a file is unreadable or a value is invalid.
    """
    values: dict[str, Any] = {}
    if DEFAULTS_FILE.exists():
        values.update(_read_json(DEFAULTS_FILE))
    if path is not None and path.exists():
        values.update(_read_json(path))
    known = {f.name for f in fields(AppConfig)}
    unknown = sorted(set(values) - known)
    if unknown:
        log.warning("ignoring unknown configuration keys", extra={"context": {"keys": unknown}})
    config = AppConfig(**{k: v for k, v in values.items() if k in known})
    config.validate()
    return config


def save_config(config: AppConfig, path: Path) -> None:
    """Validate a configuration and write it atomically as JSON.

    The file is written to a temporary sibling first and then renamed over
    the destination.

    Args:
        config: Configuration to save.
        path: Destination file; parent folders are created.

    Raises:
        ConfigError: If the configuration fails validation.
    """
    config.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(asdict(config), handle, indent=2)
        handle.write("\n")
    temp.replace(path)
