"""Test execution: device request/response cases and the pytest suites.

* :meth:`TestRunner.run_case` sends a stored request on a live connection and
  compares the first received frame byte-for-byte with the expected response.
* :meth:`TestRunner.run_pytest` runs the project test suite in a separate
  process (never blocking the GUI) and records each JUnit result.

Results are only ever recorded from tests that actually ran.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from ..config import PROJECT_ROOT
from ..database import Database, TestCaseRepository, TestResultRepository
from ..hexutil import format_hex
from ..models import Direction, Packet
from .connection_manager import ConnectionManager

log = logging.getLogger(__name__)


@dataclass
class SuiteSummary:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    exit_code: int = 0
    output: str = ""

    @property
    def total(self) -> int:
        """Number of tests counted in every status."""
        return self.passed + self.failed + self.errors + self.skipped


def parse_junit(path: Path) -> list[dict[str, Any]]:
    """Flatten a JUnit XML report into one dict per test case.

    Args:
        path: JUnit XML file written by pytest.

    Returns:
        Dicts with ``suite``, ``name``, ``status`` (``passed``, ``failed``,
        ``error`` or ``skipped``), ``message`` and ``duration_ms``.
    """
    root = ET.parse(path).getroot()
    results = []
    for case in root.iter("testcase"):
        status, message = "passed", ""
        for tag, label in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            node = case.find(tag)
            if node is not None:
                status, message = label, (node.get("message") or node.text or "").strip()
                break
        results.append(
            {
                "suite": case.get("classname", ""),
                "name": case.get("name", ""),
                "status": status,
                "message": message[:4000],
                "duration_ms": float(case.get("time", "0") or 0) * 1000.0,
            }
        )
    return results


class TestRunner(QObject):
    __test__ = False

    case_finished = Signal(int, str, str)     # case id, status, message
    suite_output = Signal(str)
    suite_finished = Signal(object)           # SuiteSummary

    def __init__(self, db: Database, connections: ConnectionManager, parent: QObject | None = None) -> None:
        """Create the runner and start listening for received packets.

        Args:
            db: Shared database for test cases and results.
            connections: Used to send requests and receive responses.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self.cases = TestCaseRepository(db)
        self.results = TestResultRepository(db)
        self.connections = connections
        self._active: dict[int, dict[str, Any]] = {}
        self._process: QProcess | None = None
        self._finished_process: QProcess | None = None
        self._junit: Path | None = None
        self._output: list[str] = []
        connections.packet.connect(self._on_packet)

    # -- device test cases ------------------------------------------------
    def save_case(
        self, name: str, request: bytes, expected: bytes | None, connection_id: int | None,
        timeout_ms: int = 2000, description: str = "", case_id: int | None = None,
    ) -> int:
        """Validate and store a device request/response test case.

        Args:
            name: Case name.
            request: Bytes to send.
            expected: Exact expected response, or None if no response is expected.
            connection_id: Connection the case runs on.
            timeout_ms: How long to wait for the response.
            description: Free-text description.
            case_id: Id of an existing case to update, or None to insert.

        Returns:
            The case id.

        Raises:
            ValueError: If the name or request is empty.
        """
        if not name.strip():
            raise ValueError("test case name is required")
        if not request:
            raise ValueError("request must not be empty")
        values = dict(
            name=name.strip(), request=request, expected_response=expected,
            connection_id=connection_id, timeout_ms=timeout_ms, description=description,
        )
        if case_id is None:
            return self.cases.insert_row(**values)
        self.cases.update_row(case_id, **values)
        return case_id

    def run_case(self, case_id: int) -> None:
        """Send a test case's request and wait asynchronously for the response.

        The result is recorded and emitted through ``case_finished``. A case
        that is already running is ignored.

        Args:
            case_id: Test case id.

        Raises:
            KeyError: If the case does not exist.
        """
        case = self.cases.get_row(case_id)
        if case is None:
            raise KeyError(f"test case {case_id} not found")
        if case_id in self._active:
            return
        connection_id = case["connection_id"]
        if connection_id is None or self.connections.endpoint(connection_id) is None:
            self._record_case_result(case_id, "error", "connection is not open", None, 0.0)
            return
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._on_timeout(case_id))
        self._active[case_id] = {"case": case, "start": time.perf_counter(), "timer": timer}
        if not self.connections.send(connection_id, bytes(case["request"])):
            self._active.pop(case_id)
            self._record_case_result(case_id, "error", "request could not be sent", None, 0.0)
            return
        if case["expected_response"] is None:
            self._active.pop(case_id)
            self._record_case_result(case_id, "passed", "request sent (no response expected)", None, 0.0)
            return
        timer.start(case["timeout_ms"])

    def _on_packet(self, packet: Packet) -> None:
        """Compare a received frame with the expected response of active cases.

        Args:
            packet: Packet from the connection manager; only RX packets on an
                active case's connection are used.
        """
        if packet.direction is not Direction.RX:
            return
        for case_id, state in list(self._active.items()):
            if state["case"]["connection_id"] != packet.connection_id:
                continue
            state["timer"].stop()
            self._active.pop(case_id)
            elapsed = (time.perf_counter() - state["start"]) * 1000.0
            expected = bytes(state["case"]["expected_response"])
            if packet.data == expected:
                self._record_case_result(case_id, "passed", "response matches", packet.data, elapsed)
            else:
                self._record_case_result(
                    case_id, "failed",
                    f"expected {format_hex(expected)} got {format_hex(packet.data)}",
                    packet.data, elapsed,
                )

    def _on_timeout(self, case_id: int) -> None:
        """Fail a case whose response did not arrive in time.

        Args:
            case_id: Test case id.
        """
        state = self._active.pop(case_id, None)
        if state:
            self._record_case_result(case_id, "failed", f"no response within {state['case']['timeout_ms']} ms", None,
                         float(state["case"]["timeout_ms"]))

    def _record_case_result(self, case_id: int, status: str, message: str, actual: bytes | None, elapsed: float) -> None:
        """Record a case result and emit ``case_finished``.

        Args:
            case_id: Test case id.
            status: ``passed``, ``failed`` or ``error``.
            message: Human-readable result detail.
            actual: Received bytes, or None.
            elapsed: Time to the response in milliseconds.
        """
        self.results.record(
            status, test_case_id=case_id, suite="device", actual_response=actual,
            message=message, duration_ms=elapsed,
        )
        self.case_finished.emit(case_id, status, message)

    # -- pytest suites ----------------------------------------------------
    @property
    def suite_running(self) -> bool:
        """True while a pytest run is in progress."""
        return self._process is not None

    def run_pytest(self, target: str = "tests", extra_args: list[str] | None = None) -> None:
        """Start pytest in a child process without blocking the GUI.

        Output is streamed through ``suite_output`` and the summary arrives via
        ``suite_finished``.

        Args:
            target: Test path or node id passed to pytest.
            extra_args: Additional pytest arguments.

        Raises:
            RuntimeError: If a run is already in progress.
        """
        if self._process is not None:
            raise RuntimeError("a test run is already in progress")
        if self._finished_process is not None:
            # Released here rather than with deleteLater() inside its own
            # finished() handler: deleting a QProcess from within its signal
            # can crash if the runner is destroyed before the deferred delete.
            self._finished_process.setParent(None)
            self._finished_process = None
        self._junit = Path(tempfile.mkdtemp(prefix="ewb_tests_")) / "junit.xml"
        self._output = []
        process = QProcess(self)
        process.setWorkingDirectory(str(PROJECT_ROOT))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_suite_output)
        process.finished.connect(self._on_suite_finished)
        self._process = process
        # A private --basetemp stops the child pytest from pruning the
        # temporary directories of any other pytest run (including the one
        # that may have launched this application).
        args = [
            "-m", "pytest", target, "-q", f"--junitxml={self._junit}",
            f"--basetemp={self._junit.parent / 'tmp'}", *(extra_args or []),
        ]
        log.info("starting pytest", extra={"context": {"args": args}})
        process.start(sys.executable, args)

    def _read_suite_output(self) -> None:
        """Forward the child process's new output through ``suite_output``."""
        if self._process is None:
            return
        text = bytes(self._process.readAllStandardOutput().data()).decode(errors="replace")
        if text:
            self._output.append(text)
            self.suite_output.emit(text)

    def _on_suite_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        """Record every JUnit result of the finished run and emit the summary.

        Args:
            exit_code: pytest's exit code.
            _status: Qt exit status (unused).
        """
        self._read_suite_output()
        summary = SuiteSummary(exit_code=exit_code, output="".join(self._output))
        if self._junit is not None and self._junit.exists():
            for result in parse_junit(self._junit):
                self.results.record(
                    result["status"], suite=result["suite"], name=result["name"],
                    message=result["message"], duration_ms=result["duration_ms"],
                )
                if result["status"] == "passed":
                    summary.passed += 1
                elif result["status"] == "failed":
                    summary.failed += 1
                elif result["status"] == "error":
                    summary.errors += 1
                else:
                    summary.skipped += 1
        else:
            log.error("pytest produced no result file", extra={"context": {"exit_code": exit_code}})
        self._finished_process, self._process = self._process, None
        self.suite_finished.emit(summary)

    def cancel_suite(self) -> None:
        """Kill the running pytest process, if any."""
        if self._process is not None:
            self._process.kill()
