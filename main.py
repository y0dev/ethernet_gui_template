"""Application entry point.

    python main.py [--config PATH] [--project PATH] [--smoke-test]

* ``--project`` / ``EWB_PROJECT``: the template profile (default ``project.json``)
  choosing pages, plugins, SQL folders, name and colours.
* ``--config`` / ``EWB_CONFIG``: per-user runtime settings (default ``data/config.json``).
* ``--smoke-test``: start, visit every page, exit 0 (CI / headless check with
  ``QT_QPA_PLATFORM=offscreen``).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:  # so plugins/ can be imported as "plugins.<name>"
    sys.path.insert(0, str(ROOT))

from app.context import AppContext  # noqa: E402
from app.core.config import ConfigError, config_path, load_config  # noqa: E402
from app.project import ProjectError, load_project, project_path  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.registry import PageRegistry  # noqa: E402
from app.ui.styles import theme  # noqa: E402

log = logging.getLogger(__name__)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the application's command-line arguments.

    Unknown arguments are ignored so Qt can consume its own options.

    Args:
        argv: Command-line arguments, excluding the program name.

    Returns:
        The parsed namespace with ``config``, ``project`` and ``smoke_test``.
    """
    parser = argparse.ArgumentParser(description="Embedded Systems Workbench")
    parser.add_argument("--config", help="path to the runtime configuration JSON")
    parser.add_argument("--project", help="path to the project profile JSON (default project.json)")
    parser.add_argument("--smoke-test", action="store_true", help="start, visit every page, exit")
    args, _qt_args = parser.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> int:
    """Start the workbench GUI and run the Qt event loop.

    Args:
        argv: Full argument vector including the program name. Defaults to
            ``sys.argv``.

    Returns:
        The process exit code: 0 on success, 2 for profile or configuration
        errors, or the smoke-test result.
    """
    argv = sys.argv if argv is None else argv
    args = parse_args(argv[1:])
    app = QApplication(argv)

    try:
        profile = load_project(project_path(args.project))
        theme.configure_colors(profile.theme)
    except (ProjectError, ValueError) as exc:
        QMessageBox.critical(None, "Project profile error", str(exc))
        return 2
    app.setApplicationName(profile.app_name)
    app.setOrganizationName(profile.organization)
    theme.apply_theme(app)

    cfg_file = config_path(args.config)
    try:
        config = load_config(cfg_file)
    except ConfigError as exc:
        QMessageBox.critical(None, "Configuration error", str(exc))
        return 2

    ctx = AppContext.create(config, cfg_file, app, sql_dirs=profile.sql_paths())
    registry = PageRegistry()
    problems = registry.load_plugins(profile.plugins)
    for hook in registry.startup_hooks:
        try:
            hook(ctx)
        except Exception:
            log.exception("plugin start-up hook failed")
    pages, warnings = registry.select(profile.pages)
    window = MainWindow(ctx, pages, profile)
    app.aboutToQuit.connect(ctx.shutdown)
    window.show()
    log.info("application started", extra={"context": {"pages": [p.key for p in pages],
                                                        "profile": profile.source or "defaults"}})
    if (problems or warnings) and not args.smoke_test:
        QMessageBox.warning(window, "Project profile", "\n".join(problems + warnings))

    if args.smoke_test:
        if problems:
            log.error("plugin problems: %s", problems)

        def visit_next_page(row: int = 0) -> None:
            """Select each navigation page in turn, then exit the smoke test.

            Args:
                row: Index of the navigation row to visit next.
            """
            if row < window.nav.count():
                window.nav.setCurrentRow(row)
                QTimer.singleShot(50, lambda: visit_next_page(row + 1))
            else:
                log.info("smoke test complete")
                window.close()
                app.exit(1 if problems else 0)

        QTimer.singleShot(200, visit_next_page)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
