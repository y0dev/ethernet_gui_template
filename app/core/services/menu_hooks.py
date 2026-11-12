"""Menu hooks: Python functions offered as options on the Operations Menu page.

A hook module (listed in ``project.json`` -> ``menu_hooks``) marks functions
with :func:`menu_option`. Each function receives a :class:`Device` for one
connected device, plus one keyword argument per :class:`Param`::

    from app.core.services.menu_hooks import Param, menu_option

    @menu_option("Ping", category="Connectivity")
    def ping(dev):
        reply = yield dev.request("PING", expect="PONG")
        dev.log(f"reply: {reply.text}")

    @menu_option("Blink", category="LED", params=[Param("times", 3, minimum=1, maximum=20)])
    def blink(dev, times):
        for _ in range(times):
            yield dev.request("LED ON", expect="OK LED ON")
            yield dev.wait(200)
            yield dev.request("LED OFF", expect="OK LED OFF")
            yield dev.wait(200)

Writing ``yield`` in front of ``dev.request()``, ``dev.send()`` or
``dev.wait()`` pauses the hook until that step finishes, without blocking
the GUI, and gives back a :class:`Reply`. A hook without ``yield`` is a plain
function: its requests are queued in order and it finishes at once.

Every request goes through the operation runner, so it is sent to that device
only, judged by its ``expect`` rule, timed out, and stored in
``operation_results`` under the current session like any other operation.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import sys
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, NoReturn

from PySide6.QtCore import QObject, QTimer, Signal

from ..hexutil import format_hex, to_ascii
from ..models import DeviceOperation, MatchMode, PayloadFormat

log = logging.getLogger(__name__)

MATCH_MODES = {"exact": MatchMode.EXACT, "prefix": MatchMode.PREFIX, "contains": MatchMode.CONTAINS,
               "any": MatchMode.ANY}


class HookStop(Exception):
    """Raised by :meth:`Device.fail` to stop a hook and mark the run failed."""


@dataclass
class Param:
    """An input asked for before a hook runs, passed as a keyword argument.

    The input widget follows the type: ``int`` and ``float`` get a number box,
    ``bool`` a checkbox, ``str`` a text field, and ``choices`` a drop-down.
    """

    #: keyword argument name of the hook function
    name: str
    #: initial value; its type is the input type unless ``kind`` is given
    default: Any = None
    #: label shown in the dialog (default: the name)
    label: str = ""
    #: ``int``, ``float``, ``str`` or ``bool``
    kind: type | None = None
    minimum: float | None = None
    maximum: float | None = None
    #: allowed values, shown as a drop-down
    choices: list[Any] | None = None

    @property
    def value_type(self) -> type:
        """The input's type: ``kind``, else the default's type, else ``str``."""
        if self.kind is not None:
            return self.kind
        return type(self.default) if self.default is not None else str


@dataclass
class MenuOption:
    """One entry of the Operations Menu."""

    title: str
    func: Callable[..., Any]
    category: str = "General"
    params: list[Param] = field(default_factory=list)
    description: str = ""
    module: str = ""


#: Options registered by @menu_option, in definition order.
_OPTIONS: list[MenuOption] = []


def menu_option(title: str, category: str = "General", params: Iterable[Param] = (),
                description: str = "") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Offer the decorated function as an option on the Operations Menu.

    Args:
        title: Menu text.
        category: Menu group.
        params: Inputs asked for before the hook runs.
        description: Help text; defaults to the first paragraph of the docstring.

    Returns:
        A decorator that registers the function and returns it unchanged.
    """
    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        text = description or (inspect.getdoc(func) or "").split("\n\n")[0].replace("\n", " ").strip()
        _OPTIONS.append(MenuOption(title, func, category, list(params), text, func.__module__))
        return func
    return decorate


def load_hook_modules(modules: Iterable[str]) -> tuple[list[MenuOption], list[str]]:
    """Import, or re-import after an edit, the hook modules and collect their options.

    Args:
        modules: Importable module names, e.g. ``plugins.example_menu``.

    Returns:
        The options in module and definition order, and one problem
        description per module that failed or defines no options.
    """
    options: list[MenuOption] = []
    problems: list[str] = []
    for name in modules:
        _OPTIONS[:] = [o for o in _OPTIONS if o.module != name]
        try:
            if name in sys.modules:
                importlib.reload(sys.modules[name])
            else:
                importlib.import_module(name)
        except Exception as exc:
            problems.append(f"{name}: {type(exc).__name__}: {exc}")
            log.error("menu hooks failed to load", extra={"context": {"module": name, "error": str(exc)}})
            continue
        found = [o for o in _OPTIONS if o.module == name]
        if not found:
            problems.append(f"{name}: no @menu_option functions")
        options.extend(found)
    return options, problems


