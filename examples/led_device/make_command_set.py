"""Build the workflow command set for the dummy LED device.

    python examples/led_device/make_command_set.py

Each entry of the C console menu (console_menu.c) becomes one operation:
the bytes ``send_cmd()`` would put on the wire, minus the LEN byte that the
workflow server's framing adds, plus the reply rule a person used to check
by eye.  The CRC is computed here once, with the same CRC-8/SMBUS the C code
uses, so the operations are plain hex that any workflow can send.

This is the pattern for your own device: describe the commands in Python,
let the compatibility layer reproduce the C encoding, and write the JSON
that step 4 of the Session Workflow imports.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.compatibility.crc import CRC8, Crc  # noqa: E402
from app.core.hexutil import format_hex  # noqa: E402

OUTPUT = Path(__file__).with_name("led_device.json")

#: Server framing for step 1: LEN (1 byte) first, counting CMD + DATA; the
#: CRC byte after them is not counted, hence length_adjust = 1.
FRAMING = {"type": "length_prefixed", "length_size": 1, "byte_order": "big",
           "length_offset": 0, "length_adjust": 1}

CMD_PING, CMD_GET_INFO, CMD_LED_SET, CMD_LED_BLINK, CMD_GET_TEMP, CMD_RESET = 0x01, 0x02, 0x10, 0x11, 0x20, 0x30
REPLY = 0x80          # reply command = request command | 0x80
ERROR = 0xFF          # reply for an unknown command

_crc = Crc(CRC8)


def request_payload(cmd: int, data: bytes = b"") -> bytes:
    """Encode a request the way ``send_cmd()`` in console_menu.c does, without LEN.

    The CRC covers LEN as well, so LEN is computed here even though the
    server's framing adds it when the operation is sent.

    Args:
        cmd: Command byte.
        data: Command data.

    Returns:
        ``CMD | DATA | CRC8``, the operation payload.
    """
    body = bytes([cmd]) + data
    return body + bytes([_crc.compute(bytes([len(body)]) + body)])


def operation(name: str, category: str, description: str, cmd: int, data: bytes = b"",
              match: str = "exact", expected: bytes = b"", timeout_ms: int = 1000) -> dict:
    """Build one command set entry.

    Replies are judged after the framing is removed, so ``expected`` is
    ``CMD | DATA`` without LEN and CRC.

    Args:
        name: Operation name shown in the menu.
        category: Menu group.
        description: Tooltip text.
        cmd: Request command byte.
        data: Request data.
        match: ``exact``, ``prefix``, ``contains``, ``any`` or ``none``.
        expected: Expected reply bytes for exact/prefix/contains.
        timeout_ms: How long to wait for the reply.

    Returns:
        The operation in the import format.
    """
    return {
        "name": name, "category": category, "description": description, "payload_format": "hex",
        "payload": format_hex(request_payload(cmd, data)), "match": match,
        "expected": format_hex(expected), "timeout_ms": timeout_ms,
    }


def build_command_set() -> dict:
    """Describe every menu entry of the C console program as an operation.

    Returns:
        The command set, ready to be written as JSON.
    """
    ops = [
        operation("Ping", "Connectivity", "Menu 1: liveness check", CMD_PING,
                  expected=bytes([CMD_PING | REPLY])),
        operation("Send device info", "Identification", "Menu 2: serial number and firmware", CMD_GET_INFO,
                  match="prefix", expected=bytes([CMD_GET_INFO | REPLY]) + b"SN="),
        operation("LED on", "LED", "Menu 3", CMD_LED_SET, b"\x01", expected=bytes([CMD_LED_SET | REPLY, 1])),
        operation("LED off", "LED", "Menu 4", CMD_LED_SET, b"\x00", expected=bytes([CMD_LED_SET | REPLY, 0])),
        operation("Blink LED x3", "LED", "Menu 5: 3 blinks, 250 ms period", CMD_LED_BLINK, bytes([3, 25]),
                  expected=bytes([CMD_LED_BLINK | REPLY, 0]), timeout_ms=2000),
        operation("Blink LED x10", "LED", "Not in the C menu: same command, other values", CMD_LED_BLINK,
                  bytes([10, 10]), expected=bytes([CMD_LED_BLINK | REPLY, 0]), timeout_ms=2000),
        operation("Read temperature", "Diagnostics", "Menu 6: int16 big-endian, 0.1 degC", CMD_GET_TEMP,
                  match="prefix", expected=bytes([CMD_GET_TEMP | REPLY])),
        operation("Reset", "Maintenance", "Menu 7: the device does not answer", CMD_RESET, match="none"),
        operation("Unknown command (expect error)", "Diagnostics", "Checks the device rejects bad commands",
                  0x7F, match="exact", expected=bytes([ERROR, 0x7F])),
    ]
    return {
        "description": "DUMMY EXAMPLE for examples/led_device (console_menu.c, led_device_sim.py). "
                       f"Start the workflow server with framing {json.dumps(FRAMING)}.",
        "framing": FRAMING,
        "operations": ops,
    }


def main() -> int:
    """Write ``led_device.json`` next to this script.

    Returns:
        The process exit code, 0 on success.
    """
    OUTPUT.write_text(json.dumps(build_command_set(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
