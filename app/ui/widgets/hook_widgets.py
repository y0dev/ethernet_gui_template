"""Building blocks for pages that run menu hooks (see app/core/services/menu_hooks.py).

The Operations Menu page and the ``menu_*`` page templates are assembled from
these, so a new layout only arranges them:

* :class:`DeviceTargets`: tickable list of the workflow's connected devices.
* :class:`WorkflowStatus`: one line saying whether hooks can run (server, session).
* :class:`ParamForm` / :class:`ParamDialog`: inputs for an option's ``Param`` list.
* :class:`HookLog`: timestamped, coloured run log.
* :class:`HookLauncher`: starts an option on devices, tracks the runs, stops them.
* :func:`load_options`: the hook options in menu order.
"""

from __future__ import annotations

import html
import time
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.services.menu_hooks import HookRun, MenuOption, Param, load_hook_modules
from ..styles import theme

LEVEL_COLORS = {"ok": lambda: theme.OK, "warn": lambda: theme.WARN, "error": lambda: theme.ERROR,
                "info": lambda: theme.TEXT}


def load_options(modules: list[str]) -> tuple[list[MenuOption], list[str]]:
    """Import the hook modules and return their options grouped by category.

    Categories keep the order in which they first appear, so numbering the
    result runs straight down a grouped menu.

    Args:
        modules: Hook module names (``ctx.menu_hooks``).

    Returns:
        The options and one problem description per module that failed.
    """
    options, problems = load_hook_modules(modules)
    categories = list(dict.fromkeys(o.category for o in options))
    return sorted(options, key=lambda o: categories.index(o.category)), problems


class DeviceTargets(QGroupBox):
    """Tickable list of the connected workflow devices; new devices start ticked."""

    def __init__(self, workflow, title: str = "Devices", parent: QWidget | None = None) -> None:
        """Build the list with All / None buttons and follow the workflow's devices.

        Args:
            workflow: The ``WorkflowService``.
            title: Group box title.
            parent: Optional parent widget.
        """
        super().__init__(title, parent)
        self.wf = workflow
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        for text, state in (("All", Qt.CheckState.Checked), ("None", Qt.CheckState.Unchecked)):
            button = QPushButton(text)
            button.clicked.connect(lambda _c=False, s=state: self.tick_all(s))
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        self._known: set[str] = set()
        workflow.devices_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        """Refill the list from the connected devices, keeping the ticks."""
        ticked = set(self.ticked_peers())
        self.list.clear()
        for device in self.wf.connected_devices():
            if not device.connected:
                continue
            item = QListWidgetItem(f"{device.label}    {device.peer}" if device.name else device.peer)
            item.setData(Qt.ItemDataRole.UserRole, (device.peer, device.label))
            new = device.peer not in self._known
            item.setCheckState(Qt.CheckState.Checked if new or device.peer in ticked else Qt.CheckState.Unchecked)
            self._known.add(device.peer)
            self.list.addItem(item)

    def ticked(self) -> list[tuple[str, str]]:
        """Return (address, name) of every ticked device."""
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def ticked_peers(self) -> list[str]:
        """Return the addresses of the ticked devices."""
        return [peer for peer, _label in self.ticked()]

    def set_targets(self, peers: list[str]) -> None:
        """Tick exactly these devices.

        Args:
            peers: Device addresses to tick.
        """
        self.refresh()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole)[0] in peers
                               else Qt.CheckState.Unchecked)

    def tick_all(self, state: Qt.CheckState) -> None:
        """Tick or untick every device.

        Args:
            state: New check state.
        """
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)


class WorkflowStatus(QLabel):
    """Says whether hooks can run: the workflow server and a session are needed."""

    def __init__(self, workflow, parent: QWidget | None = None) -> None:
        """Create the label and follow the workflow state.

        Args:
            workflow: The ``WorkflowService``.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.wf = workflow
        self.setWordWrap(True)
        for signal in (workflow.devices_changed, workflow.server_state_changed, workflow.session_changed):
            signal.connect(lambda *_: self.refresh())
        self.refresh()

    @property
    def ready(self) -> bool:
        """True when the server runs and a session records results."""
        return self.wf.server_running and self.wf.session_id is not None

    def refresh(self) -> None:
        """Show the current state."""
        if not self.wf.server_running:
            text, color = "Start the server in the Session Workflow (step 1) so devices can connect.", theme.WARN
        elif self.wf.session_id is None:
            text, color = "Start a session in the Session Workflow (step 3) so results are recorded.", theme.WARN
        else:
            count = sum(d.connected for d in self.wf.connected_devices())
            text, color = f"Session {self.wf.session_id} recording · {count} device(s) connected", theme.OK
        self.setText(text)
        self.setStyleSheet(f"color: {color};")


class ParamForm(QWidget):
    """One input per ``Param`` of an option, laid out as a form."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create an empty form; call :meth:`set_option` to fill it.

        Args:
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.form = QFormLayout(self)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.option: MenuOption | None = None
        self.inputs: dict[str, QWidget] = {}

    def set_option(self, option: MenuOption | None, values: dict[str, Any] | None = None) -> None:
        """Show the inputs of another option.

        Args:
            option: The option, or None to clear the form.
            values: Initial values by name; missing ones use the defaults.
        """
        while self.form.rowCount():
            self.form.removeRow(0)
        self.option, self.inputs = option, {}
        for param in option.params if option else []:
            widget = input_for(param, (values or {}).get(param.name, param.default))
            self.form.addRow(param.label or param.name, widget)
            self.inputs[param.name] = widget

    def values(self) -> dict[str, Any]:
        """Read the entered values.

        Returns:
            Values by parameter name, converted to each parameter's type.
        """
        result: dict[str, Any] = {}
        for name, widget in self.inputs.items():
            if isinstance(widget, QComboBox):
                result[name] = widget.currentData()
            elif isinstance(widget, QCheckBox):
                result[name] = widget.isChecked()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                result[name] = widget.value()
            else:
                result[name] = widget.text()
        return result