@dataclass
class Reply:
    """Outcome of one request, given back by ``yield dev.request(...)``."""

    #: ``passed``, ``failed``, ``timeout``, ``sent`` or ``error``
    status: str
    #: reply payload without framing, or None if nothing came back
    data: bytes | None
    #: detail, e.g. why it failed
    message: str = ""

    @property
    def passed(self) -> bool:
        """True if the reply matched the rule (or a send went out)."""
        return self.status in ("passed", "sent")

    @property
    def text(self) -> str:
        """The reply decoded as text, without trailing line endings."""
        return self.data.decode("utf-8", errors="replace").rstrip("\r\n") if self.data else ""

    def __bool__(self) -> bool:
        return self.passed


class Pending:
    """A request or wait in progress. ``yield`` it to wait for its result."""

    def __init__(self) -> None:
        """Create an unresolved step."""
        self.done = False
        self.result: Any = None
        self._waiter: Callable[[Any], None] | None = None

    def resolve(self, value: Any) -> None:
        """Store the result and wake the waiting hook.

        Args:
            value: The step's result.
        """
        self.done, self.result = True, value
        waiter, self._waiter = self._waiter, None
        if waiter is not None:
            waiter(value)

    def then(self, callback: Callable[[Any], None]) -> None:
        """Call ``callback`` with the result, now if it is already known.

        Args:
            callback: Receives the result.
        """
        if self.done:
            callback(self.result)
        else:
            self._waiter = callback


