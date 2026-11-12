"""The C console menu of console_menu.c as Operations Menu hooks.

DUMMY EXAMPLE, like the rest of examples/led_device. Each ``case`` of the C
``switch`` becomes one ``@menu_option`` function, and the encoding that
``send_cmd()`` does in C is ``request()`` below. Unlike the fixed operations
in ``led_device.json``, hooks can take inputs (blink count, period) and
decode replies (temperature, serial number) in Python.

Use it: add ``"examples.led_device.menu_hooks"`` to ``project.json`` ->
``menu_hooks``, start the Session Workflow server with the framing from
``make_command_set.py`` (``FRAMING``) and a session, start
``led_device_sim.py``, then open the Operations Menu page.
"""

from __future__ import annotations

import struct

from app.core.compatibility.crc import CRC8, Crc
from app.core.services.menu_hooks import Param, menu_option

CMD_PING, CMD_GET_INFO, CMD_LED_SET, CMD_LED_BLINK, CMD_GET_TEMP, CMD_RESET = 0x01, 0x02, 0x10, 0x11, 0x20, 0x30
REPLY = 0x80          # reply command = request command | 0x80

_crc = Crc(CRC8)


def request(dev, cmd: int, data: bytes = b"", timeout_ms: int = 1000, name: str = ""):
    """Send ``CMD | DATA | CRC8`` (the server framing adds LEN) and expect ``CMD|0x80`` back.

    Args:
        dev: Device the hook runs on.
        cmd: Command byte.
        data: Command data.
        timeout_ms: Reply timeout.
        name: Result name (default: the menu option's title).

    Returns:
        The step to ``yield``; replies are compared without LEN and CRC.
    """
    body = bytes([cmd]) + data
    payload = body + bytes([_crc.compute(bytes([len(body)]) + body)])
    return dev.request(payload, expect=bytes([cmd | REPLY]), match="prefix", timeout_ms=timeout_ms, name=name)


@menu_option("Ping", category="Connectivity")
def ping(dev):
    """Menu 1: liveness check."""
    reply = yield request(dev, CMD_PING)
    if not reply:
        dev.fail(reply.message)


@menu_option("Send device info", category="Identification")
def device_info(dev):
    """Menu 2: read the serial number and firmware version."""
    reply = yield request(dev, CMD_GET_INFO)
    if not reply:
        dev.fail(reply.message)
    fields = dict(part.split("=", 1) for part in reply.data[1:].decode(errors="replace").split(";") if "=" in part)
    dev.log(f"serial {fields.get('SN', '?')}, firmware {fields.get('FW', '?')}")


@menu_option("Set LED", category="LED", params=[Param("on", True, label="LED on")])
def set_led(dev, on):
    """Menus 3 and 4: switch the LED and check the device reports the new state."""
    reply = yield request(dev, CMD_LED_SET, bytes([int(on)]))
    if not reply or reply.data[1:2] != bytes([int(on)]):
        dev.fail(f"LED state not confirmed ({reply.message})")


@menu_option("Blink LED", category="LED", params=[
    Param("times", 3, label="Blinks", minimum=1, maximum=255),
    Param("period_ms", 250, label="Period (ms)", minimum=10, maximum=2550),
])
def blink(dev, times, period_ms):
    """Menu 5, with the count and period as inputs instead of fixed values."""
    reply = yield request(dev, CMD_LED_BLINK, bytes([times, period_ms // 10]), timeout_ms=2000)
    if not reply or reply.data[1:2] != b"\x00":
        dev.fail(f"blink refused ({reply.message})")


@menu_option("Read temperature", category="Diagnostics",
             params=[Param("max_temp", 40.0, label="Max temperature (°C)", minimum=-40, maximum=125)])
def read_temperature(dev, max_temp):
    """Menu 6: decode the int16 big-endian reading (0.1 °C) and check it against a limit."""
    reply = yield request(dev, CMD_GET_TEMP)
    if not reply or len(reply.data) < 3:
        dev.fail(f"no temperature ({reply.message})")
    temp = struct.unpack(">h", reply.data[1:3])[0] / 10
    dev.log(f"temperature {temp:.1f} °C")
    if temp > max_temp:
        dev.fail(f"{temp:.1f} °C is above {max_temp:.1f} °C")


@menu_option("Reset", category="Maintenance")
def reset(dev):
    """Menu 7: the device resets without answering; check it answers a ping afterwards."""
    body = bytes([CMD_RESET])
    yield dev.send(body + bytes([_crc.compute(bytes([len(body)]) + body)]))
    yield dev.wait(300)
    reply = yield request(dev, CMD_PING, name="Ping after reset")
    if not reply:
        dev.fail("no answer after reset")
