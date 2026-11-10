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
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
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
        self.recent_databases = [path] + [p for p in self.recent_databases if p != path]
        del self.recent_databases[keep:]

    def resolve(self, value: str) -> Path:
        """Absolute path for a possibly project-relative config path."""
        path = Path(value).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read configuration {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"configuration {path} must contain a JSON object")
    return data


def config_path(cli_path: str | None = None) -> Path:
    return Path(cli_path or os.environ.get("EWB_CONFIG") or DEFAULT_CONFIG_FILE)


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration; a missing user file simply yields the defaults."""
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
    config.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(asdict(config), handle, indent=2)
        handle.write("\n")
    temp.replace(path)
