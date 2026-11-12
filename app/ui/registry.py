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
    ("operations_menu", "Operations Menu", "app.ui.pages.operations_menu:OperationsMenuPage"),
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
        """Return the page factory, importing a ``"module:Class"`` string on first use.

        Returns:
            The callable that builds the page widget from the context.

        Raises:
            ImportError: If the page's module cannot be imported.
            AttributeError: If the module has no such class.
        """
        if callable(self.factory):
            return self.factory
        module_name, _, attr = self.factory.partition(":")
        module = importlib.import_module(module_name)
        factory = getattr(module, attr)
        self.factory = factory
        return factory


class PageRegistry:
    def __init__(self, include_builtins: bool = True) -> None:
        """Create a registry, optionally pre-filled with the built-in pages.

        Args:
            include_builtins: Register every entry of ``BUILTIN_PAGES``.
        """
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
        """Register a page.

        Args:
            key: Unique identifier used in ``project.json``.
            title: Navigation title.
            factory: Widget factory taking the context, or a ``"module:Class"``
                string imported lazily.
            description: Text shown in the page list.
            replace: Allow replacing an existing page, such as a built-in.

        Raises:
            RegistryError: If the key is not an identifier, or is already taken
                and ``replace`` is False.
        """
        if not key.isidentifier():
            raise RegistryError(f"page key {key!r} must be an identifier (letters, digits, _)")
        if key in self._pages and not replace:
            raise RegistryError(f"page '{key}' is already registered (pass replace=True to override)")
        if key not in self._pages:
            self._order.append(key)
        self._pages[key] = PageSpec(key, title, factory, description, self._current_origin)

    def remove_page(self, key: str) -> None:
        """Unregister a page. Unknown keys are ignored.

        Args:
            key: Page key.
        """
        self._pages.pop(key, None)
        if key in self._order:
            self._order.remove(key)

    def on_startup(self, hook: Callable[[Any], None]) -> None:
        """Register a hook called as ``hook(ctx)`` once the services exist.

        Hooks run before the main window is created.

        Args:
            hook: Callable receiving the application context.
        """
        self.startup_hooks.append(hook)

    # -- queries ----------------------------------------------------------
    def keys(self) -> list[str]:
        """List the registered page keys.

        Returns:
            The keys in registration order.
        """
        return list(self._order)

    def get(self, key: str) -> PageSpec | None:
        """Look up a registered page.

        Args:
            key: Page key.

        Returns:
            The page spec, or None if not registered.
        """
        return self._pages.get(key)

    def load_plugins(self, modules: Iterable[str]) -> list[str]:
        """Import each plugin module and call its ``register(registry)`` function.

        A failing plugin is reported and skipped, never raised.

        Args:
            modules: Importable module names.

        Returns:
            One problem description per plugin that failed.
        """
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
        """Choose the pages to show, in order, and collect warnings.

        With an explicit list, plugin pages that are not listed are appended,
        so a new plugin appears without editing the list. Built-ins that are
        not listed are hidden. Pages whose factory cannot be imported are
        skipped with a warning.

        Args:
            keys: Page keys in navigation order, or None for every page.

        Returns:
            The page specs to show and the warning messages.
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
