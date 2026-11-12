"""Template features: named SQL, SQL migrations, project profile, page registry, theme, tools."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.database import Database, migrate
from app.core.database.migrations import MigrationError, load_migrations, split_statements
from app.core.database.sql_library import SqlLibrary, SqlLibraryError, parse_sql_file
from app.project import ProjectError, load_project

ROOT = Path(__file__).resolve().parents[2]


# -- named SQL ----------------------------------------------------------------
def test_parse_sql_file():
    """Named queries get their descriptions, parameters, sources and read-only flags."""
    text = """
-- header comment is fine
-- name: a.one
-- First line of description.
-- Second line.
SELECT * FROM t WHERE x = :x AND y = ':not_a_param' AND z = :z AND x2 = :x;

-- name: a.two
DELETE FROM t WHERE id = :id
"""
    one, two = parse_sql_file(text, "f.sql")
    assert one.name == "a.one" and one.description == "First line of description. Second line."
    assert one.parameters == ["x", "z"] and one.read_only and one.source == "f.sql:3"
    assert not one.sql.endswith(";")
    assert two.parameters == ["id"] and not two.read_only


@pytest.mark.parametrize("text, message", [
    ("SELECT 1;", "before the first"),
    ("-- name: a\n", "has no SQL"),
    ("-- name: a\nSELECT 1;\n-- name: a\nSELECT 2;", "duplicate"),
    ("-- name: a\nSELECT 'unterminated", "not a complete"),
])
def test_parse_errors(text, message):
    """Malformed query files raise ``SqlLibraryError`` with a helpful message.

    Args:
        text: Malformed file contents.
        message: Expected part of the error message.
    """
    with pytest.raises(SqlLibraryError, match=message):
        parse_sql_file(text, "bad.sql")


def test_user_folder_overrides_builtin(tmp_path, caplog):
    """A user folder overrides built-in queries with a logged warning, and missing folders are skipped.

    Args:
        tmp_path: pytest temporary directory.
        caplog: pytest log capture fixture.
    """
    (tmp_path / "mine.sql").write_text("-- name: logs.recent\nSELECT 42 AS answer;\n-- name: mine.q\nSELECT 1;\n")
    lib = SqlLibrary.load([tmp_path, tmp_path / "missing"])
    assert lib.get("logs.recent").sql == "SELECT 42 AS answer"
    assert "mine.q" in lib and "sessions.list_with_counts" in lib
    assert "sql query overridden" in caplog.text
    with pytest.raises(SqlLibraryError, match="unknown SQL query"):
        lib.get("nope")


def test_every_shipped_query_runs(db):
    """Built-in queries and the examples in ``sql/`` are valid against the real schema.

    Modifying queries run inside a transaction that is rolled back.

    Args:
        db: Migrated test database.
    """
    db.sql = SqlLibrary.load([ROOT / "sql"])
    assert len(db.sql.names("example.")) >= 3
    for name in db.sql.names():
        query = db.sql.get(name)
        params = {p: 1 for p in query.parameters}
        if query.read_only:
            db.run(name, **params)
        else:
            with pytest.raises(RuntimeError):     # roll back whatever it would change
                with db.transaction():
                    db.run_write(name, **params)
                    raise RuntimeError


def test_run_helpers_and_read_only_connection(db):
    """The named-query helpers work and the read-only connection refuses writes.

    Args:
        db: Migrated test database.
    """
    db.execute("INSERT INTO devices (name, transport) VALUES ('d1', 'tcp_client')")
    assert db.run_one("stats.database_summary")["devices"] == 1
    assert db.run_scalar("stats.database_summary") == 0       # first column = sessions
    headers, rows = db.run_read_only("SELECT name FROM devices WHERE name = :n", {"n": "d1"})
    assert headers == ["name"] and rows == [("d1",)]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.run_read_only("DELETE FROM devices")
    assert db.scalar("SELECT COUNT(*) FROM devices") == 1


# -- migrations as .sql files -------------------------------------------------
def test_split_statements_handles_triggers_and_literals():
    """Statement splitting keeps triggers and semicolons inside literals intact."""
    script = """
-- comment
CREATE TABLE a (x TEXT DEFAULT 'semi;colon');
CREATE TRIGGER t AFTER INSERT ON a BEGIN
    UPDATE a SET x = 'y; z' WHERE rowid = NEW.rowid;
