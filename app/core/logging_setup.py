"""Structured logging.

Every record becomes one JSON object.  Extra structured data is passed as
``log.info("message", extra={"context": {...}})``.

Sinks:
* rotating JSON-lines file in the log directory
* ``application_logs`` table (optional)
* a Qt signal for the Application Logs page (queued, so logging from worker
  threads is safe)
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from .database import LogRepository


def record_to_dict(record: logging.LogRecord) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "timestamp": record.created,
        "level": record.levelname,
        "logger": record.name,
        "message": record.getMessage(),
        "context": getattr(record, "context", None) or {},
    }
    if record.exc_info:
        entry["context"] = dict(entry["context"])
        entry["context"]["exception"] = logging.Formatter().formatException(record.exc_info)
    return entry


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(record_to_dict(record), default=str)


class LogEmitter(QObject):
    """Bridges log records into the Qt world."""

    record_logged = Signal(dict)


class QtLogHandler(logging.Handler):
    def __init__(self, emitter: LogEmitter) -> None:
        super().__init__()
        self.emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.emitter.record_logged.emit(record_to_dict(record))
        except RuntimeError:
            pass  # emitter already destroyed during shutdown


class DatabaseLogHandler(logging.Handler):
    """Writes records to ``application_logs``; never raises into the caller."""

    def __init__(self, repository: LogRepository, max_rows: int) -> None:
        super().__init__()
        self._repository = repository
        self._max_rows = max_rows
        self._guard = threading.local()
        self._since_prune = 0

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._guard, "busy", False):
            return  # a log call made while writing a log row
        self._guard.busy = True
        try:
            entry = record_to_dict(record)
            self._repository.add(
                entry["timestamp"], entry["level"], entry["logger"],
                entry["message"], entry["context"],
            )
            self._since_prune += 1
            if self._since_prune >= 1000:
                self._since_prune = 0
                self._repository.prune(self._max_rows)
        except Exception:
            self.handleError(record)
        finally:
            self._guard.busy = False


def configure_logging(
    level: str,
    log_dir: Path | None = None,
    emitter: LogEmitter | None = None,
    repository: LogRepository | None = None,
    max_database_rows: int = 50000,
) -> list[logging.Handler]:
    """Install handlers on the root logger; returns them for later removal."""
    root = logging.getLogger()
    root.setLevel(level)
    handlers: list[logging.Handler] = []

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handlers.append(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "workbench.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(JsonFormatter())
        handlers.append(file_handler)
    if emitter is not None:
        handlers.append(QtLogHandler(emitter))
    if repository is not None:
        handlers.append(DatabaseLogHandler(repository, max_database_rows))

    for handler in handlers:
        root.addHandler(handler)
    return handlers


def remove_handlers(handlers: list[logging.Handler]) -> None:
    root = logging.getLogger()
    for handler in handlers:
        root.removeHandler(handler)
        handler.close()
