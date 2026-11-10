"""GUI startup and page wiring (offscreen)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from app.context import AppContext
from app.core.config import AppConfig
from app.core.models import ConnectionConfig, Device, Direction, Packet, Transport
from app.ui.main_window import MainWindow
from app.ui.registry import BUILTIN_PAGES


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    """Provide a shown main window over a fresh database and isolated settings.

    Args:
        qtbot: pytest-qt robot fixture.
        tmp_path: pytest temporary directory.
        monkeypatch: pytest monkeypatch fixture.

    Yields:
        The main window; it and its services are shut down afterwards.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # isolate QSettings
    config = AppConfig(
        database_path=str(tmp_path / "gui.db"), backup_dir=str(tmp_path / "bk"),
        log_dir=str(tmp_path / "logs"),
    )
    from PySide6.QtCore import QCoreApplication, QEvent, QObject

    owner = QObject()
    ctx = AppContext.create(config, tmp_path / "config.json", owner)
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    win.show()
    yield win
    win.close()
    ctx.shutdown()
    qtbot.wait(20)
    owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def page_or_skip(window, key: str):
    """Return the page for a key, skipping the test if the project removed it.

    Args:
        window: Main window.
        key: Page key.

    Returns:
        The page widget.
    """
    page = window.pages_by_key.get(key)
    if page is None:
        pytest.skip(f"page '{key}' is not part of this project")
    return page


def show_page(window, key: str) -> None:
    """Select a page in the navigation.

    Args:
        window: Main window.
        key: Page key; the test is skipped if the page does not exist.
    """
    page = page_or_skip(window, key)
    window.nav.setCurrentRow(window.stack.indexOf(page))


def test_every_page_opens(qtbot, window):
    """Every built-in page is listed and can be selected.

    Args:
        qtbot: pytest-qt robot fixture.
        window: Main window.
    """
    assert window.nav.count() == len(BUILTIN_PAGES)
    for row in range(window.nav.count()):
        window.nav.setCurrentRow(row)
        qtbot.wait(10)
        assert window.stack.currentIndex() == row


def test_pages_reflect_services(qtbot, window):
    """Pages show service data, and the packet monitor's hex and direction filters work.

    Args:
        qtbot: pytest-qt robot fixture.
        window: Main window.
    """
    ctx = window.ctx
    ctx.devices.save(Device("dev-a", "127.0.0.1", 1234))
    ctx.connections.save_config(ConnectionConfig("c-a", Transport.UDP_SERVER, "127.0.0.1"))
    show_page(window, "devices")
    assert page_or_skip(window, "devices").model.rowCount() == 1
    show_page(window, "connections")
    assert page_or_skip(window, "connections").model.rowCount() == 1

    monitor = page_or_skip(window, "packet_monitor")
    ctx.capture.add_packet(Packet(0.0, Direction.RX, "a", "b", b"\xAA\x55hello"))
    ctx.capture.add_packet(Packet(0.0, Direction.TX, "b", "a", b"\x01\x02"))
    qtbot.waitUntil(lambda: monitor.model.rowCount() == 2, timeout=2000)
    monitor.search.setText("aa 55")
    assert monitor.proxy.rowCount() == 1
    monitor.search.setText("")
    monitor.direction.setCurrentIndex(2)  # TX only
    assert monitor.proxy.rowCount() == 1


def test_porting_page_lists_modules(qtbot, window):
    """The porting page lists a project's modules and shows the selected one.

    Args:
        qtbot: pytest-qt robot fixture.
        window: Main window.
    """
    porting = window.ctx.porting
    pid = porting.create_project("fw")
    porting.add_module(pid, "src/a.c")
    page = page_or_skip(window, "porting")
    page.reload_projects(pid)
    assert page.modules_model.rowCount() == 1
    page.modules_view.selectRow(0)
    page.modules_view.setCurrentIndex(page.modules_model.index(0, 0))
    assert page.detail_title.text() == "a"
    assert "no verification" in page.verify_hint.text()
    assert page.modules_model.index(0, 2).data() == "Not Analyzed"


def test_connection_dialog_returns_enum(qtbot):
    """The connection dialog stores a ``Transport`` enum and raw framing for UDP.

    Args:
        qtbot: pytest-qt robot fixture.
    """
    from app.ui.dialogs.editors import ConnectionDialog

    dialog = ConnectionDialog(None, [], [])
    qtbot.addWidget(dialog)
    dialog.name.setText("x")
    dialog.transport.setCurrentIndex(3)  # UDP server
    dialog._validate_and_accept()
    assert dialog.config.transport is Transport.UDP_SERVER
    assert dialog.config.framing == {"type": "raw"}


def test_runner_executes_pytest_subprocess(qtbot, db):
    """The Test Bench path runs pytest in a QProcess, parses JUnit and stores results.

    Args:
        qtbot: pytest-qt robot fixture.
        db: Migrated test database.
    """
    from app.core.services import ConnectionManager, TestRunner

    runner = TestRunner(db, ConnectionManager(db))
    with qtbot.waitSignal(runner.suite_finished, timeout=60000) as done:
        runner.run_pytest("tests/unit/test_database.py::test_migrate_is_idempotent", ["-p", "no:cacheprovider"])
    summary = done.args[0]
    assert (summary.passed, summary.failed, summary.exit_code) == (1, 0, 0)
    assert runner.results.recent()[0]["status"] == "passed"


def test_window_follows_project_profile(qtbot, tmp_path, monkeypatch):
    """The window follows the project profile: title, page order, start page, plugins and SQL folders.

    Args:
        qtbot: pytest-qt robot fixture.
        tmp_path: pytest temporary directory.
        monkeypatch: pytest monkeypatch fixture.
    """
    from PySide6.QtCore import QCoreApplication, QEvent, QObject

    from app.project import ProjectProfile
    from app.ui.registry import PageRegistry

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    profile = ProjectProfile(app_name="Acme Bench", pages=["database", "settings"], start_page="settings",
                             plugins=["plugins.example_queries"])
    registry = PageRegistry()
    assert registry.load_plugins(profile.plugins) == []
    pages, warnings = registry.select(profile.pages)
    assert warnings == []
    owner = QObject()
    ctx = AppContext.create(AppConfig(database_path=str(tmp_path / "p.db"), log_dir=str(tmp_path / "l")),
                            tmp_path / "c.json", owner, sql_dirs=profile.sql_paths())
    win = MainWindow(ctx, pages, profile)
    qtbot.addWidget(win)
    try:
        assert win.windowTitle() == "Acme Bench"
        assert [win.nav.item(i).text() for i in range(win.nav.count())] == [
            "1  Database Explorer", "2  Settings", "3  My Queries"]
        assert win.nav.currentRow() == 1                     # start_page
        explorer = win.pages_by_key["database"].queries
        explorer.reload_list()
        item = explorer.list.findItems("example.operation_pass_rate", Qt.MatchFlag.MatchExactly)[0]
        explorer.list.setCurrentItem(item)
        assert 'ctx.db.run("example.operation_pass_rate")' == explorer.python_call.text()
        explorer.run_query()
        assert explorer.model.headers == ["operation_name", "runs", "passed", "pass_percent"]
        explorer.editor.setPlainText("SELECT name FROM sqlite_master WHERE type = :kind")
        assert list(explorer._param_edits) == ["kind"]
        explorer._param_edits["kind"].setText("table")
        explorer.run_query()
        assert explorer.model.rowCount() > 10
        win.pages_by_key["example_queries"].run_selected_query()
    finally:
        win.close()
        ctx.shutdown()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