def input_for(param: Param, value: Any) -> QWidget:
    """Create the input widget that suits a parameter's type.

    Args:
        param: Parameter description.
        value: Initial value, or None.

    Returns:
        A combo box, checkbox, number box or text field.
    """
    kind = param.value_type
    if param.choices:
        box = QComboBox()
        for choice in param.choices:
            box.addItem(str(choice), choice)
        box.setCurrentIndex(max(0, box.findData(value)))
        return box
    if kind is bool:
        check = QCheckBox()
        check.setChecked(bool(value))
        return check
    if kind in (int, float):
        spin = QSpinBox() if kind is int else QDoubleSpinBox()
        low = param.minimum if param.minimum is not None else -1_000_000
        high = param.maximum if param.maximum is not None else 1_000_000
        spin.setRange(kind(low), kind(high))
        if value is not None:
            spin.setValue(kind(value))
        return spin
    return QLineEdit("" if value is None else str(value))


class ParamDialog(QDialog):
    """Asks for an option's inputs before it runs."""

    def __init__(self, option: MenuOption, values: dict[str, Any], parent: QWidget | None = None) -> None:
        """Build the dialog.

        Args:
            option: Option whose parameters are asked for.
            values: Initial values by name (the last ones used), else the defaults.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.setWindowTitle(option.title)
        layout = QVBoxLayout(self)
        if option.description:
            text = QLabel(option.description)
            text.setWordWrap(True)
            text.setObjectName("Dim")
            layout.addWidget(text)
        self.form = ParamForm()
        self.form.set_option(option, values)
        layout.addWidget(self.form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict[str, Any]:
        """Return the entered values by parameter name."""
        return self.form.values()


class HookLog(QPlainTextEdit):
    """Read-only, timestamped, coloured log of hook runs."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create the log.

        Args:
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(theme.monospace_font(9))

    def append(self, level: str, text: str) -> None:
        """Add a line.

        Args:
            level: ``info``, ``ok``, ``warn`` or ``error``.
            text: Message.
        """
        color = LEVEL_COLORS.get(level, LEVEL_COLORS["info"])()
        self.appendHtml(f'<span style="color:{theme.TEXT_DIM}">{time.strftime("%H:%M:%S")}</span> '
                        f'<span style="color:{color}; white-space:pre">{html.escape(text)}</span>')


class HookLauncher(QObject):
    """Starts options on devices and keeps track of the runs."""

    #: a run started (HookRun)
    run_started = Signal(object)
    #: a run finished (HookRun); its ``state`` is done, failed, error or cancelled
    run_finished = Signal(object)

    def __init__(self, workflow, log: HookLog | None = None, parent: QObject | None = None) -> None:
        """Create the launcher.

        Args:
            workflow: The ``WorkflowService`` whose server and session are used.
            log: Log that receives every run's output, or None.
            parent: Optional Qt parent.
        """
        super().__init__(parent)
        self.wf = workflow
        self.log = log
        self.runs: list[HookRun] = []
        #: last input values per option, offered again next time
        self.last_values: dict[str, dict[str, Any]] = {}

    def problem(self, targets: list[tuple[str, str]]) -> str:
        """Explain why nothing can run now.

        Args:
            targets: The (address, name) pairs that would run.

        Returns:
            The reason, or ``""`` when the run can start.
        """
        if not self.wf.server_running:
            return "Start the server in the Session Workflow (step 1) first."
        if self.wf.session_id is None:
            return "Start a session in the Session Workflow (step 3) first."
        if not targets:
            return "Tick at least one connected device."
        return ""

    def ask_values(self, option: MenuOption, parent: QWidget) -> dict[str, Any] | None:
        """Ask for an option's inputs with a dialog, offering the last values.

        Args:
            option: Option to run.
            parent: Dialog parent.

        Returns:
            The values, ``{}`` for an option without inputs, or None if cancelled.
        """
        if not option.params:
            return {}
        key = f"{option.module}:{option.title}"
        dialog = ParamDialog(option, self.last_values.get(key, {}), parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        self.last_values[key] = dialog.values()
        return self.last_values[key]

    def start(self, option: MenuOption, targets: list[tuple[str, str]], values: dict[str, Any]) -> list[HookRun]:
        """Start an option on each device.

        Args:
            option: Option to run.
            targets: (address, name) of each device.
            values: Input values by parameter name.

        Returns:
            The started runs.
        """
        self.last_values[f"{option.module}:{option.title}"] = dict(values)
        shown = ", ".join(f"{k}={v!r}" for k, v in values.items())
        started = []
        for peer, label in targets:
            # No Qt parent: a stopped run may still get replies to requests it queued, so it is
            # freed by Python once nothing refers to it any more.
            run = HookRun(self.wf, option, peer, label, dict(values))
            if self.log is not None:
                run.output.connect(self.log.append)
                self.log.append("info", f"{label}: ▶ {option.title}" + (f" ({shown})" if shown else ""))
            run.finished.connect(lambda _state, r=run: self._finished(r))
            self.runs.append(run)
            self.run_started.emit(run)
            run.start()
            started.append(run)
        return started

    def _finished(self, run: HookRun) -> None:
        """Forget a finished run and announce it.

        Args:
            run: The run that finished.
        """
        if run in self.runs:
            self.runs.remove(run)
        self.run_finished.emit(run)

    def stop_all(self) -> None:
        """Stop every running hook; requests already queued still complete."""
        for run in list(self.runs):
            run.cancel()
        if self.log is not None:
            self.log.append("warn", "stopped all running hooks")
