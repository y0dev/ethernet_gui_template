"""The examples/ folder works as documented."""

from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path

import pytest

from app.context import AppContext
from app.core.compatibility.crc import CRC8, Crc
from app.core.config import AppConfig
from app.core.models import Transport

ROOT = Path(__file__).resolve().parents[2]
LED_EXAMPLE = ROOT / "examples" / "led_device"
sys.path.insert(0, str(LED_EXAMPLE))
import led_device_sim  # noqa: E402
import make_command_set  # noqa: E402

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


def _free_port() -> int:
    """Find a local TCP port that is currently free.

    Returns:
        The port number.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_led_command_set_is_up_to_date():
    """The committed led_device.json is exactly what make_command_set.py generates."""
    committed = json.loads((LED_EXAMPLE / "led_device.json").read_text(encoding="utf-8"))
    assert committed == make_command_set.build_command_set()


def test_led_simulator_crc_matches_the_compatibility_layer():
    """The simulator's firmware-style CRC equals the CRC-8/SMBUS the generator uses."""
    engine = Crc(CRC8)
    for data in (b"", b"\x01\x01", bytes(range(200)), b"123456789"):
        assert led_device_sim.crc8(data) == engine.compute(data)
    frame = led_device_sim.encode_frame(0x82, b"SN=1")
    assert led_device_sim.split_frames(bytearray(frame + frame[:2])) == [frame]


def test_led_menu_runs_as_a_workflow(qtbot, ctx):
    """Every C menu entry runs as an operation on several simulated devices and passes.

    Args:
        qtbot: pytest-qt robot fixture.
        ctx: Application context.
    """
    command_set = json.loads((LED_EXAMPLE / "led_device.json").read_text(encoding="utf-8"))
    wf = ctx.workflow
    port = _free_port()
    wf.start_server("127.0.0.1", port, Transport.TCP_SERVER, command_set["framing"])
    wf.use_current_database()
    assert wf.import_operations(command_set["operations"]) == len(command_set["operations"])

    stop = threading.Event()
    threads = [threading.Thread(target=led_device_sim.run_device, args=("127.0.0.1", port, f"LED-{i}", stop),
                                daemon=True) for i in range(1, 4)]
    for thread in threads:
        thread.start()
    try:
        qtbot.waitUntil(lambda: len(wf.connected_devices()) == 3, timeout=T)
        wf.start_session("LED menu")
        results = []
        ctx.operations.result_ready.connect(results.append)
        operations = wf.list_operations()
        queued = wf.run_operations([d.peer for d in wf.connected_devices()], operations)
        qtbot.waitUntil(lambda: len(results) == queued, timeout=T * 2)
        statuses = {(r["operation_name"], r["status"]) for r in results}
        assert {status for _name, status in statuses} == {"passed", "sent"}
        assert ("Reset", "sent") in statuses
        info = [bytes(r["response"]) for r in results if r["operation_name"] == "Send device info"]
        # stored responses are the raw frames: LEN | 0x82 | "SN=..." | CRC
        assert sorted(reply[2:11] for reply in info) == [b"SN=LED-1;", b"SN=LED-2;", b"SN=LED-3;"]
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=3)
