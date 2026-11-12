# Build a standalone executable

Package the workbench so it runs on a machine **without Python**: copy one
folder, double-click the `.exe`.

## 1. Install the build tool (once)

```bash
pip install -r requirements-build.txt
```

This adds [PyInstaller](https://pyinstaller.org) to your development
environment. The machines you ship to need nothing.

## 2. Build

```bash
python tools/build_exe.py
```

The build takes a few minutes. At the end it starts the new executable
headless, visits every page and checks it exits cleanly, then prints:

```text
smoke test: passed
done: ...\dist\EmbeddedSystemsWorkbench\EmbeddedSystemsWorkbench.exe
```

## 3. Ship the folder

Zip and copy the **whole** `dist/<name>/` folder, not just the `.exe`:

```text
dist/EmbeddedSystemsWorkbench/
    EmbeddedSystemsWorkbench.exe   the application (Python, Qt and app/ are inside)
    _internal/                     libraries the executable needs
    project.json                   name, pages, plugins, colours: still editable
    plugins/                       your pages, loaded from source at start-up
    sql/                           your named queries
    resources/                     default config, command sets, icons
    data/                          empty; databases, logs, reports appear here
```

The files beside the executable work exactly as in a source checkout: edit
`project.json`, a plugin or a `.sql` file and restart the program; no rebuild
needed. Each machine keeps its own settings and databases in `data/`.

## Options

| Option | Effect |
|---|---|
| `--onefile` | Everything in one `.exe` (no `_internal/`). Starts more slowly: it unpacks itself on every launch. |
| `--name NAME` | Executable name. Default: `app_name` from the profile, without spaces. |
| `--project profiles/x.json` | Build with another profile; it is shipped as `project.json`. |
| `--console` | Keep a console window showing the log. Use it when a build will not start. |
| `--hidden-import MODULE` | Bundle an extra module (see below). Repeatable. |
| `--no-smoke-test` | Skip the headless start-up check. |
| `--clean` | Discard PyInstaller's cache first. |

The window and taskbar icon come from `icon` in `project.json`; the build also
converts it to `.ico` for the `.exe` file itself.

## Plugins in a packaged build

Plugins stay as `.py` files next to the executable, but they can only import
modules that are **inside** it. The build reads the profile's plugins and
bundles every module they import. So:

* **Changed a plugin's code?** Just copy the new `.py` into `plugins/`.
* **A plugin now imports something new** (another Qt module, a library you
  installed with pip)? Build again. If the import is hidden, for example done
  with `importlib`, name it: `--hidden-import PySide6.QtCharts`.
* **Added a new plugin?** Build again so its imports are bundled.

## What does not work in the executable

* **Test Bench → Run tests** needs Python with pytest on `PATH`, because the
  test suites are not inside the executable. Without it the page says so.
* **`tools/*.py`** (new page, new migration, device simulator) are development
  tools; run them from a source checkout.

## Check

* `dist/<name>/` contains the executable and the folders listed above.
* On a machine without Python, the program starts and shows your pages.
* If it does not start, build with `--console` and read the error in the
  console window, or the log in `data/logs/`.

PyInstaller builds for the system it runs on: build on Windows for Windows,
on Linux for Linux.
