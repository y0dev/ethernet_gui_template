"""Demo device simulator for trying the Session Workflow without hardware.

Each simulated device connects to the workbench server and answers a tiny
line-based DEMO protocol (one command per line, ``\\n`` terminated).  This is
not a real device protocol; it exists only to exercise the workflow, and it
matches ``resources/command_sets/demo_simulator.json``.

    PING          -> PONG
    ID?           -> ID <device id>
    VERSION?      -> VERSION 1.0.0-sim
    STATUS?       -> STATUS OK TEMP=<value> UPTIME=<seconds>
    LED ON|OFF    -> OK LED ON|OFF
    RESET         -> OK RESET
    anything else -> ERR UNKNOWN <command>

Usage (with the workflow server listening, framing "Delimiter 0a"):

    python tools/device_simulator.py --host 127.0.0.1 --port 5000 --count 3
    python tools/device_simulator.py --udp --port 5000        # UDP server mode
"""

from __future__ import annotations

import argparse
import random
import socket
import threading
import time


def respond(line: str, device_id: str, started: float, state: dict) -> str:
    """Answer one DEMO protocol command.

    Args:
        line: Received command line.
        device_id: Id reported by ``ID?``.
        started: ``time.monotonic()`` value when the device started, used for
            ``UPTIME``.
        state: Per-device state; ``LED`` sets it and ``RESET`` clears it.

    Returns:
        The reply line, without the newline.
    """
    command = line.strip()
    upper = command.upper()
    if upper == "PING":
        return "PONG"
    if upper == "ID?":
        return f"ID {device_id}"
    if upper == "VERSION?":
        return "VERSION 1.0.0-sim"
    if upper == "STATUS?":
        temp = 22.0 + random.random() * 4
        return f"STATUS OK TEMP={temp:.1f} UPTIME={int(time.monotonic() - started)}"
    if upper in ("LED ON", "LED OFF"):
        state["led"] = upper.endswith("ON")
        return f"OK {upper}"
    if upper == "RESET":
        state.clear()
        return "OK RESET"
    return f"ERR UNKNOWN {command}"


def run_tcp_device(host: str, port: int, device_id: str, stop: threading.Event, delay_ms: int = 0) -> None:
    """Run one simulated TCP device until stopped or disconnected.

    Args:
        host: Workbench server host.
        port: Workbench server port.
        device_id: Id this device reports.
        stop: Event that ends the device when set.
        delay_ms: Artificial delay before each reply.

    Raises:
        OSError: If the server cannot be reached.
    """
    started = time.monotonic()
    state: dict = {}
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(0.2)
        buffer = b""
        while not stop.is_set():
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if delay_ms:
                    time.sleep(delay_ms / 1000)
                reply = respond(line.decode(errors="replace"), device_id, started, state)
                sock.sendall(reply.encode() + b"\n")


def run_udp_device(host: str, port: int, device_id: str, stop: threading.Event) -> None:
    """Run one simulated UDP device until stopped.

    The device announces itself with ``HELLO <id>`` and then answers every
    datagram line from the server.

    Args:
        host: Workbench server host.
        port: Workbench server port.
        device_id: Id this device reports.
        stop: Event that ends the device when set.

    Raises:
        OSError: If the socket cannot be used.
    """
    started = time.monotonic()
    state: dict = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(0.2)
        sock.sendto(f"HELLO {device_id}\n".encode(), (host, port))
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(65535)
            except TimeoutError:
                continue
            for line in data.split(b"\n"):
                if line.strip():
                    reply = respond(line.decode(errors="replace"), device_id, started, state)
                    sock.sendto(reply.encode() + b"\n", addr)


def main() -> None:
    """Parse the command line and run the simulated devices until Ctrl+C."""
    parser = argparse.ArgumentParser(description="Demo devices for the Embedded Workbench session workflow")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--count", type=int, default=1, help="number of simulated devices")
    parser.add_argument("--prefix", default="SIM", help="device id prefix")
    parser.add_argument("--udp", action="store_true", help="talk to a UDP server instead of TCP")
    parser.add_argument("--delay-ms", type=int, default=0, help="artificial response delay (TCP)")
    args = parser.parse_args()

    stop = threading.Event()
    threads = []
    for i in range(1, args.count + 1):
        device_id = f"{args.prefix}-{i:04d}"
        if args.udp:
            target, extra = run_udp_device, ()
        else:
            target, extra = run_tcp_device, (args.delay_ms,)
        def run_device_thread(target=target, device_id=device_id, extra=extra) -> None:
            """Run one device and print how it ended.

            Args:
                target: Device function, TCP or UDP.
                device_id: Id this device reports.
                extra: Extra positional arguments for ``target``.
            """
            try:
                target(args.host, args.port, device_id, stop, *extra)
            except OSError as exc:
                print(f"{device_id}: cannot reach {args.host}:{args.port} ({exc}). Is the server started?")
            else:
                print(f"{device_id} disconnected")

        t = threading.Thread(target=run_device_thread, daemon=True)
        t.start()
        threads.append(t)
        print(f"{device_id} started")
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop.set()
    print("simulator stopped")


if __name__ == "__main__":
    main()
