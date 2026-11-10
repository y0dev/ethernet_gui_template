"""Page registry: what the navigation can show.

Built-in pages are referenced by ``"module:Class"`` strings and imported only
when used.  Deleting a page's module therefore does not break start-up: the
page is skipped with a warning (remove its key from ``project.json`` to
silence it).

Plugins add pages (and start-up hooks) through :class:`PageRegistry`::

    # plugins/my_page.py
    def register(registry):
        registry.add_page("my_page", "My Page", MyPage, description="...")

where ``MyPage(ctx)`` is a QWidget factory receiving the :class:`AppContext`.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

PageFactory = Callable[[Any], QWidget]

#: (key, navigation title, "module:Class") in default order
BUILTIN_PAGES: tuple[tuple[str, str, str], ...] = (
    ("workflow", "Session Workflow", "app.ui.pages.workflow:WorkflowPage"),
    ("dashboard", "Dashboard", "app.ui.pages.dashboard:DashboardPage"),
    ("devices", "Device Manager", "app.ui.pages.devices:DevicesPage"),
    ("connections", "Network Connections", "app.ui.pages.connections:ConnectionsPage"),
    ("packet_monitor", "Packet Monitor", "app.ui.pages.packet_monitor:PacketMonitorPage"),
    ("test_bench", "Test Bench", "app.ui.pages.test_bench:TestBenchPage"),
    ("porting", "C-to-Python Porting", "app.ui.pages.porting:PortingPage"),
    ("database", "Database Explorer", "app.ui.pages.database_explorer:DatabaseExplorerPage"),
    ("logs", "Application Logs", "app.ui.pages.logs:LogsPage"),
    ("settings", "Settings", "app.ui.pages.settings:SettingsPage"),
)


class RegistryError(ValueError):
    pass


@dataclass
class PageSpec:
    key: str
    title: str
    factory: PageFactory | str
    description: str = ""
    origin: str = "built-in"

    def resolve(self) -> PageFactory:
        """The factory; imports ``"module:Class"`` on first use."""
        if callable(self.factory):
            return self.factory
        module_name, _, attr = self.factory.partition(":")
        module = importlib.import_module(module_name)
        factory = getattr(module, attr)
        self.factory = factory
        return factory


class PageRegistry:
    def __init__(self, include_builtins: bool = True) -> None:
        self._pages: dict[str, PageSpec] = {}
        self._order: list[str] = []
        self.startup_hooks: list[Callable[[Any], None]] = []
        self._current_origin = "built-in"
        if include_builtins:
            for key, title, target in BUILTIN_PAGES:
                self.add_page(key, title, target)

    # -- plugin API -------------------------------------------------------
    def add_page(self, key: str, title: str, factory: PageFactory | str, description: str = "",
                 replace: bool = False) -> None:
        """Register a page.  ``replace=True`` swaps a built-in for your own."""
        if not key.isidentifier():
            raise RegistryError(f"page key {key!r} must be an identifier (letters, digits, _)")
        if key in self._pages and not replace:
            raise RegistryError(f"page '{key}' is already registered (pass replace=True to override)")
        if key not in self._pages:
            self._order.append(key)
        self._pages[key] = PageSpec(key, title, factory, description, self._current_origin)

    def remove_page(self, key: str) -> None:
        self._pages.pop(key, None)
        if key in self._order:
            self._order.remove(key)

    def on_startup(self, hook: Callable[[Any], None]) -> None:
        """Call ``hook(ctx)`` once the services exist (before the window)."""
        self.startup_hooks.append(hook)

    # -- queries ----------------------------------------------------------
    def keys(self) -> list[str]:
        return list(self._order)

    def get(self, key: str) -> PageSpec | None:
        return self._pages.get(key)

    def load_plugins(self, modules: Iterable[str]) -> list[str]:
        """Import each plugin and call its ``register(registry)``; returns problems."""
        problems = []
        for name in modules:
            self._current_origin = name
            try:
                module = importlib.import_module(name)
                register = getattr(module, "register", None)
                if register is None:
                    raise RegistryError("module has no register(registry) function")
                register(self)
                log.info("plugin loaded", extra={"context": {"plugin": name}})
            except Exception as exc:
                problems.append(f"{name}: {type(exc).__name__}: {exc}")
                log.error("plugin failed to load", extra={"context": {"plugin": name, "error": str(exc)}})
            finally:
                self._current_origin = "built-in"
        return problems

    def select(self, keys: list[str] | None) -> tuple[list[PageSpec], list[str]]:
        """Pages to show, in order, and warnings.

        ``keys=None`` shows every registered page.  With an explicit list, pages
        registered by plugins but not listed are appended (so a new plugin shows
        up without editing the list); built-ins not listed are hidden.
        """
        warnings: list[str] = []
        if keys is None:
            chosen = list(self._order)
        else:
            chosen = []
            for key in keys:
                if key in self._pages:
                    chosen.append(key)
                else:
                    warnings.append(f"project.json lists unknown page '{key}' (available: {', '.join(self._order)})")
            chosen += [k for k in self._order if k not in chosen and self._pages[k].origin != "built-in"]
        specs = []
        for key in chosen:
            spec = self._pages[key]
            try:
                spec.resolve()
            except Exception as exc:  # e.g. the page's module was deleted
                warnings.append(f"page '{key}' skipped: {type(exc).__name__}: {exc}")
                continue
            specs.append(spec)
        for message in warnings:
            log.warning(message)
        return specs, warnings
