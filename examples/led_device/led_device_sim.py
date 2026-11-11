"""DUMMY EXAMPLE: simulated LED devices for trying the Session Workflow.

    python examples/led_device/led_device_sim.py --port 5000 --count 3

Each simulated device connects to the workflow server (the role the C
console program had) and answers the frames described in console_menu.c:

    LEN | CMD | DATA... | CRC8        CRC-8/SMBUS over LEN, CMD and DATA

    0x01 PING        -> 0x81
    0x02 GET_INFO    -> 0x82 "SN=<serial>;FW=1.2.0"
    0x10 LED_SET n   -> 0x90 n
    0x11 LED_BLINK   -> 0x91 0x00
    0x20 GET_TEMP    -> 0xA0 int16 big-endian (0.1 degC)
    0x30 RESET       -> (no reply)
    other            -> 0xFF <cmd>;  bad CRC -> 0xFF 0xEE

The protocol is invented for this example.  Only the standard library is
used, like small firmware would, so the CRC here is an independent check of
the CRC the command set generator uses.
"""

from __future__ import annotations

import argparse
import random
import socket
import struct
import threading
import time


def crc8(data: bytes) -> int:
    """Compute CRC-8/SMBUS (poly 0x07, init 0x00), as ``crc8()`` in console_menu.c.

    Args:
        data: Bytes to checksum.

    Returns:
        The 8-bit CRC.
    """
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def encode_frame(cmd: int, data: bytes = b"") -> bytes:
    """Build a reply frame.

    Args:
        cmd: Reply command byte.
        data: Reply data.

    Returns:
        ``LEN | CMD | DATA | CRC8``.
    """
    head = bytes([len(data) + 1, cmd]) + data
    return head + bytes([crc8(head)])


class LedDevice:
    """State and command handling of one simulated device."""

    def __init__(self, serial: str) -> None:
        """Create a device with its LED off.

        Args:
            serial: Serial number reported by GET_INFO.
        """
        self.serial = serial
        self.led_on = False
        self.blinks = 0

    def handle(self, frame: bytes) -> bytes | None:
        """Answer one complete request frame.

        Args:
            frame: ``LEN | CMD | DATA | CRC8`` as received.

        Returns:
            The reply frame, or None for commands that are not answered.
        """
        if crc8(frame[:-1]) != frame[-1]:
            return encode_frame(0xFF, b"\xEE")
        cmd, data = frame[1], frame[2:-1]
        if cmd == 0x01:
            return encode_frame(0x81)
        if cmd == 0x02:
            return encode_frame(0x82, f"SN={self.serial};FW=1.2.0".encode())
        if cmd == 0x10 and len(data) == 1:
            self.led_on = bool(data[0])
            return encode_frame(0x90, bytes([int(self.led_on)]))
        if cmd == 0x11 and len(data) == 2:
            self.blinks += data[0]
            return encode_frame(0x91, b"\x00")
        if cmd == 0x20:
            return encode_frame(0xA0, struct.pack(">h", int((22.0 + random.random() * 4) * 10)))
        if cmd == 0x30:
            self.led_on = False
            return None
        return encode_frame(0xFF, bytes([cmd]))


def split_frames(buffer: bytearray) -> list[bytes]:
    """Remove and return every complete frame at the start of a receive buffer.

    Args:
        buffer: Bytes received so far; complete frames are deleted from it.

    Returns:
        The complete frames, oldest first.
    """
    frames = []
    while buffer and len(buffer) >= buffer[0] + 2:
        size = buffer[0] + 2          # LEN byte + LEN bytes + CRC byte
        frames.append(bytes(buffer[:size]))
        del buffer[:size]
    return frames


def run_device(host: str, port: int, serial: str, stop: threading.Event) -> None:
    """Connect one simulated device and answer requests until stopped or disconnected.

    Args:
        host: Workflow server host.
        port: Workflow server port.
        serial: Serial number of this device.
        stop: Event that ends the device when set.

    Raises:
        OSError: If the server cannot be reached.
    """
    device = LedDevice(serial)
    buffer = bytearray()
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(0.2)
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
            for frame in split_frames(buffer):
                reply = device.handle(frame)
                if reply is not None:
                    sock.sendall(reply)


def main() -> None:
    """Start the requested number of simulated devices and run until Ctrl+C."""
    parser = argparse.ArgumentParser(description="Dummy LED devices for the Session Workflow example")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--count", type=int, default=1, help="number of simulated devices")
    args = parser.parse_args()
    stop = threading.Event()
    threads = []
    for i in range(1, args.count + 1):
        serial = f"LED-{i:04d}"

        def run_one(serial: str = serial) -> None:
            """Run one device and print how it ended.

            Args:
                serial: Serial number of the device.
            """
            try:
                run_device(args.host, args.port, serial, stop)
            except OSError as exc:
                print(f"{serial}: cannot reach {args.host}:{args.port} ({exc}). Is the server started?")
            else:
                print(f"{serial} disconnected")

        thread = threading.Thread(target=run_one, daemon=True)
        thread.start()
        threads.append(thread)
        print(f"{serial} started")
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop.set()
    print("simulator stopped")


if __name__ == "__main__":
    main()
