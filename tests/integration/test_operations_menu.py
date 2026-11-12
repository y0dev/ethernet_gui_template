"""Operations Menu: Python hooks run on simulated devices over loopback."""

from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path

import pytest

from app.context import AppContext
from app.core.config import AppConfig
from app.core.models import Transport
from app.core.services.menu_hooks import HookRun, MenuOption, Param, load_hook_modules, menu_option

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "examples" / "led_device"))
import device_simulator  # noqa: E402
import led_device_sim  # noqa: E402
import make_command_set  # noqa: E402

LINE = {"type": "delimiter", "delimiter_hex": "0a"}
T = 5000


@pytest.fixture
def ctx(qtbot, tmp_path):
    """Provide a full application context over temporary files.

    Args:
        qtbot: pytest-qt robot fixture.
        tmp_path: pytest temporary directory.

    Yields:
        The context; its services are shut down and deleted afterwards.
    """
    from PySide6.QtCore import QCoreApplication, QEvent, QObject

    owner = QObject()
    config = AppConfig(database_path=str(tmp_path / "main.db"), log_dir=str(tmp_path / "logs"),
                       reports_dir=str(tmp_path / "reports"))
    context = AppContext.create(config, tmp_path / "config.json", owner)
    yield context
    context.shutdown()
    qtbot.wait(50)
    owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def session(qtbot, ctx):
    """Start the workflow server, connect devices and start a session.

    Yields:
        ``start(framing, device_target, count)`` returning the device addresses.
    """
    stop = threading.Event()
    threads: list[threading.Thread] = []

    def start(framing: dict, target, count: int = 2) -> list[str]:
        """Start the server, the simulated devices and a session.

        Args:
            framing: Server framing.
            target: ``(host, port, serial, stop)`` device function.
            count: Number of devices.

        Returns:
            The connected device addresses.
        """
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        wf = ctx.workflow
        wf.start_server("127.0.0.1", port, Transport.TCP_SERVER, framing)
        wf.use_current_database()
        for i in range(count):
            thread = threading.Thread(target=target, args=("127.0.0.1", port, f"SIM-{i + 1:04d}", stop), daemon=True)
            thread.start()
            threads.append(thread)
        qtbot.waitUntil(lambda: len(wf.connected_devices()) == count, timeout=T)
        wf.start_session("menu hooks")
        return [d.peer for d in wf.connected_devices()]

    yield start
    stop.set()
    for thread in threads:
        thread.join(timeout=3)


def run_hook(qtbot, ctx, option: MenuOption, peer: str, **values) -> tuple[str, list[tuple[str, str]]]:
    """Run one option on one device and wait until it finishes.

    Args:
        qtbot: pytest-qt robot fixture.
        ctx: Application context.
        option: Option to run.
        peer: Device address.
        **values: Input values.

    Returns:
        The final state and every (level, text) log line.
    """
    run = HookRun(ctx.workflow, option, peer, peer, values)
    states: list[str] = []
    lines: list[tuple[str, str]] = []
    run.finished.connect(states.append)
    run.output.connect(lambda level, text: lines.append((level, text)))
    run.start()
    qtbot.waitUntil(lambda: bool(states), timeout=T * 2)
    return states[0], lines


def by_title(module: str) -> dict[str, MenuOption]:
    """Load a hook module and index its options by title.

    Args:
        module: Module name.

    Returns:
        Options by title.
    """
    options, problems = load_hook_modules([module])
    assert problems == []
    return {o.title: o for o in options}


