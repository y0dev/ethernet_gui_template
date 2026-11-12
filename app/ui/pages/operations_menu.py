"""Operations Menu: a numbered menu of Python hook functions to run on devices.

The options come from the modules in ``project.json`` -> ``menu_hooks``
(see ``plugins/example_menu.py``). Each option is a function marked with
``@menu_option``; it can send requests, wait for replies, decide what to
send next and log, see :mod:`app.core.services.menu_hooks`.

The page uses the Session Workflow's server and session: start both there,
tick devices here, then pick an option (double-click, Run, or type its number
and press Enter). Edit a hook file and press Reload hooks to pick up the change
without restarting.

Other layouts of the same menu are page templates (``menu_console``,
``menu_buttons``, ``menu_form``, ``menu_sequence``), built from the widgets in
:mod:`app.ui.widgets.hook_widgets`.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...context import AppContext
from ...core.services.menu_hooks import HookRun, MenuOption
from ..widgets.common import PageHeader, show_error
from ..widgets.hook_widgets import DeviceTargets, HookLauncher, HookLog, WorkflowStatus, load_options


class OperationsMenuPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None, embedded: bool = False) -> None:
        """Build the device list, the hook menu and the run log, then load the hooks.

        Args:
            ctx: Application context.
            parent: Optional parent widget.
            embedded: Shown as a step of the Session Workflow: no page title or margins.
        """
        super().__init__(parent)
        self.ctx = ctx
        self.wf = ctx.workflow
        self.options: list[MenuOption] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*((0, 0, 0, 0) if embedded else (18, 14, 18, 14)))
        header = PageHeader("Operations Menu", "Pick an option to run its Python hook on the ticked devices.")
        if embedded:   # the workflow's step bar already names the step
            header.findChild(QLabel, "PageTitle").hide()
        reload_button = QPushButton("Reload hooks")
        reload_button.setToolTip("Re-import the hook modules after editing them")
        reload_button.clicked.connect(self.reload_hooks)
        stop = QPushButton("Stop all")
        stop.setObjectName("Danger")
        stop.clicked.connect(self.stop_all)
        header.actions.addWidget(reload_button)
        header.actions.addWidget(stop)
        layout.addWidget(header)
        self.status = WorkflowStatus(self.wf)
        layout.addWidget(self.status)

        # Top: devices on the left, the menu on the right. Bottom: the log.
        top = QSplitter(Qt.Orientation.Horizontal)
        self.targets = DeviceTargets(self.wf)
        self.devices = self.targets.list
        top.addWidget(self.targets)

        menu_box = QGroupBox("Menu")
        mlayout = QVBoxLayout(menu_box)
        self.menu = QTreeWidget()
        self.menu.setHeaderLabels(["#", "Option", "Inputs"])
        self.menu.itemDoubleClicked.connect(lambda item, _c: self.run_item(item))
        self.menu.currentItemChanged.connect(lambda item, _p: self._show_description(item))
        mlayout.addWidget(self.menu, 1)
        self.description = QLabel("")
        self.description.setObjectName("Dim")
        self.description.setWordWrap(True)
        mlayout.addWidget(self.description)
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("Choice:"))
        self.choice = QLineEdit()
        self.choice.setPlaceholderText("number")
        self.choice.setMaximumWidth(90)
        self.choice.returnPressed.connect(self.run_choice)
        run = QPushButton("Run on ticked devices")
        run.setObjectName("Primary")
        run.clicked.connect(lambda: self.run_item(self.menu.currentItem()))
        mrow.addWidget(self.choice)
        mrow.addStretch(1)
        mrow.addWidget(run)
        mlayout.addLayout(mrow)
        top.addWidget(menu_box)
        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 2)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(top)
        log_box = QWidget()
        llayout = QVBoxLayout(log_box)
        llayout.setContentsMargins(0, 0, 0, 0)
        lrow = QHBoxLayout()
        lrow.addWidget(QLabel("Log"))
        lrow.addStretch(1)
        clear = QPushButton("Clear")
        lrow.addWidget(clear)
        llayout.addLayout(lrow)
        self.log = HookLog()
        clear.clicked.connect(self.log.clear)
        llayout.addWidget(self.log, 1)
        split.addWidget(log_box)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)

        self.launcher = HookLauncher(self.wf, self.log, self)
        self.reload_hooks()

    @property
    def runs(self) -> list[HookRun]:
        """Runs still in progress."""
        return self.launcher.runs

    # -- menu -------------------------------------------------------------
    def reload_hooks(self) -> None:
        """Re-import the hook modules and rebuild the numbered menu."""
        self.options, problems = load_options(self.ctx.menu_hooks)
        self.menu.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        for number, option in enumerate(self.options, start=1):
            group = groups.get(option.category)
            if group is None:
                group = QTreeWidgetItem(["", option.category])
                group.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.menu.addTopLevelItem(group)
                groups[option.category] = group
            inputs = ", ".join(p.label or p.name for p in option.params)
            item = QTreeWidgetItem([str(number), option.title, inputs])
            item.setData(0, Qt.ItemDataRole.UserRole, number - 1)
            item.setToolTip(1, option.description)
            group.addChild(item)
        self.menu.expandAll()
        for column in range(3):
            self.menu.resizeColumnToContents(column)
        if not self.ctx.menu_hooks:
            self.log.append("warn", "No hook modules: add one to project.json -> menu_hooks "
                                    "(see plugins/example_menu.py).")
        for problem in problems:
            self.log.append("error", f"hooks: {problem}")
        self.log.append("info", f"loaded {len(self.options)} option(s) from {', '.join(self.ctx.menu_hooks) or 'nothing'}")

    def _show_description(self, item: QTreeWidgetItem | None) -> None:
        """Show the selected option's help text.

        Args:
            item: Selected menu row, or None.
        """
        option = self._option_for(item)
        self.description.setText(option.description if option else "")

    def _option_for(self, item: QTreeWidgetItem | None) -> MenuOption | None:
        """Return the option behind a menu row.

        Args:
            item: Menu row, or None.

        Returns:
            The option, or None for a category row.
        """
        index = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        return self.options[index] if index is not None else None

    # -- devices ----------------------------------------------------------
    def ticked_peers(self) -> list[str]:
        """Return the addresses of the ticked devices."""
        return self.targets.ticked_peers()

    def set_targets(self, peers: list[str]) -> None:
        """Tick exactly these devices (used by the workflow's "Operations for selected").

        Args:
            peers: Device addresses to tick.
        """
        self.targets.set_targets(peers)

    # -- running ----------------------------------------------------------
    def run_choice(self) -> None:
        """Run the option whose number was typed, like a console menu."""
        text = self.choice.text().strip()
        if not text.isdigit() or not 1 <= int(text) <= len(self.options):
            show_error(self, "Choice", f"Enter a number from 1 to {len(self.options)}.")
            return
        self.choice.clear()
        self.run_option(self.options[int(text) - 1])

    def run_item(self, item: QTreeWidgetItem | None) -> None:
        """Run the option behind a menu row.

        Args:
            item: Menu row; category rows are ignored.
        """
        option = self._option_for(item)
        if option is None:
            show_error(self, "Run", "Select an option in the menu first.")
            return
        self.run_option(option)

    def run_option(self, option: MenuOption, values: dict[str, Any] | None = None) -> list[HookRun]:
        """Ask for the option's inputs, then start it on every ticked device.

        Args:
            option: Option to run.
            values: Input values; None asks with a dialog when the option has inputs.

        Returns:
            The started runs (empty if nothing ran).
        """
        targets = self.targets.ticked()
        problem = self.launcher.problem(targets)
        if problem:
            show_error(self, option.title, problem)
            return []
        if values is None:
            values = self.launcher.ask_values(option, self)
            if values is None:
                return []
        return self.launcher.start(option, targets, values)

    def stop_all(self) -> None:
        """Stop every running hook; requests already queued still complete."""
        self.launcher.stop_all()

    def append_log(self, level: str, text: str) -> None:
        """Add a line to the log.

        Args:
            level: ``info``, ``ok``, ``warn`` or ``error``.
            text: Message.
        """
        self.log.append(level, text)
