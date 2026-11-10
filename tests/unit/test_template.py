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
    with pytest.raises(SqlLibraryError, match=message):
        parse_sql_file(text, "bad.sql")


def test_user_folder_overrides_builtin(tmp_path, caplog):
    (tmp_path / "mine.sql").write_text("-- name: logs.recent\nSELECT 42 AS answer;\n-- name: mine.q\nSELECT 1;\n")
    lib = SqlLibrary.load([tmp_path, tmp_path / "missing"])
    assert lib.get("logs.recent").sql == "SELECT 42 AS answer"
    assert "mine.q" in lib and "sessions.list_with_counts" in lib
    assert "sql query overridden" in caplog.text
    with pytest.raises(SqlLibraryError, match="unknown SQL query"):
        lib.get("nope")


def test_every_shipped_query_runs(db):
    """Built-in queries and the examples in sql/ are valid against the real schema."""
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
    """Every page and plugin named in project.json exists (stays true as you customise it)."""
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