def test_hook_modules_load_reload_and_report_problems(tmp_path, monkeypatch):
    """Options keep definition order, a reload does not duplicate them, and bad modules are reported."""
    first = list(by_title("plugins.example_menu"))
    assert first[:3] == ["Ping", "Identify", "Set LED"]
    assert list(by_title("plugins.example_menu")) == first
    (tmp_path / "empty_hooks.py").write_text("X = 1\n")
    (tmp_path / "broken_hooks.py").write_text("raise RuntimeError('boom')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    options, problems = load_hook_modules(["empty_hooks", "broken_hooks", "no_such_module"])
    assert options == []
    assert [p.split(":")[0] for p in problems] == ["empty_hooks", "broken_hooks", "no_such_module"]
    assert "RuntimeError: boom" in problems[1]


def test_param_types_and_descriptions():
    """Inputs take their type from the default, and the docstring becomes the help text."""
    assert Param("n", 3).value_type is int and Param("t", 1.5).value_type is float
    assert Param("s").value_type is str and Param("x", 1, kind=float).value_type is float
    option = by_title("plugins.example_menu")["Blink LED"]
    assert [p.name for p in option.params] == ["times", "period_ms"]
    assert option.description.startswith("Blink the LED")


def test_hook_errors_are_reported_not_raised(qtbot, ctx):
    """An exception or a wrong yield ends the run as "error" with the hook's line number."""
    def boom(dev):
        raise ValueError("bad value")

    def wrong_yield(dev):
        yield 42

    state, lines = run_hook(qtbot, ctx, MenuOption("Boom", boom), "1.2.3.4:5")
    assert state == "error" and "ValueError: bad value (line" in lines[-1][1]
    state, lines = run_hook(qtbot, ctx, MenuOption("Wrong", wrong_yield), "1.2.3.4:5")
    assert state == "error" and "yield dev.request" in lines[-1][1]

    def no_session(dev):
        yield dev.request("PING")

    state, lines = run_hook(qtbot, ctx, MenuOption("No server", no_session), "1.2.3.4:5")
    assert state == "error" and "server is not running" in lines[-1][1]


def test_example_hooks_on_simulated_devices(qtbot, ctx, session):
    """Every example option runs on real sockets; replies are decoded and stored under the session."""
    peers = session(LINE, device_simulator.run_tcp_device)
    options = by_title("plugins.example_menu")
    results = []
    ctx.operations.result_ready.connect(results.append)

    assert run_hook(qtbot, ctx, options["Ping"], peers[0])[0] == "done"
    state, lines = run_hook(qtbot, ctx, options["Identify"], peers[1])
    assert state == "done" and any(", VERSION 1.0.0-sim" in text and "ID SIM-000" in text for _l, text in lines)
    assert run_hook(qtbot, ctx, options["Set LED"], peers[0], state="OFF")[0] == "done"
    assert run_hook(qtbot, ctx, options["Blink LED"], peers[0], times=2, period_ms=50)[0] == "done"
    assert run_hook(qtbot, ctx, options["Health check"], peers[0], max_temp=150.0)[0] == "done"
    state, lines = run_hook(qtbot, ctx, options["Health check"], peers[0], max_temp=0.0)
    assert state == "failed" and "above" in lines[-1][1]
    assert run_hook(qtbot, ctx, options["Reset and check"], peers[1])[0] == "done"
    state, lines = run_hook(qtbot, ctx, options["Send text"], peers[1], text="HELLO")
    assert state == "done" and any("ERR UNKNOWN HELLO" in text for _l, text in lines)

    # a plain (non-generator) hook finishes at once; its requests still run in order
    assert run_hook(qtbot, ctx, options["LED on then off (no waiting)"], peers[1])[0] == "done"
    qtbot.waitUntil(lambda: [r["operation_name"] for r in results][-2:] == ["LED on then off (no waiting)"] * 2,
                    timeout=T)

    stored = ctx.db.query("SELECT COUNT(*) AS n FROM operation_results WHERE session_id = ?",
                          (ctx.workflow.session_id,))
    assert stored[0]["n"] == len(results) >= 15
    assert "Ping after reset" in {r["operation_name"] for r in results}


def test_page_runs_options_on_every_ticked_device(qtbot, ctx, session):
    """The page lists the hooks with numbers, runs an option on each ticked device and logs it."""
    from app.ui.pages.operations_menu import OperationsMenuPage

    session(LINE, device_simulator.run_tcp_device, count=3)
    ctx.menu_hooks = ["plugins.example_menu"]
    page = OperationsMenuPage(ctx)
    qtbot.addWidget(page)
    assert "recording" in page.status.text() and page.devices.count() == 3
    first = page.menu.topLevelItem(0).child(0)
    assert (first.text(0), first.text(1)) == ("1", "Ping")
    runs = page.run_option(page.options[0])
    assert len(runs) == 3
    qtbot.waitUntil(lambda: not page.runs, timeout=T)
    assert {run.state for run in runs} == {"done"}
    assert page.log.toPlainText().count("Ping: done") == 3


def test_led_example_hooks(qtbot, ctx, session):
    """The C console menu as hooks: encoding, decoding and checks work against the LED simulator."""
    peers = session(make_command_set.FRAMING, led_device_sim.run_device)
    options = by_title("examples.led_device.menu_hooks")
    for title, values in (("Ping", {}), ("Send device info", {}), ("Set LED", {"on": True}),
                          ("Blink LED", {"times": 2, "period_ms": 50}), ("Read temperature", {"max_temp": 100.0}),
                          ("Reset", {})):
        state, lines = run_hook(qtbot, ctx, options[title], peers[0], **values)
        assert state == "done", (title, lines)
        if title == "Send device info":
            assert any("serial SIM-000" in text and "firmware 1.2.0" in text for _l, text in lines)
    state, _lines = run_hook(qtbot, ctx, options["Read temperature"], peers[1], max_temp=-40.0)
    assert state == "failed"


def test_menu_option_decorator_returns_the_function():
    """Decorating leaves the function callable as before."""
    @menu_option("Plain")
    def plain(dev):
        return "ok"

    assert plain(None) == "ok"


def test_workflow_step_4_can_be_the_operations_menu(qtbot, ctx, session):
    """With workflow_operations = "menu", step 4 is the hook menu and "Operations for selected" ticks devices."""
    from app.ui.pages.operations_menu import OperationsMenuPage
    from app.ui.pages.workflow import OperationsStep, WorkflowPage

    peers = session(LINE, device_simulator.run_tcp_device, count=2)
    ctx.menu_hooks = ["plugins.example_menu"]
    ctx.workflow_operations = "menu"
    page = WorkflowPage(ctx)
    qtbot.addWidget(page)
    step = page.operations_step
    assert isinstance(step, OperationsMenuPage) and step.options
    step.set_targets([peers[1]])
    assert step.ticked_peers() == [peers[1]]
    runs = step.run_option(step.options[0])
    qtbot.waitUntil(lambda: not step.runs, timeout=T)
    assert [run.state for run in runs] == ["done"]

    ctx.workflow_operations = "operations"
    default = WorkflowPage(ctx)
    qtbot.addWidget(default)
    assert isinstance(default.operations_step, OperationsStep)


def test_workflow_operations_setting_is_validated(tmp_path):
    """project.json accepts "operations", "menu" or "module:Class" for workflow_operations."""
    from app.project import ProjectError, load_project

    good = tmp_path / "good.json"
    good.write_text(json.dumps({"workflow_operations": "menu", "menu_hooks": ["plugins.example_menu"]}))
    assert load_project(good).workflow_operations == "menu"
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps({"workflow_operations": "plugins.ops:OpsPage"}))
    assert load_project(custom).workflow_operations == "plugins.ops:OpsPage"
    for value in ("hooks", "plugins.ops:", ":OpsPage"):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"workflow_operations": value}))
        with pytest.raises(ProjectError, match="workflow_operations"):
            load_project(bad)


