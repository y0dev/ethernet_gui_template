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
    """Connect, answer commands until ``stop`` is set or the server closes."""
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
    """Announce with ``HELLO <id>`` then answer datagrams from the server."""
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
        def run(target=target, device_id=device_id, extra=extra) -> None:
            try:
                target(args.host, args.port, device_id, stop, *extra)
            except OSError as exc:
                print(f"{device_id}: cannot reach {args.host}:{args.port} ({exc}). Is the server started?")
            else:
                print(f"{device_id} disconnected")

        t = threading.Thread(target=run, daemon=True)
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