END;
-- trailing comment
"""
    statements = split_statements(script)
    assert len(statements) == 2 and statements[1].startswith("CREATE TRIGGER") and statements[1].endswith("END")
    with pytest.raises(MigrationError):
        split_statements("CREATE TABLE b (x")


def test_migration_files_rules(tmp_path):
    """Migration files must be consecutive, non-empty and named ``NNNN_name.sql``.

    Args:
        tmp_path: pytest temporary directory.
    """
    (tmp_path / "0001_first.sql").write_text("CREATE TABLE a (x);")
    (tmp_path / "0003_gap.sql").write_text("CREATE TABLE b (x);")
    with pytest.raises(MigrationError, match="consecutive"):
        load_migrations(tmp_path)
    (tmp_path / "0003_gap.sql").unlink()
    (tmp_path / "0002_empty.sql").write_text("-- nothing\n")
    with pytest.raises(MigrationError, match="no SQL"):
        load_migrations(tmp_path)
    (tmp_path / "0002_empty.sql").write_text("CREATE TABLE b (x);\nINSERT INTO b VALUES (1);")
    (tmp_path / "bad name.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="NNNN_name"):
        load_migrations(tmp_path)
    (tmp_path / "bad name.sql").unlink()
    migrations = load_migrations(tmp_path)
    database = Database(tmp_path / "x.db")
    try:
        assert migrate(database, migrations) == 2
        assert database.scalar("SELECT COUNT(*) FROM b") == 1
    finally:
        database.close()


def test_new_migration_tool(tmp_path):
    """``tools/new_migration.py`` creates the next numbered file with a slugged name.

    Args:
        tmp_path: pytest temporary directory.
    """
    schema = tmp_path / "app" / "core" / "database" / "schema"
    schema.mkdir(parents=True)
    for f in (ROOT / "app" / "core" / "database" / "schema").glob("*.sql"):
        shutil.copy(f, schema)
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "new_migration.py"), "Add calibration table!",
                          "--root", str(tmp_path)], capture_output=True, text=True, check=True)
    created = sorted(schema.glob("*.sql"))[-1]
    assert created.name == "0004_add_calibration_table.sql" and "created" in out.stdout


# -- project profile ----------------------------------------------------------
def test_shipped_project_json_is_valid(monkeypatch):
    """Every page and plugin named in ``project.json`` exists.

    This stays true as you customise the profile.

    Args:
        monkeypatch: pytest monkeypatch fixture.
    """
    from app.ui.registry import PageRegistry

    monkeypatch.syspath_prepend(str(ROOT))
    profile = load_project(ROOT / "project.json")
    registry = PageRegistry()
    assert registry.load_plugins(profile.plugins) == []
    specs, warnings = registry.select(profile.pages)
    assert warnings == [] and specs
    if profile.start_page:
        assert profile.start_page in [s.key for s in specs]


def test_project_profile_rules(tmp_path):
    """A missing profile gives the defaults, ``_`` keys are ignored and invalid profiles are rejected.

    Args:
        tmp_path: pytest temporary directory.
    """
    assert load_project(tmp_path / "missing.json").pages is None     # defaults: every page
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"_note": "ignored", "app_name": "Acme Bench", "pages": ["devices"]}))
    profile = load_project(path)
    assert profile.app_name == "Acme Bench" and profile.pages == ["devices"]
    for bad, message in ((({"colour": "x"}), "unknown keys"), ({"pages": ["a", "a"]}, "more than once"),
                         ({"app_name": " "}, "app_name"), ({"plugins": "x"}, "plugins")):
        path.write_text(json.dumps(bad))
        with pytest.raises(ProjectError, match=message):
            load_project(path)


# -- registry -----------------------------------------------------------------
def test_registry_selection_and_plugins(tmp_path, monkeypatch):
    """Plugins add pages and hooks, failures are reported, and selection orders, replaces and skips pages.

    Args:
        tmp_path: pytest temporary directory.
        monkeypatch: pytest monkeypatch fixture.
    """
    from PySide6.QtWidgets import QWidget

    from app.ui.registry import BUILTIN_PAGES, PageRegistry, RegistryError

    plugin_dir = tmp_path / "plugpkg"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("")
    (plugin_dir / "good.py").write_text(
        "from PySide6.QtWidgets import QLabel\n"
        "def register(r):\n"
        "    r.add_page('extra', 'Extra', lambda ctx: QLabel('x'))\n"
        "    r.on_startup(lambda ctx: None)\n"
    )
    (plugin_dir / "broken.py").write_text("raise ImportError('boom')\n")
    (plugin_dir / "noreg.py").write_text("x = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    registry = PageRegistry()
    assert registry.keys() == [k for k, _t, _m in BUILTIN_PAGES]
    problems = registry.load_plugins(["plugpkg.good", "plugpkg.broken", "plugpkg.noreg"])
    assert len(problems) == 2 and len(registry.startup_hooks) == 1
    specs, warnings = registry.select(["settings", "devices", "ghost"])
    assert [s.key for s in specs] == ["settings", "devices", "extra"]   # plugin appended
    assert any("ghost" in w for w in warnings)
    with pytest.raises(RegistryError):
        registry.add_page("devices", "Mine", QWidget)
    registry.add_page("devices", "My Devices", QWidget, replace=True)
    assert registry.get("devices").title == "My Devices"
    registry.add_page("gone", "Gone", "app.ui.pages.does_not_exist:Page")
    specs, warnings = registry.select(["gone", "logs"])
    assert [s.key for s in specs] == ["logs", "extra"] and any("skipped" in w for w in warnings)
    with pytest.raises(RegistryError):
        registry.add_page("bad key", "x", QWidget)


# -- theme --------------------------------------------------------------------
def test_theme_overrides():
    """Theme overrides change the stylesheet and colour maps and reject bad keys or colours."""
    from app.ui.styles import theme

    original = {key: getattr(theme, attr) for key, attr in theme.THEME_KEYS.items()}
    try:
        theme.configure_colors({"accent": "#ff8800", "ok": "#00ff00"})
        assert "#ff8800" in theme.build_stylesheet() and theme.STATE_COLORS["Connected"] == "#00ff00"
        with pytest.raises(ValueError, match="unknown theme keys"):
            theme.configure_colors({"accentt": "#fff"})
        with pytest.raises(ValueError, match="not a colour"):
            theme.configure_colors({"accent": "notacolour"})
    finally:
        theme.configure_colors(original)
    assert theme.ACCENT == original["accent"]


# -- generated page plugin works ----------------------------------------------
def test_new_page_tool_creates_working_plugin(qtbot, tmp_path):
    """``tools/new_page.py`` writes a plugin, enables it in ``project.json`` and the page works.

    Args:
        qtbot: pytest-qt robot fixture.
        tmp_path: pytest temporary directory.
    """
    from PySide6.QtCore import QCoreApplication, QEvent, QObject

    from app.context import AppContext
    from app.core.config import AppConfig
    from app.ui.registry import PageRegistry

    (tmp_path / "project.json").write_text(json.dumps({"pages": ["workflow", "settings"], "plugins": []}))
    subprocess.run([sys.executable, str(ROOT / "tools" / "new_page.py"), "calibration", "Calibration",
                    "--root", str(tmp_path)], check=True, capture_output=True)
    data = json.loads((tmp_path / "project.json").read_text())
    assert data["plugins"] == ["plugins.calibration"]
    assert data["pages"] == ["workflow", "calibration", "settings"]

    spec = importlib.util.spec_from_file_location("gen_calibration", tmp_path / "plugins" / "calibration.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registry = PageRegistry(include_builtins=False)
    module.register(registry)
    owner = QObject()
    ctx = AppContext.create(AppConfig(database_path=str(tmp_path / "d.db"), log_dir=str(tmp_path / "l")),
                            tmp_path / "c.json", owner)
    try:
        page = registry.get("calibration").resolve()(ctx)
        qtbot.addWidget(page)
        page.show()
        assert page.status.text() == "0 session(s)"
    finally:
        ctx.shutdown()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _load_generated_page(tmp_path: Path, key: str, ctx):
    """Import a generated plugin and build its page.

    Args:
        tmp_path: Project root the plugin was generated in.
        key: Page key.
        ctx: Application context passed to the page.

    Returns:
        The page widget.
    """
    from app.ui.registry import PageRegistry

    spec = importlib.util.spec_from_file_location(f"gen_{key}", tmp_path / "plugins" / f"{key}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registry = PageRegistry(include_builtins=False)
    module.register(registry)
    return registry.get(key).resolve()(ctx)


def _exercise_page(template: str, page) -> None:
    """Run one action of each built-in template's page and check its effect.

    Args:
        template: Template the page was generated from.
        page: Generated page widget, already shown.
    """
    from app.core.models import Direction, Packet

    if template == "table":
        assert page.status.text() == "0 session(s)"
    elif template == "query_form":
        assert list(page.inputs) == ["session_id"]
        page.inputs["session_id"].setText("1")
        page.run_query()
        assert page.status.text() == "0 row(s) from report.traffic"
    elif template == "live_packets":
        page.add_packets([Packet(0.0, Direction.RX, "a:1", "b:2", b"\x01hi")])
        assert page.table.rowCount() == 1 and page.table.item(0, 5).text() == "01 68 69"
        page.set_paused(True)
        page.add_packets([Packet(0.0, Direction.TX, "b:2", "a:1", b"x")])
        assert page.table.rowCount() == 1
        page.clear_packets()
        assert page.table.rowCount() == 0
    elif template == "device_console":
        assert page.connection.count() == 0
        page.show_frame(Packet(0.0, Direction.RX, "a:1", "b:2", b"OK"))
        assert "<< " in page.log.toPlainText() and "|OK|" in page.log.toPlainText()
    elif template == "blank":
        assert page.content.count() >= 1
    elif template == "db_table":
        assert page.editor is not None and page.editor.chooser.count() == 0     # only workbench tables exist
        assert "no tables of its own" in page.editor.status.text()
    elif template == "workflow":
        page.go_to(1)
        assert page.stack.currentIndex() == 0 and "run name" in page.hint.text()   # locked until set up
        page.steps[0].name.setText("Bring-up")
        page.go_to(1)
        page.steps[1].run()
        page.go_to(2)
        assert page.stack.currentIndex() == 2 and "Runs completed: 1" in page.steps[2].summary.text()
    elif template.startswith("menu_"):
        # no hook modules and no server in this context: an empty menu that says what to do
        assert page.options == [] and "Start the server" in page.status.text()
        page.set_targets([])
        assert page.ticked_peers() == []
    elif template == "device_workflow":
        import socket

        page.go_to(1)
        assert page.stack.currentIndex() == 0                                    # locked until the server runs
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = page.steps[0]
        server.host.setText("127.0.0.1")
        server.port.setValue(port)
        server.start()
        assert server.is_done() and "Listening" in server.status.text()
        page.go_to(1)
        page.steps[1].start_session()
        assert page.steps[1].is_done()
        page.go_to(2)
        assert page.stack.currentIndex() == 2 and "no operations yet" in page.steps[2].operations.item(0).text()
        page.go_to(3)
        assert "no results yet" in page.steps[3].summary.text()
        page.ctx.workflow.end_session()
        page.ctx.workflow.stop_server()


@pytest.mark.parametrize("template", ["table", "blank", "query_form", "live_packets", "device_console", "db_table",
                                      "workflow", "device_workflow", "menu_console", "menu_buttons", "menu_form",
                                      "menu_sequence"])
def test_every_builtin_page_template_works(qtbot, tmp_path, template):
    """Each built-in template generates a page that loads, opens and does its job.

    Args:
        qtbot: pytest-qt robot fixture.
        tmp_path: pytest temporary directory.
        template: Built-in template name.
    """
    from PySide6.QtCore import QCoreApplication, QEvent, QObject

    from app.context import AppContext
    from app.core.config import AppConfig

    subprocess.run([sys.executable, str(ROOT / "tools" / "new_page.py"), "sample", "Sample \"Page\"",
                    "--template", template, "--root", str(tmp_path)], check=True, capture_output=True)
    source = (tmp_path / "plugins" / "sample.py").read_text(encoding="utf-8")
    assert "class SamplePage(QWidget)" in source and "# template:" not in source and "$" not in source
    owner = QObject()
    ctx = AppContext.create(AppConfig(database_path=str(tmp_path / "d.db"), log_dir=str(tmp_path / "l")),
                            tmp_path / "c.json", owner)
    try:
        page = _load_generated_page(tmp_path, "sample", ctx)
        qtbot.addWidget(page)
        page.show()
        _exercise_page(template, page)
    finally:
        ctx.shutdown()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_new_page_custom_templates_and_errors(tmp_path, capsys, monkeypatch):
    """Project templates are listed and override built-ins, paths work, and broken templates are refused.

    Args:
        tmp_path: pytest temporary directory.
        capsys: pytest output capture fixture.
        monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    import new_page

    templates = tmp_path / "page_templates"
    templates.mkdir()
    (templates / "mine.py.tmpl").write_text(
        "# template: My own page\nclass $cls:\n    KEY = \"$key\"\n    TITLE = \"$title\"\n    PRICE = \"$$5\"\n"
    )
    (templates / "blank.py.tmpl").write_text("# template: Project blank\nOVERRIDDEN = \"$key\"\n")
    (templates / "typo.py.tmpl").write_text("NAME = \"$titel\"\n")
    (templates / "broken.py.tmpl").write_text("def $cls(:\n")
    root = ["--root", str(tmp_path), "--no-enable"]

    assert new_page.main(["--list-templates", *root]) == 0
    listing = capsys.readouterr().out
    assert "mine             My own page" in listing and "Project blank" in listing
    assert "query_form" in listing and "(default)" in listing

    assert new_page.main(["log_view", "Log View", "--template", "mine", *root]) == 0
    generated = (tmp_path / "plugins" / "log_view.py").read_text()
    assert generated == 'class LogViewPage:\n    KEY = "log_view"\n    TITLE = "Log View"\n    PRICE = "$5"\n'
    assert new_page.main(["over", "Over", "--template", "blank", *root]) == 0
    assert "OVERRIDDEN" in (tmp_path / "plugins" / "over.py").read_text()
    assert new_page.main(["by_path", "By Path", "--template", str(templates / "mine.py.tmpl"), *root]) == 0

    capsys.readouterr()
    assert new_page.main(["a", "A", "--template", "typo", *root]) == 2
    assert "bad placeholder" in capsys.readouterr().out
    assert new_page.main(["b", "B", "--template", "broken", *root]) == 2
    assert "not valid Python" in capsys.readouterr().out
    assert new_page.main(["c", "C", "--template", "nope", *root]) == 2
    assert "unknown template 'nope'" in capsys.readouterr().out
    assert new_page.main(["log_view", "Again", *root]) == 2          # never overwrites
    assert not (tmp_path / "plugins" / "a.py").exists()
    assert new_page.main(root) == 2                                   # key and title required