MENU_TEMPLATES = ["menu_console", "menu_buttons", "menu_form", "menu_sequence"]


def _generate_menu_page(tmp_path: Path, template: str) -> str:
    """Generate a page from a menu template and import it under a unique module name.

    Args:
        tmp_path: Project root to generate into.
        template: Template name.

    Returns:
        The ``"module:Class"`` value for ``workflow_operations``.
    """
    import importlib.util
    import subprocess

    subprocess.run([sys.executable, str(ROOT / "tools" / "new_page.py"), "sample", "Sample", "--template", template,
                    "--root", str(tmp_path), "--no-enable"], check=True, capture_output=True)
    name = f"gen_{template}"
    spec = importlib.util.spec_from_file_location(name, tmp_path / "plugins" / "sample.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return f"{name}:SamplePage"


@pytest.mark.parametrize("template", MENU_TEMPLATES)
def test_menu_templates_run_hooks_as_workflow_step_4(qtbot, ctx, session, tmp_path, template):
    """Each menu template drops into workflow step 4 and runs hooks on the devices, its own way."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    from app.ui.pages.workflow import WorkflowPage
    from app.ui.styles import theme

    session(LINE, device_simulator.run_tcp_device, count=2)
    ctx.menu_hooks = ["plugins.example_menu"]
    ctx.workflow_operations = _generate_menu_page(tmp_path, template)
    page = WorkflowPage(ctx)
    qtbot.addWidget(page)
    page.show()
    step = page.operations_step
    assert type(step).__name__ == "SamplePage" and len(step.options) == 8
    title = step.findChild(QLabel, "PageTitle")
    assert title is None or title.isHidden()                       # embedded: no page title
    titles = [o.title for o in step.options]

    def settle() -> None:
        """Wait until every hook run has finished."""
        qtbot.waitUntil(lambda: not step.runs, timeout=T * 2)

    if template == "menu_console":
        step.entry.setText(str(titles.index("Ping") + 1))
        step.submit()
        settle()
        step.entry.setText(str(titles.index("Blink LED") + 1))
        step.submit()
        assert step.prompt.text().startswith("Blinks")            # inputs are asked one by one
        for typed in ("abc", "2", "60"):                           # a bad value, then blinks, then period
            step.entry.setText(typed)
            step.submit()
        settle()
        text = step.log.toPlainText()
        assert text.count("Ping: done") == 2 and text.count("Blink LED: done") == 2
        assert "invalid literal" in text
    elif template == "menu_buttons":
        step.buttons["Ping"].click()
        settle()
        assert theme.OK in step.buttons["Ping"].styleSheet()
        step.run_option(step.options[titles.index("Health check")], {"max_temp": 0.0})
        settle()
        assert theme.ERROR in step.buttons["Health check"].styleSheet()
    elif template == "menu_form":
        for row in range(step.list.count()):
            if step.list.item(row).data(Qt.ItemDataRole.UserRole) == titles.index("Health check"):
                step.list.setCurrentRow(row)
        assert step.title.text() == "Health check" and "max_temp" in step.form.inputs
        step.form.inputs["max_temp"].setValue(150.0)
        step.run_button.click()
        settle()
        assert step.results.rowCount() == 2
        assert {step.results.item(r, 3).text() for r in range(2)} == {"DONE"}
    else:  # menu_sequence
        by = {o.title: o for o in step.options}
        step.add_step(by["Ping"], {})
        step.add_step(by["Set LED"], {"state": "ON"})
        step.add_step(by["Health check"], {"max_temp": 0.0})     # fails...
        step.add_step(by["Ping"], {})                             # ...so this is skipped
        step.run_sequence()
        qtbot.waitUntil(lambda: all(step.grid.item(r, 3) is not None and step.grid.item(r, 3).text() == "skipped"
                                    for r in range(2)), timeout=T * 2)
        assert [step.grid.item(0, c).text() for c in range(4)] == ["DONE", "DONE", "FAILED", "skipped"]
        saved = tmp_path / "seq.json"
        saved.write_text(json.dumps([{"option": "Ping", "values": {}}, {"option": "Nope", "values": {}}]))
        step.load_sequence(str(saved))
        assert [o.title for o, _v in step.steps] == ["Ping"]

    # the workflow's right-click "Run operation" path works on every template
    step.set_targets([d.peer for d in ctx.workflow.connected_devices()][:1])
    assert len(step.run_option(step.options[titles.index("Ping")], {})) == 1
    settle()


def test_new_page_workflow_option_sets_step_4(tmp_path):
    """--workflow points workflow_operations at the new page instead of adding a tab."""
    import subprocess

    (tmp_path / "project.json").write_text(json.dumps({"plugins": [], "pages": ["workflow"]}))
    subprocess.run([sys.executable, str(ROOT / "tools" / "new_page.py"), "ops", "Ops", "--template", "menu_form",
                    "--workflow", "--root", str(tmp_path)], check=True, capture_output=True)
    data = json.loads((tmp_path / "project.json").read_text())
    assert data == {"plugins": [], "pages": ["workflow"], "workflow_operations": "plugins.ops:OpsPage"}


def test_unloadable_step_4_falls_back_to_the_operations_step(qtbot, ctx):
    """A wrong "module:Class" is logged and the fixed-operations step is used."""
    from app.ui.pages.workflow import OperationsStep, operations_step_for

    ctx.workflow_operations = "no_such_module:Page"
    assert isinstance(operations_step_for(ctx), OperationsStep)
