"""Composition root: builds every service once and hands them to the UI."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject

from .core.config import AppConfig, save_config
from .core.database import Database, LogRepository, migrate
from .core.database.sql_library import SqlLibrary
from .core.logging_setup import LogEmitter, configure_logging, remove_handlers
from .core.services import (
    CaptureService,
    ConnectionManager,
    DeviceService,
    OperationRunner,
    PortingService,
    ProtocolService,
    TestRunner,
    WorkflowService,
)

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    config: AppConfig
    config_file: Path
    db: Database
    log_emitter: LogEmitter
    devices: DeviceService
    connections: ConnectionManager
    capture: CaptureService
    protocols: ProtocolService
    porting: PortingService
    tests: TestRunner
    operations: OperationRunner = field(init=False)
    workflow: WorkflowService = field(init=False)
    _log_handlers: list[logging.Handler] = field(default_factory=list)

    @classmethod
    def create(cls, config: AppConfig, config_file: Path, parent: QObject | None = None,
               sql_dirs: list[Path] | None = None) -> AppContext:
        """Build the database, logging and every service from configuration.

        Args:
            config: Validated runtime configuration.
            config_file: Path the configuration is saved back to.
            parent: Optional Qt parent that owns the services' QObjects.
            sql_dirs: Extra folders of named-query ``.sql`` files to load.

        Returns:
            A fully wired application context.
        """
        db = Database(config.resolve(config.database_path), sql=SqlLibrary.load(sql_dirs or []))
        version = migrate(db)
        emitter = LogEmitter(parent)
        handlers = configure_logging(
            config.log_level,
            config.resolve(config.log_dir),
            emitter,
            LogRepository(db) if config.log_to_database else None,
            config.max_database_log_rows,
        )
        log.info("database ready", extra={"context": {"path": str(db.path), "schema_version": version}})
        connections = ConnectionManager(db, parent)
        protocols = ProtocolService(db)
        for problem in protocols.load():
            log.warning("protocol not loaded: %s", problem)
        ctx = cls(
            config=config,
            config_file=config_file,
            db=db,
            log_emitter=emitter,
            devices=DeviceService(db),
            connections=connections,
            capture=CaptureService(
                db, connections, config.max_live_packets,
                config.capture_flush_interval_ms, config.monitor_refresh_interval_ms, parent,
            ),
            protocols=protocols,
            porting=PortingService(db),
            tests=TestRunner(db, connections, parent),
        )
        ctx._log_handlers = handlers
        ctx.operations = OperationRunner(db, connections, parent)
        ctx.workflow = WorkflowService(
            db, config, connections, ctx.capture, ctx.operations, ctx.devices, protocols,
            ctx.save_config, parent,
        )
        return ctx

    def save_config(self) -> None:
        """Persist the current runtime configuration to ``config_file``."""
        save_config(self.config, self.config_file)

    def backup_database(self) -> Path:
        """Copy the database to a timestamped file in the backup directory.

        Returns:
            Path of the backup file that was written.
        """
        import datetime as dt

        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.db.backup(self.config.resolve(self.config.backup_dir) / f"workbench_{stamp}.db")

    def shutdown(self) -> None:
        """Stop workflows and capture, close connections, logging and the database."""
        log.info("shutting down")
        self.workflow.shutdown()
        self.capture.shutdown()
        self.connections.close_all()
        remove_handlers(self._log_handlers)
        self.db.close()