def test_table_option_generates_an_editing_page_for_another_database(qtbot, tmp_path):
    """``--table`` with ``--database`` builds a page that edits that file's table and leaves the file alone.

    Args:
        qtbot: pytest-qt robot fixture.
        tmp_path: pytest temporary directory.
    """
    from PySide6.QtCore import QCoreApplication, QEvent, QObject

    from app.context import AppContext
    from app.core.config import AppConfig

    plant = tmp_path / "data" / "plant.db"
    plant.parent.mkdir()
    with sqlite3.connect(plant) as conn:
        conn.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, value REAL)")
        conn.execute("INSERT INTO readings (value) VALUES (1.0)")
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "new_page.py"), "plant", "Plant", "--table",
                          "readings", "--database", "data/plant.db", "--root", str(tmp_path), "--no-enable"],
                         check=True, capture_output=True, text=True)
    assert "template 'db_table'" in out.stdout
    source = (tmp_path / "plugins" / "plant.py").read_text(encoding="utf-8")
    assert 'TABLE = "readings"' in source and 'DATABASE = "data/plant.db"' in source

    owner = QObject()
    config = AppConfig(database_path=str(tmp_path / "d.db"), log_dir=str(tmp_path / "l"))
    ctx = AppContext.create(config, tmp_path / "c.json", owner)
    ctx.config.resolve = lambda value: tmp_path / value          # resolve paths against this test project
    try:
        page = _load_generated_page(tmp_path, "plant", ctx)
        qtbot.addWidget(page)
        page.show()
        model = page.editor.model
        assert model.rowCount() == 1
        assert model.setData(model.index(0, 1), "2.5")
        assert page.editor.save_changes() == 1
        page.editor.db.close()
    finally:
        ctx.shutdown()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    with sqlite3.connect(plant) as conn:
        assert conn.execute("SELECT value FROM readings").fetchone()[0] == 2.5
        assert [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")] == ["readings"]


def test_app_icon_from_project_profile(qtbot, tmp_path):
    """The profile's icon is applied to the application; missing or empty icons fall back quietly.

    Args:
        qtbot: pytest-qt robot fixture.
        tmp_path: pytest temporary directory.
    """
    from PySide6.QtWidgets import QApplication

    import main
    from app.project import ProjectProfile

    profile = load_project(ROOT / "project.json")
    assert profile.icon_path().is_file()
    app = QApplication.instance()
    icon = main.apply_app_icon(app, profile)
    assert icon is not None and not icon.isNull() and not app.windowIcon().isNull()
    assert icon.pixmap(16, 16).width() == 16                      # the SVG renders at taskbar size

    assert main.apply_app_icon(app, ProjectProfile(icon=str(tmp_path / "missing.png"))) is None
    assert ProjectProfile(icon="").icon_path() is None
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"icon": 5}))
    with pytest.raises(ProjectError, match="icon"):
        load_project(path)
