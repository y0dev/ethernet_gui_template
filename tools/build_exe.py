"""Package the application as a standalone executable that needs no Python.

    python tools/build_exe.py [--onefile] [--name NAME] [--project PATH]
                              [--console] [--hidden-import MODULE ...]
                              [--no-smoke-test] [--clean]

Uses PyInstaller (``pip install -r requirements-build.txt``) and produces
``dist/<NAME>/``, a folder you can zip and copy to a machine without Python:

    dist/<NAME>/
        <NAME>.exe          the application (Python, Qt and app/ are inside)
        _internal/          libraries the executable loads (not with --onefile)
        project.json        the profile it was built with -- still editable
        plugins/            plugin pages, loaded from source at start-up
        sql/                named queries (every relative folder in sql_dirs)
        resources/          default config, command sets, icons
        data/               databases, logs, reports and config.json at run time

The files next to the executable are read exactly as they are from a source
checkout, so pages, queries and colours can still be changed without
rebuilding. A plugin may only import modules that are inside the executable:
the build scans the profile's plugins for imports and bundles those modules,
so after adding a plugin that imports something new, build again (or name it
with ``--hidden-import``).

PyInstaller builds for the system it runs on: build on Windows for a Windows
``.exe``, on Linux for a Linux binary.

``--onefile`` packs everything into the single executable instead of
``_internal/``. It starts more slowly, because it unpacks itself to a
temporary folder on every launch; the editable files above still sit beside it.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.project import ProjectProfile, load_project, project_path  # noqa: E402

BUILD_DIR = ROOT / "build" / "pyinstaller"
DIST_DIR = ROOT / "dist"

#: Folders inside app/ that hold data files read at run time via ``__file__``.
APP_DATA_DIRS = ["app/core/database/schema", "app/core/database/sql"]

#: Always bundled: the SVG plugins Qt needs to draw .svg window icons.
ALWAYS_HIDDEN = ["PySide6.QtSvg"]


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the build options.

    Args:
        argv: Command-line arguments, excluding the program name.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(description="Build a standalone executable with PyInstaller.")
    parser.add_argument("--onefile", action="store_true",
                        help="one self-extracting executable instead of an executable plus _internal/")
    parser.add_argument("--name", help="executable name (default: app_name from the profile, without spaces)")
    parser.add_argument("--project", help="profile to build with and ship as project.json (default project.json)")
    parser.add_argument("--console", action="store_true",
                        help="keep a console window for log output (useful when a build will not start)")
    parser.add_argument("--hidden-import", action="append", default=[], metavar="MODULE",
                        help="extra module to bundle, e.g. one a plugin imports indirectly (repeatable)")
    parser.add_argument("--no-smoke-test", action="store_true", help="skip starting the built executable")
    parser.add_argument("--clean", action="store_true", help="discard PyInstaller's cache before building")
    return parser.parse_args(argv)


def exe_name(profile: ProjectProfile, override: str | None) -> str:
    """Choose a file-system-safe executable name.

    Args:
        profile: Project profile supplying ``app_name``.
        override: Name given with ``--name``, if any.

    Returns:
        The name without extension.
    """
    name = override or profile.app_name.replace(" ", "")
    name = re.sub(r'[<>:"/\\|?*]', "", name).strip(". ")
    if not name:
        raise SystemExit("error: the executable name is empty; pass --name")
    return name


def make_icon(profile: ProjectProfile) -> Path | None:
    """Convert the profile's icon to ``.ico`` for the Windows executable.

    Other systems take the window icon from the profile at run time, so the
    executable itself gets none.

    Args:
        profile: Project profile naming the icon.

    Returns:
        The ``.ico`` file to embed, or None.
    """
    source = profile.icon_path()
    if sys.platform != "win32" or source is None or not source.is_file():
        return None
    if source.suffix.lower() == ".ico":
        return source
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QGuiApplication, QIcon

    _app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 (needed to render)
    pixmap = QIcon(str(source)).pixmap(QSize(256, 256))
    target = BUILD_DIR / "app.ico"
    target.parent.mkdir(parents=True, exist_ok=True)
    if pixmap.isNull() or not pixmap.save(str(target), "ICO"):
        print(f"warning: could not convert {source} to .ico; the executable keeps the default icon")
        return None
    return target


def module_file(module: str) -> Path | None:
    """Find the source of a project module such as ``plugins.example_queries``.

    Args:
        module: Dotted module name.

    Returns:
        The ``.py`` file or package folder under the project root, or None.
    """
    base = ROOT.joinpath(*module.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base
    return None


def plugin_imports(profile: ProjectProfile) -> list[str]:
    """List the modules the profile's plugins import, so they get bundled.

    Plugins stay as source files beside the executable, so PyInstaller never
    sees them; their imports are found here and bundled explicitly.

    Args:
        profile: Project profile listing the plugins.

    Returns:
        Sorted absolute module names, excluding ``app`` and the plugins themselves.
    """
    local = {name.split(".")[0] for name in profile.plugins}
    found: set[str] = set()
    for name in profile.plugins:
        path = module_file(name)
        if path is None:
            print(f"warning: plugin {name} not found under {ROOT}; it is not shipped")
            continue
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for file in files:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    found.add(node.module)
    return sorted(m for m in found if m.split(".")[0] not in local | {"app", "__future__"})


def run_pyinstaller(args: argparse.Namespace, name: str, icon: Path | None, hidden: list[str]) -> Path:
    """Run PyInstaller on ``main.py``.

    Args:
        args: Build options.
        name: Executable name.
        icon: ``.ico`` to embed, or None.
        hidden: Extra modules to bundle.

    Returns:
        The output folder that holds the executable.
    """
    try:
        import PyInstaller.__main__
    except ImportError:
        raise SystemExit("error: PyInstaller is not installed. Run: pip install -r requirements-build.txt") from None

    out = DIST_DIR / name
    if out.exists():
        shutil.rmtree(out)
    # A one-folder build creates dist/<name>/ itself; a one-file build only
    # writes the executable, so point it straight at dist/<name>/.
    distpath = out if args.onefile else DIST_DIR
    command = [
        str(ROOT / "main.py"),
        "--name", name,
        "--noconfirm",
        "--distpath", str(distpath),
        "--workpath", str(BUILD_DIR / "work"),
        "--specpath", str(BUILD_DIR),
        "--paths", str(ROOT),
        # Built-in pages are imported by name ("app.ui.pages.x:Class"), which
        # PyInstaller cannot follow, so bundle every module of the app package.
        "--collect-submodules", "app",
        "--exclude-module", "tkinter",
        "--onefile" if args.onefile else "--onedir",
        "--console" if args.console else "--windowed",
    ]
    for folder in APP_DATA_DIRS:
        command += ["--add-data", f"{ROOT / folder}{os.pathsep}{folder}"]
    for module in hidden:
        command += ["--hidden-import", module]
    if icon is not None:
        command += ["--icon", str(icon)]
    if args.clean:
        command.append("--clean")
    PyInstaller.__main__.run(command)
    return out


def copy_project_files(profile: ProjectProfile, profile_file: Path, out: Path) -> None:
    """Place the editable project files next to the executable.

    Args:
        profile: Project profile being shipped.
        profile_file: Its source file (may not exist: the defaults are used).
        out: Folder that holds the executable.
    """
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "local_*.py")

    def copy_dir(relative: Path) -> None:
        source = ROOT / relative
        if source.is_dir():
            shutil.copytree(source, out / relative, ignore=ignore, dirs_exist_ok=True)

    if profile_file.is_file():
        shutil.copy2(profile_file, out / "project.json")
    copy_dir(Path("resources"))
    for name in profile.plugins:
        copy_dir(Path(name.split(".")[0]))
    for value in profile.sql_dirs:
        if not Path(value).expanduser().is_absolute():
            copy_dir(Path(value))
    icon = Path(profile.icon) if profile.icon else None
    if icon is not None and not icon.is_absolute() and (ROOT / icon).is_file():
        (out / icon).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / icon, out / icon)
    (out / "data").mkdir(exist_ok=True)


def smoke_test(executable: Path) -> bool:
    """Start the built executable headless, visit every page and check it exits cleanly.

    Runtime files go to a temporary folder so the build output stays clean.

    Args:
        executable: The built executable.

    Returns:
        True if it exited with status 0.
    """
    with tempfile.TemporaryDirectory(prefix="ewb_smoke_") as tmp:
        config = Path(tmp) / "config.json"
        keys = {"database_path": "smoke.db", "log_dir": "logs", "backup_dir": "backups", "reports_dir": "reports"}
        config.write_text(json.dumps({key: str(Path(tmp, sub)) for key, sub in keys.items()}), encoding="utf-8")
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "EWB_CONFIG": str(config)}
        try:
            result = subprocess.run([str(executable), "--smoke-test"], env=env, timeout=180,
                                    capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            print("smoke test: timed out after 180 s")
            return False
        if result.returncode != 0:
            print(f"smoke test: exit status {result.returncode}")
            print(result.stdout[-4000:], result.stderr[-4000:], sep="\n")
            log_dir = Path(tmp, "logs")
            for log_file in sorted(log_dir.glob("*")) if log_dir.is_dir() else []:
                print(f"--- {log_file.name} ---\n{log_file.read_text(encoding='utf-8', errors='replace')[-4000:]}")
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    """Build the executable, ship the project files beside it and smoke-test it.

    Args:
        argv: Command-line arguments, excluding the program name.

    Returns:
        0 on success, 1 if the smoke test failed.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    profile_file = project_path(args.project).resolve()
    profile = load_project(profile_file)
    name = exe_name(profile, args.name)
    hidden = sorted(set(ALWAYS_HIDDEN + plugin_imports(profile) + args.hidden_import))
    print(f"building {name} from {profile_file if profile_file.is_file() else 'the default profile'}")
    print(f"bundling for plugins: {', '.join(hidden)}")

    out = run_pyinstaller(args, name, make_icon(profile), hidden)
    copy_project_files(profile, profile_file, out)
    executable = out / (name + (".exe" if sys.platform == "win32" else ""))
    if not executable.is_file():
        print(f"error: PyInstaller did not produce {executable}")
        return 1

    if not args.no_smoke_test:
        print("smoke test: starting the executable headless...")
        if not smoke_test(executable):
            return 1
        print("smoke test: passed")
    print(f"\ndone: {executable}\nCopy or zip the whole folder {out} to run it on another machine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