def _to_bytes(value: bytes | bytearray | str) -> bytes:
    """Turn a payload argument into bytes: ``str`` is sent as UTF-8 text.

    Args:
        value: Bytes, or text such as ``"PING"``. For hex, pass
            ``bytes.fromhex("01 12")``.

    Returns:
        The bytes to send or expect.
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError(f"payload must be bytes or str, not {type(value).__name__}")


class Device:
    """The connected device a hook runs on: send, wait for replies, log."""

    def __init__(self, run: HookRun, peer: str, name: str) -> None:
        """Bind the device to its run.

        Args:
            run: The hook run that owns this device view.
            peer: Device address, ``host:port``.
            name: Device name shown in the workbench.
        """
        self._run = run
        self.peer = peer
        self.name = name

    def request(self, payload: bytes | str, expect: bytes | str | None = None, match: str = "exact",
                timeout_ms: int = 2000, name: str = "") -> Pending:
        """Send a payload and wait for the device's reply.

        Args:
            payload: Bytes, or text such as ``"PING"``.
            expect: Reply to check for; None accepts any reply.
            match: How ``expect`` is compared: ``exact``, ``prefix`` or ``contains``.
            timeout_ms: How long to wait for the reply.
            name: Name stored with the result (default: the menu option's title).

        Returns:
            A step to ``yield``; it gives back a :class:`Reply`.
        """
        if expect is None:
            mode = MatchMode.ANY
        elif match in MATCH_MODES:
            mode = MATCH_MODES[match]
        else:
            raise ValueError(f"match must be one of {sorted(MATCH_MODES)}, not {match!r}")
        return self._run.queue(name, _to_bytes(payload), mode, _to_bytes(expect) if expect is not None else b"",
                               timeout_ms)

    def send(self, payload: bytes | str, name: str = "") -> Pending:
        """Send a payload without waiting for a reply.

        Args:
            payload: Bytes, or text.
            name: Name stored with the result (default: the menu option's title).

        Returns:
            A step to ``yield`` (optional); it gives back a :class:`Reply`.
        """
        return self._run.queue(name, _to_bytes(payload), MatchMode.NONE, b"", 1000)

    def wait(self, ms: int) -> Pending:
        """Pause the hook without blocking the GUI.

        Args:
            ms: Milliseconds to wait.

        Returns:
            A step to ``yield``.
        """
        pending = Pending()
        QTimer.singleShot(max(0, int(ms)), lambda: pending.resolve(None))
        return pending

    def log(self, text: str) -> None:
        """Write a line to the Operations Menu log.

        Args:
            text: Message.
        """
        self._run.output.emit("info", f"{self.name}: {text}")

    def fail(self, reason: str) -> NoReturn:
        """Stop the hook and mark this run failed.

        Args:
            reason: Shown in the log.

        Raises:
            HookStop: Always.
        """
        raise HookStop(reason)


class HookRun(QObject):
    """Runs one menu option on one device, resuming it as replies arrive."""

    #: (level, text); level is ``info``, ``ok``, ``warn`` or ``error``
    output = Signal(str, str)
    #: ``done``, ``failed``, ``error`` or ``cancelled``
    finished = Signal(str)

    def __init__(self, workflow, option: MenuOption, peer: str, name: str, values: dict[str, Any],
                 parent: QObject | None = None) -> None:
        """Prepare the run; nothing is sent until :meth:`start`.

        Args:
            workflow: The ``WorkflowService`` whose server and session are used.
            option: Menu option to run.
            peer: Device address.
            name: Device name for the log and results.
            values: Input values, by parameter name.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self.workflow = workflow
        self.option = option
        self.values = values
        self.device = Device(self, peer, name)
        self.state = "ready"
        self._gen: Any = None

    def start(self) -> None:
        """Call the hook; a generator hook then advances as each yielded step finishes."""
        self.state = "running"
        try:
            result = self.option.func(self.device, **self.values)
        except Exception as exc:
            self._fail(exc)
            return
        if inspect.isgenerator(result):
            self._gen = result
            self._advance(None)
        else:
            self._finish("done")

    def cancel(self) -> None:
        """Stop the hook; requests already queued still run and are recorded."""
        if self.state == "running":
            if self._gen is not None:
                self._gen.close()
            self._finish("cancelled")

    def queue(self, name: str, payload: bytes, mode: MatchMode, expected: bytes, timeout_ms: int) -> Pending:
        """Queue one request on the device through the workflow; used by :class:`Device`.

        Args:
            name: Result name, or ``""`` for the option title.
            payload: Bytes to send.
            mode: Reply match rule.
            expected: Bytes for the rule.
            timeout_ms: Reply timeout.

        Returns:
            The pending step, resolved with a :class:`Reply`.
        """
        op = DeviceOperation(
            name=name or self.option.title, payload_text=format_hex(payload), payload_format=PayloadFormat.HEX,
            match_mode=mode, expected_text=format_hex(expected), timeout_ms=timeout_ms,
            category=self.option.category, description="menu hook",
        )
        pending = Pending()

        def on_result(row: dict[str, Any]) -> None:
            """Turn the stored result row into a Reply and resume the hook."""
            frame = row.get("response")
            data = None
            if frame is not None and self.workflow.server_id is not None:
                data = self.workflow.runner.unwrap(self.workflow.server_id, bytes(frame))
            reply = Reply(row["status"], data, row.get("message") or "")
            level = "ok" if reply.passed else "warn"
            shown = f"  ← {to_ascii(data)}  [{format_hex(data)}]" if data else ""
            self.output.emit(level, f"{self.device.name}: {op.name}: {reply.status.upper()}{shown}"
                                    + ("" if reply.passed else f"  ({reply.message})"))
            pending.resolve(reply)

        self.workflow.run_operations([self.device.peer], [op], on_result=on_result)
        return pending

    def _advance(self, value: Any) -> None:
        """Send the last step's result into the hook and wait on the next step it yields.

        Args:
            value: Result of the step the hook was waiting on.
        """
        if self.state != "running":
            return
        try:
            step = self._gen.send(value)
        except StopIteration:
            self._finish("done")
            return
        except Exception as exc:
            self._fail(exc)
            return
        if not isinstance(step, Pending):
            self._gen.close()
            self._fail(TypeError(f"yield dev.request(...), dev.send(...) or dev.wait(...), not {step!r}"))
            return
        # Resume from the event loop, not from inside the runner's callback.
        step.then(lambda result: QTimer.singleShot(0, lambda: self._advance(result)))

    def _fail(self, exc: Exception) -> None:
        """Report a hook's exception and finish the run.

        Args:
            exc: ``HookStop`` from ``dev.fail()``, or any error in the hook.
        """
        if isinstance(exc, HookStop):
            self.output.emit("error", f"{self.device.name}: {self.option.title}: failed: {exc}")
            self._finish("failed")
            return
        frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == inspect.getfile(self.option.func)]
        where = f" (line {frames[-1].lineno})" if frames else ""
        self.output.emit("error", f"{self.device.name}: {self.option.title}: {type(exc).__name__}: {exc}{where}")
        log.error("menu hook raised", extra={"context": {"option": self.option.title, "error": str(exc)}})
        self._finish("error")

    def _finish(self, state: str) -> None:
        """Mark the run finished and announce it.

        Args:
            state: Final state.
        """
        self.state = state
        if state == "done":
            self.output.emit("ok", f"{self.device.name}: {self.option.title}: done")
        self.finished.emit(state)
