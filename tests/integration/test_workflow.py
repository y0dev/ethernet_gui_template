"""End-to-end session workflow with simulated devices on loopback."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from app.context import AppContext
from app.core.config import AppConfig
from app.core.models import DeviceOperation, MatchMode, PayloadFormat, Transport
from app.core.reporting import collect_session_data, render_csv, render_html, render_markdown
from app.core.services import WorkflowError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import device_simulator  # noqa: E402

DEMO_SET = ROOT / "resources" / "command_sets" / "demo_simulator.json"
LINE = {"type": "delimiter", "delimiter_hex": "0a"}
T = 5000


@pytest.fixture
def ctx(qtbot, tmp_path):
    from PySide6.QtCore import QCoreApplication, QEvent, QObject

    owner = QObject()  # owns every service so they are destroyed deterministically
    config = AppConfig(database_path=str(tmp_path / "main.db"), log_dir=str(tmp_path / "logs"),
                       backup_dir=str(tmp_path / "bk"), reports_dir=str(tmp_path / "reports"))
    context = AppContext.create(config, tmp_path / "config.json", owner)
    yield context
    context.shutdown()
    qtbot.wait(50)
    owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def devices():
    stop = threading.Event()
    threads: list[threading.Thread] = []

    def start(port: int, count: int, delay_ms: int = 0) -> None:
        for i in range(count):
            t = threading.Thread(
                target=device_simulator.run_tcp_device,
                args=("127.0.0.1", port, f"SIM-{i + 1:04d}", stop, delay_ms), daemon=True,
            )
            t.start()
            threads.append(t)

    yield start
    stop.set()
    for t in threads:
        t.join(timeout=3)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_operation_rules():
    op = DeviceOperation("x", "PING\\n", PayloadFormat.ASCII, MatchMode.PREFIX, "PO")
    assert op.payload == b"PING\n" and op.judge(b"PONG\n") and not op.judge(b"NO")
    assert DeviceOperation("x", "01", match_mode=MatchMode.CONTAINS, expected_text="02").judge(b"\x01\x02\x03")
    assert DeviceOperation("x", "01", match_mode=MatchMode.EXACT, expected_text="01").judge(b"\x01")
    with pytest.raises(ValueError):
        DeviceOperation("x", "01", match_mode=MatchMode.EXACT).validate()
    with pytest.raises(ValueError):
        DeviceOperation("", "01").validate()
    with pytest.raises(ValueError):
        DeviceOperation("x", "").validate()


def test_full_workflow(qtbot, ctx, devices, tmp_path):
    wf = ctx.workflow
    port = _free_port()

    # Step 1: server
    with pytest.raises(WorkflowError):
        wf.start_session("too early")
    wf.start_server("127.0.0.1", port, Transport.TCP_SERVER, LINE)
    assert wf.server_running and wf.server_address() == f"127.0.0.1:{port}"
    assert ctx.config.workflow_server_port == port            # remembered in config
    assert json.loads((tmp_path / "config.json").read_text())["workflow_server_port"] == port

    # Step 2: database (switch to a new file)
    session_db = tmp_path / "sessions" / "bench.db"
    wf.open_database(session_db)
    assert ctx.db.path == session_db.resolve()
    assert wf.database_info()["sessions"] == 0
    assert wf.import_operations(json.loads(DEMO_SET.read_text())["operations"]) == 8

    # Step 3: devices connect, session starts
    devices(port, 3)
    qtbot.waitUntil(lambda: len(wf.connected_devices()) == 3, timeout=T)
    session_id = wf.start_session("bench run")
    with pytest.raises(WorkflowError):
        wf.open_database(tmp_path / "other.db")               # not while recording
    peers = [d.peer for d in wf.connected_devices()]
    wf.rename_device(peers[0], "Unit A", save_to_inventory=True)
    assert [d.name for d in ctx.devices.list()] == ["Unit A"]

    # Step 4: operations menu, sequence on every device
    ops = {op.name: op for op in wf.list_operations()}
    sequence = [ops["Ping"], ops["Read ID"], ops["Read status"], ops["Unknown command (expect error)"]]
    results = []
    ctx.operations.result_ready.connect(results.append)
    assert wf.run_operations(peers, sequence) == 12
    qtbot.waitUntil(lambda: len(results) == 12, timeout=T)
    assert {r["status"] for r in results} == {"passed"}, [(r["operation_name"], r["response"], r["message"]) for r in results if r["status"] != "passed"]
    # each device got its own replies, in order
    for peer in peers:
        names = [r["operation_name"] for r in results if r["peer"] == peer]
        assert names == [op.name for op in sequence]
    id_replies = {bytes(r["response"]) for r in results if r["operation_name"] == "Read ID"}
    assert id_replies == {b"ID SIM-0001\n", b"ID SIM-0002\n", b"ID SIM-0003\n"}

    # a failing rule and a timeout are reported as such
    wrong = DeviceOperation("Wrong", "PING", PayloadFormat.ASCII, MatchMode.EXACT, "NOPE", timeout_ms=1000)
    silent = DeviceOperation("Silent", "HELLO?", PayloadFormat.ASCII, MatchMode.ANY, timeout_ms=300)
    wf.run_operations([peers[0]], [wrong])
    qtbot.waitUntil(lambda: len(results) == 13, timeout=T)
    assert results[-1]["status"] == "failed"
    # the simulator answers every line, so this gets "ERR UNKNOWN" and passes "any"
    wf.run_operations([peers[0]], [silent])
    qtbot.waitUntil(lambda: len(results) == 14, timeout=T)
    assert results[-1]["status"] == "passed"

    # device disconnect mid-session is tracked
    wf.disconnect_device(peers[2])
    qtbot.waitUntil(lambda: len(wf.connected_devices()) == 2, timeout=T)

    # Step 5: report
    qtbot.wait(400)   # let the capture flush
    ended = wf.end_session()
    assert ended == session_id
    data = collect_session_data(ctx.db, session_id)
    assert data["counts"]["passed"] == 13 and data["counts"]["failed"] == 1
    assert len(data["devices"]) == 3
    unit_a = next(d for d in data["devices"] if d["peer"] == peers[0])
    assert unit_a["name"] == "Unit A" and unit_a["rx_frames"] == 6 and unit_a["tx_frames"] == 6
    gone = next(d for d in data["devices"] if d["peer"] == peers[2])
    assert gone["disconnected_at"] is not None
    md = render_markdown(data)
    assert "# Session Report: bench run" in md and "Unit A" in md and "FAILED" in md
    page = render_html(data)
    assert page.startswith("<!doctype html>") and "Pass rate" in page
    csv_text = render_csv(data)
    assert csv_text.count("\n") == 15   # header + 14 results

    # the main database was not touched by the session
    wf.open_database(tmp_path / "main.db")
    assert wf.database_info()["sessions"] == 0


def test_runner_timeout_and_disconnect(qtbot, ctx, devices):
    wf = ctx.workflow
    port = _free_port()
    wf.start_server("127.0.0.1", port, Transport.TCP_SERVER, LINE)
    wf.use_current_database()
    devices(port, 1, delay_ms=600)
    qtbot.waitUntil(lambda: len(wf.connected_devices()) == 1, timeout=T)
    wf.start_session("slow")
    peer = wf.connected_devices()[0].peer
    results = []
    ctx.operations.result_ready.connect(results.append)
    slow = DeviceOperation("Ping", "PING", PayloadFormat.ASCII, MatchMode.EXACT, "PONG", timeout_ms=200)
    wf.run_operations([peer], [slow, slow, slow])
    qtbot.waitUntil(lambda: len(results) >= 1, timeout=T)
    assert results[0]["status"] == "timeout"
    wf.disconnect_device(peer)
    qtbot.waitUntil(lambda: ctx.operations.pending() == 0, timeout=T)
    assert {r["status"] for r in results[1:]} <= {"timeout", "error", "failed", "passed"}
    assert ctx.operations.pending() == 0


def test_server_port_in_use(ctx):
    import socket

    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        with pytest.raises(WorkflowError):
            ctx.workflow.start_server("127.0.0.1", port, Transport.TCP_SERVER, LINE)
    assert ctx.workflow.server_id is None


def test_workflow_page_end_to_end(qtbot, ctx, devices, monkeypatch, tmp_path):
    """Drive the five steps through the actual widgets."""
    from PySide6.QtWidgets import QMessageBox

    from app.ui.pages.workflow import WorkflowPage

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    page = WorkflowPage(ctx)
    qtbot.addWidget(page)
    page.show()
    assert page.stack.currentIndex() == 0
    page.go_to(2)
    assert page.stack.currentIndex() == 0          # locked until the server runs

    # 1 server
    step = page.server_step
    port = _free_port()
    step.host.setEditText("127.0.0.1")
    step.port.setValue(port)
    step.framing.kind.setCurrentIndex(step.framing.kind.findData("delimiter"))
    step.framing.delimiter.setText("0a")
    step.start()
    assert ctx.workflow.server_running
    page.next.click()
    assert page.stack.currentIndex() == 1

    # 2 database
    page.database_step._open(str(tmp_path / "campaign.db"))
    assert ctx.db.path.name == "campaign.db"
    page.next.click()
    assert page.stack.currentIndex() == 2

    # 3 devices + session
    devices(port, 2)
    qtbot.waitUntil(lambda: page.devices_step.model.rowCount() == 2, timeout=T)
    assert not page.next.isEnabled()
    page.devices_step.session_name.setText("GUI run")
    page.devices_step.start_session()
    assert ctx.workflow.session_id is not None
    page.next.click()
    assert page.stack.currentIndex() == 3

    # 4 operations
    ops_step = page.operations_step
    ctx.workflow.import_operations(json.loads(DEMO_SET.read_text())["operations"])
    ops_step.refresh_ops()
    ops_step._check_all(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.CheckState.Checked)
    assert len(ops_step.target_peers()) == 2
    items = [it for it in ops_step._all_items() if it.text(0) in ("Ping", "Read ID")]
    for it in items:
        it.setSelected(True)
    ops_step.run_button.click()
    qtbot.waitUntil(lambda: ops_step.results_model.rowCount() == 4, timeout=T)
    statuses = [r["status"] for r in ops_step.results_model.records]
    assert statuses == ["passed"] * 4
    ops_step.custom_text.setText("VERSION?")
    ops_step.send_custom()
    qtbot.waitUntil(lambda: ops_step.results_model.rowCount() == 6, timeout=T)

    # 5 report
    page.next.click()
    assert page.stack.currentIndex() == 4
    report = page.report_step
    report.finish_session()                     # question -> No: keep the server running
    assert ctx.workflow.session_id is None and ctx.workflow.server_running
    files = sorted(p.name for p in (tmp_path / "reports").iterdir())
    assert len(files) == 3 and any(f.endswith(".html") for f in files)
    assert "GUI run" in report.browser.toPlainText()


def test_udp_server_workflow(qtbot, ctx):
    wf = ctx.workflow
    port = _free_port()
    wf.start_server("127.0.0.1", port, Transport.UDP_SERVER, {"type": "raw"})
    wf.use_current_database()
    stop = threading.Event()
    t = threading.Thread(target=device_simulator.run_udp_device, args=("127.0.0.1", port, "UDP-1", stop), daemon=True)
    t.start()
    try:
        qtbot.waitUntil(lambda: len(wf.connected_devices()) == 1, timeout=T)   # HELLO datagram
        wf.start_session("udp")
        results = []
        ctx.operations.result_ready.connect(results.append)
        ping = DeviceOperation("Ping", "PING", PayloadFormat.ASCII, MatchMode.PREFIX, "PONG")
        wf.run_operations([wf.connected_devices()[0].peer], [ping])
        qtbot.waitUntil(lambda: len(results) == 1, timeout=T)
        assert results[0]["status"] == "passed"
        with pytest.raises(WorkflowError):
            wf.disconnect_device(wf.connected_devices()[0].peer)    # UDP has no connection
    finally:
        stop.set()
        t.join(timeout=3)
