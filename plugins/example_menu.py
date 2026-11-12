"""Example menu hooks: the options on the Operations Menu page.

DEMO ONLY: these talk to ``tools/device_simulator.py`` (start the Session
Workflow server with framing "Delimiter 0a"). Replace them with your device's
commands; ``examples/led_device/menu_hooks.py`` does the same for a binary
protocol with a CRC.

Each ``@menu_option`` function is one menu entry. It receives ``dev``, the
device it runs on, plus one keyword argument per ``Param``:

* ``reply = yield dev.request(payload, expect=..., match="exact"|"prefix"|"contains")``
  sends and waits for the reply (a ``Reply``: ``.passed``, ``.status``,
  ``.data`` bytes, ``.text``).
* ``yield dev.send(payload)`` sends without waiting for a reply.
* ``yield dev.wait(ms)`` pauses without freezing the window.
* ``dev.log(text)`` writes to the page's log; ``dev.fail(reason)`` stops
  the hook and marks it failed.

Payloads are bytes (``bytes.fromhex("01 12")``) or text (``"PING"``). Every
request is stored in ``operation_results`` under the current session, so
reports include them. Edit this file and press **Reload hooks**.
"""

from __future__ import annotations

from app.core.services.menu_hooks import Param, menu_option


@menu_option("Ping", category="Connectivity")
def ping(dev):
    """Check the device answers PING with PONG."""
    reply = yield dev.request("PING", expect="PONG")
    if not reply:
        dev.fail(f"no PONG ({reply.message})")


@menu_option("Identify", category="Connectivity")
def identify(dev):
    """Read the device id and firmware version and log them."""
    device_id = yield dev.request("ID?", expect="ID ", match="prefix")
    version = yield dev.request("VERSION?", expect="VERSION ", match="prefix")
    dev.log(f"{device_id.text}, {version.text}")


@menu_option("Set LED", category="LED", params=[Param("state", "ON", label="LED", choices=["ON", "OFF"])])
def set_led(dev, state):
    """Switch the LED on or off and check the device confirms it."""
    yield dev.request(f"LED {state}", expect=f"OK LED {state}")


@menu_option("Blink LED", category="LED", params=[
    Param("times", 3, label="Blinks", minimum=1, maximum=20),
    Param("period_ms", 400, label="Period (ms)", minimum=50, maximum=5000),
])
def blink(dev, times, period_ms):
    """Blink the LED by switching it on and off, pausing between steps.

    The pause uses ``yield dev.wait()``, so the window stays responsive.
    """
    for i in range(times):
        on = yield dev.request("LED ON", expect="OK LED ON")
        yield dev.wait(period_ms // 2)
        off = yield dev.request("LED OFF", expect="OK LED OFF")
        if not (on and off):
            dev.fail(f"blink {i + 1} was not confirmed")
        yield dev.wait(period_ms // 2)
    dev.log(f"blinked {times} time(s)")


@menu_option("Health check", category="Diagnostics",
             params=[Param("max_temp", 45.0, label="Max temperature (°C)", minimum=0, maximum=150)])
def health_check(dev, max_temp):
    """Read STATUS and fail if the temperature is above the limit.

    Shows how to look inside a reply and decide in Python.
    """
    reply = yield dev.request("STATUS?", expect="STATUS OK", match="prefix")
    if not reply:
        dev.fail(f"status not OK: {reply.text or reply.message}")
    fields = dict(part.split("=", 1) for part in reply.text.split() if "=" in part)
    temp = float(fields.get("TEMP", "nan"))
    dev.log(f"temperature {temp:.1f} °C, uptime {fields.get('UPTIME', '?')} s")
    if not temp <= max_temp:
        dev.fail(f"temperature {temp:.1f} °C is above {max_temp:.1f} °C")


@menu_option("Reset and check", category="Maintenance")
def reset_and_check(dev):
    """Reset the device, wait, then check it answers again."""
    yield dev.request("RESET", expect="OK RESET")
    yield dev.wait(500)
    reply = yield dev.request("PING", expect="PONG", name="Ping after reset")
    if not reply:
        dev.fail("no answer after reset")


@menu_option("Send text", category="Tools", params=[Param("text", "PING", label="Text to send")])
def send_text(dev, text):
    """Send any text and log whatever comes back."""
    reply = yield dev.request(text, name=f"Send {text!r}")
    dev.log(f"reply: {reply.text!r}" if reply.data else f"no reply ({reply.message})")


@menu_option("LED on then off (no waiting)", category="LED")
def led_on_off(dev):
    """A plain function (no ``yield``): both requests are queued in order and run on their own.

    Use this form when the hook never needs to look at a reply.
    """
    dev.request("LED ON", expect="OK LED ON")
    dev.request("LED OFF", expect="OK LED OFF")
