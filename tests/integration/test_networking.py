"""Loopback tests for the Qt network endpoints (real sockets on 127.0.0.1)."""

from __future__ import annotations

import socket

import pytest

from app.core.compatibility.framing import DelimiterFramer, LengthPrefixedFramer
from app.core.models import ConnectionConfig, ConnectionState, Direction, Transport
from app.core.networking import TcpClient, TcpServer, UdpClient, UdpServer
from app.core.services import CaptureService, ConnectionManager

TIMEOUT = 5000


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def tcp_pair(qtbot):
    server = TcpServer(0, "127.0.0.1", lambda: LengthPrefixedFramer(2))
    server.open()
    assert server.state is ConnectionState.LISTENING
    client = TcpClient("127.0.0.1", server.listening_port(), LengthPrefixedFramer(2), timeout_ms=2000)
    with qtbot.waitSignal(server.peer_changed, timeout=TIMEOUT):
        client.open()
    qtbot.waitUntil(lambda: client.state is ConnectionState.CONNECTED, timeout=TIMEOUT)
    yield server, client
    client.close()
    server.close()


def test_tcp_round_trip_with_framing(qtbot, tcp_pair):
    server, client = tcp_pair
    received = []
    server.frame_received.connect(lambda f, p: received.append(f))
    client.send(b"hello")
    client.send(b"world!")
    qtbot.waitUntil(lambda: len(received) == 2, timeout=TIMEOUT)
    assert received == [b"\x00\x05hello", b"\x00\x06world!"]
    assert client.tx_frames == 2 and client.tx_bytes == 15
    assert server.rx_bytes == 15

    replies = []
    client.frame_received.connect(lambda f, p: replies.append((f, p)))
    assert server.send(b"ack")
    qtbot.waitUntil(lambda: len(replies) == 1, timeout=TIMEOUT)
    assert replies[0][0] == b"\x00\x03ack"


def test_tcp_stream_reassembly_from_raw_socket(qtbot):
    """A message split across many writes still arrives as one frame."""
    server = TcpServer(0, "127.0.0.1", lambda: DelimiterFramer(b"\n"))
    server.open()
    frames = []
    server.frame_received.connect(lambda f, p: frames.append(f))
    raw = socket.create_connection(("127.0.0.1", server.listening_port()))
    try:
        for chunk in (b"par", b"tial\nsec", b"ond\nthi", b"rd"):
            raw.sendall(chunk)
            qtbot.wait(20)
        qtbot.waitUntil(lambda: len(frames) == 2, timeout=TIMEOUT)
        assert frames == [b"partial\n", b"second\n"]
    finally:
        raw.close()
        server.close()


def test_tcp_connect_failure_reports_error(qtbot):
    client = TcpClient("127.0.0.1", free_port(), timeout_ms=2000)
    with qtbot.waitSignal(client.error_occurred, timeout=TIMEOUT):
        client.open()
    qtbot.waitUntil(lambda: client.state is ConnectionState.ERROR, timeout=TIMEOUT)
    assert client.error_count >= 1
    assert not client.send(b"x")
    client.close()


def test_tcp_auto_reconnect(qtbot):
    port = free_port()
    client = TcpClient("127.0.0.1", port, timeout_ms=1000, auto_reconnect=True, reconnect_interval_ms=200)
    client.open()
    qtbot.waitUntil(lambda: client.state is ConnectionState.ERROR, timeout=TIMEOUT)
    server = TcpServer(port, "127.0.0.1")
    server.open()
    try:
        qtbot.waitUntil(lambda: client.state is ConnectionState.CONNECTED, timeout=TIMEOUT)
    finally:
        client.close()
        server.close()


def test_udp_round_trip(qtbot):
    server = UdpServer(0, "127.0.0.1")
    server.open()
    assert server.state is ConnectionState.LISTENING
    client = UdpClient("127.0.0.1", server.bound_port())
    client.open()
    assert client.state is ConnectionState.CONNECTED
    try:
        with qtbot.waitSignal(server.frame_received, timeout=TIMEOUT) as blocker:
            client.send(b"\x01\x02\x03")
        frame, peer = blocker.args
        assert frame == b"\x01\x02\x03"
        assert peer.endswith(f":{client.bound_port()}")
        with qtbot.waitSignal(client.frame_received, timeout=TIMEOUT) as reply:
            assert server.send(b"pong")  # goes to last sender
        assert reply.args[0] == b"pong"
        assert server.peers() == [peer]
    finally:
        client.close()
        server.close()


def test_udp_server_without_peer_cannot_send():
    server = UdpServer(0, "127.0.0.1")
    server.open()
    try:
        assert not server.send(b"x")
    finally:
        server.close()


def test_udp_client_rejects_hostname():
    client = UdpClient("not-an-ip.example", 9)
    client.open()
    assert client.state is ConnectionState.ERROR


def test_manager_capture_and_device_test_case(qtbot, db, tmp_path):
    """Two managed connections talk; capture records both directions to SQLite."""
    from app.core.services import TestRunner  # Test Bench feature

    manager = ConnectionManager(db)
    capture = CaptureService(db, manager, flush_interval_ms=50, refresh_interval_ms=20)
    runner = TestRunner(db, manager)
    port = free_port()
    framing = {"type": "delimiter", "delimiter_hex": "0a"}
    srv = manager.save_config(ConnectionConfig("srv", Transport.TCP_SERVER, "127.0.0.1", local_port=port, framing=framing))
    cli = manager.save_config(ConnectionConfig("cli", Transport.TCP_CLIENT, "127.0.0.1", port, framing=framing))
    server_ep = manager.open(srv.id)
    # echo server: reply to each request with "OK"
    server_ep.frame_received.connect(lambda f, p: server_ep.send(b"OK", p))
    session = capture.start_recording("loopback")
    manager.open(cli.id)
    qtbot.waitUntil(lambda: manager.state(cli.id) is ConnectionState.CONNECTED, timeout=TIMEOUT)

    case_pass = runner.save_case("ping", b"PING", b"OK\n", cli.id, timeout_ms=2000)
    case_fail = runner.save_case("wrong", b"PING", b"NO\n", cli.id, timeout_ms=2000)
    with qtbot.waitSignal(runner.case_finished, timeout=TIMEOUT) as first:
        runner.run_case(case_pass)
    assert first.args[1] == "passed"
    with qtbot.waitSignal(runner.case_finished, timeout=TIMEOUT) as second:
        runner.run_case(case_fail)
    assert second.args[1] == "failed"

    qtbot.waitUntil(lambda: len(capture.live) >= 8, timeout=TIMEOUT)  # 4 frames seen on each side
    capture.stop_recording()
    stored = capture.load_session(session)
    assert len(stored) == len(capture.live)
    assert {p.direction for p in stored} == {Direction.RX, Direction.TX}
    assert b"PING\n" in {p.data for p in stored}
    statuses = [r["status"] for r in runner.results.recent()]
    assert sorted(statuses) == ["failed", "passed"]

    out = tmp_path / "export.csv"
    assert CaptureService.export(stored, out) == len(stored)
    assert "50494E470A" in out.read_text()
    manager.close_all()
    capture.shutdown()
