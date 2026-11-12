# Using this project as a template

Everything you normally change lives in a few places:

| What | Where |
|---|---|
| Name, icon, pages, page order, start page, plugins, SQL folders, colours | `project.json` |
| Your SQL queries | `sql/*.sql` |
| Your database tables | `app/core/database/schema/NNNN_*.sql` |
| Your pages | `plugins/*.py` |
| Per-user runtime settings (paths, server IP/port, limits) | `data/config.json` (git-ignored, edited in Settings) |
| Local experiments, never committed | `plugins/local_*.py`, `*.local.json`, `*_old.py`, `scratch/` (see `.gitignore`) |

`project.json` is committed with your project; `data/config.json` is per machine.

`.gitignore` is an allowlist: only the folders and file types it lists are
committed. Add a line there when you add a new kind of file (see
[the guides](docs/guides/README.md#where-things-live)).

This file is the reference. For step-by-step walkthroughs with working code
(a database page, your own page, device operations, decoders, branding), see
the **[guides](docs/guides/README.md)**.

---

## 1. Rename, re-colour and change the icon

```json
{
  "app_name": "Acme Gateway Bench",
  "app_subtitle": "production test",
  "organization": "Acme",
  "icon": "resources/icons/acme.svg",
  "theme": { "accent": "#ff8a00", "ok": "#22c55e", "background": "#101418" }
}
```

Theme keys: `background, panel, input, border, text, text_dim, accent,
accent_dim, ok, warning, error, rx, tx`. Invalid keys or colours stop start-up
with a clear message. Also rename `name` in `pyproject.toml` if you publish it.

`icon` is the window and taskbar icon: an `.svg`, `.png` or `.ico` file,
relative to the project root. The default is `resources/icons/app.svg`; an
empty string uses Qt's default icon. SVG stays sharp at every size. On
Windows the app registers its own taskbar identity (from `organization` and
`app_name`), so the taskbar shows your icon instead of Python's. A missing
icon file is logged and the app still starts.

## 2. Hide, reorder or choose the start page

```json
"pages": ["workflow", "devices", "database", "logs", "settings"],
"start_page": "workflow"
```

Built-in page keys:

| Key | Page |
|---|---|
| `workflow` | Session Workflow (server → database → devices → operations → report) |
| `operations_menu` | Operations Menu: numbered options that run your Python hooks (`menu_hooks`, see [the guide](docs/guides/menu-hooks.md)). `"workflow_operations": "menu"` also puts it in step 4 of the Session Workflow. |
| `dashboard` | Dashboard |
| `devices` | Device Manager |
| `connections` | Network Connections |
| `packet_monitor` | Packet Monitor |
| `test_bench` | Test Bench |
| `porting` | C-to-Python Porting |
| `database` | Database Explorer |
| `logs` | Application Logs |
| `settings` | Settings |

* A key not in the list is hidden (its code stays, nothing else changes).
* Plugin pages you don't list are added at the end automatically.
* Remove `"pages"` entirely to show every page.

This is the recommended way to "remove" a feature: no code changes, and you
can bring it back later.

## 3. Add a page

```bash
python tools/new_page.py calibration "Calibration"
```

This writes `plugins/calibration.py` (a working page with a table and a
named-query call) and adds it to `project.json` (`plugins` and `pages`). Edit
the file and restart.

### Start from a template

Pick a different starting point with `--template` (`-t`):

```bash
python tools/new_page.py calibration "Calibration" --template query_form
python tools/new_page.py --list-templates
```

| Template | Starting point |
|---|---|
| `table` (default) | Refresh button and a table filled from a named query |
| `blank` | Header only, for building your own layout |
| `query_form` | One input per `:parameter` of a named query, a Run button and a result table. Set `QUERY` at the top of the file. |
| `live_packets` | Live table of traffic on every connection, with pause and clear. Filter it in `accepts()`. |
| `device_console` | Send HEX/ASCII to an open connection and log its replies. Change the display in `format_frame()`. |
| `workflow` | Guided step-by-step page: a step bar, Back/Next, and steps that unlock in order. Each step is a `Step` subclass (`build()`, `is_done()`, `refresh()`); `STEPS` sets the order. Ships with three example steps to replace. |
| `device_workflow` | A shorter, editable Session Workflow on the same services: Server → Devices → Run → Finish, with a read-only database browser. Results land in the same tables and reports as the built-in page. Add or remove steps in `STEPS`. |
| `menu_console` / `menu_buttons` / `menu_form` / `menu_sequence` | The menu hooks as a C-style console, a button panel, a form, or a test sequence with a results grid. Add `--workflow` to make the page step 4 of the Session Workflow. Screenshots: [Pick a layout](docs/guides/menu-templates.md). |
| `db_table` | View and edit a database table: search, paging, inline editing, Save/Revert. Used automatically with `--table`, see [View and edit a database table](#view-and-edit-a-database-table). |

**Your own templates.** Put `<name>.py.tmpl` files in `page_templates/` at
the project root and use them with `--template <name>`. A file named like a
built-in (for example `table.py.tmpl`) replaces it for your project, and
`--template path/to/file.py.tmpl` uses any file directly. A template is
plain Python with three placeholders:

| Placeholder | Becomes |
|---|---|
| `$key` | the page key, e.g. `calibration` |
| `$title` | the navigation title, e.g. `Calibration` |
| `$cls` | the class name, e.g. `CalibrationPage` |

Write `$$` for a literal `$`. An optional first line
`# template: <description>` is shown by `--list-templates` and left out of
the generated file. The easiest start is to copy one of
`tools/page_templates/*.py.tmpl`. The template must define
`register(registry)`, which calls `registry.add_page("$key", "$title", $cls)`.
The tool refuses a template with an unknown placeholder, or one that
produces invalid Python, before it writes anything.

### What a page can use

The page receives `ctx`, which holds every service:

| `ctx.` | Use |
|---|---|
| `db` | `db.run("query.name", x=1)`, `db.run_one`, `db.run_scalar`, `db.run_write`, `db.transaction()` |
| `workflow` | the Session Workflow (server, devices, sessions, operations) |
| `connections` | open/close/send on connections, `packet` signal for every frame |
| `capture` | live packets, recording sessions |
| `operations` | run device operations, `result_ready` signal |
| `devices`, `protocols`, `porting`, `tests`, `config` | the other services |

A plugin's `register(registry)` can also:

```python
registry.add_page("devices", "My Devices", MyDevicesPage, replace=True)  # replace a built-in page
registry.on_startup(lambda ctx: ...)   # run code once services exist (e.g. start your own service)
```

`plugins/example_queries.py` is a complete example; delete it and its two
entries in `project.json` when you no longer need it.

### Update a generated page

`tools/new_page.py` runs once to create the starting file. After that,
`plugins/<key>.py` is your code: edit it directly and restart the
application (pages are not reloaded while it runs). The tool never
overwrites an existing page.

| To change | Do this |
|---|---|
| What the page shows or does | Edit the class in `plugins/<key>.py`. Put logic you want to test in a service or plain function, not in the widget. |
| The navigation title | Change the second argument of `registry.add_page(...)` in `register()`. The `PageHeader(...)` title at the top of the page is separate, so update it too. |
| Where it appears in the navigation | Move the key within `"pages"` in `project.json`. Set `"start_page"` to open it at start-up. |
| The page key | Rename the file, then change the key in `add_page(...)`, the `"plugins.<key>"` entry and the `"pages"` entry in `project.json`. |
| Hide it without deleting it | Remove `"plugins.<key>"` from `"plugins"`. Removing it from `"pages"` alone is not enough: plugin pages that are not listed are added at the end automatically. |
| Remove it | Delete `plugins/<key>.py` and both of its `project.json` entries. |
| Start again from a template | Move your file aside (for example to `<key>_old.py`), run `python tools/new_page.py <key> "<Title>" --template <name>` again, then copy your code across. Re-running does not duplicate the `project.json` entries. |
| Pages you create later | Change or add a template in `page_templates/`. Pages that already exist are not affected. |

Use `--no-enable` to create the file without touching `project.json`, and
add the two entries yourself when the page is ready.

After any of these changes, check the result:

```bash
python -m pytest tests/unit/test_template.py::test_shipped_project_json_is_valid   # project.json names real pages and plugins
python main.py --smoke-test                                                          # every page opens
```

New functions should keep the Google-style docstrings that the generated
file starts with (summary line, then `Args:` / `Returns:` / `Raises:`).

## 4. Define SQL once, use it anywhere

Put queries in any `.sql` file in `sql/` (folders are listed in
`project.json` → `sql_dirs`):

```sql
-- name: calibration.latest
-- Most recent calibration per device.
SELECT device_id, MAX(taken_at) AS taken_at FROM calibration GROUP BY device_id;

-- name: calibration.for_device
SELECT * FROM calibration WHERE device_id = :device_id ORDER BY taken_at DESC;
```

* `-- name:` starts a query; `--` lines right after it are its description.
* Parameters are `:name`; one statement per query.
* Call from code: `rows = ctx.db.run("calibration.for_device", device_id=3)`.
* Try them in **Database Explorer → Saved queries**: pick a query, fill the
  parameters, Run. The explorer uses a read-only connection, so it can never
  modify data; it also shows the exact Python call to copy. After editing a
  `.sql` file, click *Reload .sql files*. Saved queries always run against
  the application database, even while the Tables tab browses another file.
* The application's own queries are in `app/core/database/sql/workbench.sql`.
  To change one, define a query with the same name in `sql/`; your version
  wins and the override is logged.

## 5. Add tables (schema migrations)

```bash
python tools/new_migration.py "add calibration table"
```

This creates `app/core/database/schema/0004_add_calibration_table.sql`. Write
`CREATE TABLE`/`ALTER TABLE` statements in it (end each with `;`). Every
database is upgraded on start-up, and when opened in the Session Workflow.

Rules: files are numbered consecutively; never edit a migration that has
already been applied to a database you care about (add a new one, e.g.
`ALTER TABLE …` or `DROP TABLE …`); a file with only comments is rejected.

### View and edit a database table

One command makes a page for viewing and editing a table:

```bash
# a table in the application database (e.g. one added by a migration)
python tools/new_page.py calibration "Calibration" --table calibration

# a table in your own SQLite file, which is left exactly as it is
python tools/new_page.py plant "Plant data" --table readings --database data/plant.db

# no --table: the page shows a chooser with every table of the database
python tools/new_page.py tables "Tables" --template db_table
```

The page shows the table in a grid:

* **Search and paging.** Search matches text in any column.
* **Editing.** Double-click a cell to edit it. Empty means NULL (or `''` for
  a `NOT NULL` text column), numbers are checked against the column type,
  and BLOBs are typed as hex. **Add row** fills unset columns with their
  defaults on save, and **Delete row** marks rows (select again to unmark).
* **Saving.** Changed cells, new rows and deleted rows stay coloured until
  you **Save**, which writes them all in one transaction. If a constraint
  fails (for example a `UNIQUE` column), nothing is written and your edits
  stay so you can fix them. **Revert** discards them.

Set `READ_ONLY = True` at the top of the generated file for a view-only
page. The grid is `app.ui.widgets.table_editor.TableEditor`, so you can also
put it in your own page: `TableEditor(ctx.db, "calibration")`.

Rows are found by `rowid`, or by the primary key for `WITHOUT ROWID` tables.
Table and column names come from the database schema and are never built
from what users type, and values always go through SQL parameters.

### Your own database vs. the application database

The application database (`database_path` in Settings, or the file chosen
in the Session Workflow) stores the workbench's sessions, packets, devices
and logs. When the app opens a file as its application database, it adds
the workbench's tables to that file. To keep a database of your own clean,
**don't** make it the application database. Instead:

* **To look at it:** open it with **Database Explorer → Open file…**. Neither
  tables nor settings are added.
* **To work with it:** generate a page with `--database path/to/your.db`.
  Like the explorer, the page opens the file as it is.

The Database Explorer hides the workbench's own tables by default, so a
database shows only its own tables. Tick **Show workbench tables** to see
them. The hidden names are listed in `WORKBENCH_TABLES`
(`app/core/database/migrations.py`). Tables your migrations add are always
shown.

## 6. Remove a feature's code entirely

Hiding a page (section 2) is usually enough. If you want the code gone, these
steps were tried on a copy of this project; after each removal the remaining
tests pass and the app starts. Finish every removal with
`ruff check . --fix && pytest`.

Database tables are **not** removed: older databases keep working. Drop them
with a new migration if you want.

### C-to-Python Porting

1. Delete `app/ui/pages/porting.py`, `app/core/porting/`,
   `app/core/services/porting_service.py`, `tests/unit/test_porting.py`,
   `docs/PORTING_GUIDE.md`.
2. `project.json`: remove `"porting"` from `pages`.
3. `app/core/services/__init__.py`: remove the `porting_service` import and
   `ImportSummary`, `PortingRuleError`, `PortingService` from `__all__`.
4. `app/context.py`: remove `PortingService` from the import, the
   `porting: PortingService` field and `porting=PortingService(db),`.
5. `app/ui/registry.py`: remove the `("porting", …)` line from `BUILTIN_PAGES`.
6. `tests/integration/test_gui.py`: delete `test_porting_page_lists_modules`.
7. Optional: remove the "Porting status" panel from `app/ui/pages/dashboard.py`.

### Session Workflow

1. Delete `app/ui/pages/workflow.py`, `app/core/services/workflow_service.py`,
   `app/core/services/operation_runner.py`, `app/core/reporting/`,
   `tools/device_simulator.py`, `resources/command_sets/`,
   `tests/integration/test_workflow.py`.
2. `project.json`: remove `"workflow"` from `pages`; change `start_page`.
3. `app/core/services/__init__.py`: remove the `operation_runner` and
   `workflow_service` imports and their names from `__all__`.
4. `app/context.py`: remove `OperationRunner` and `WorkflowService` from the
   import, the `operations:` and `workflow:` fields, the two lines creating
   them at the end of `create()`, and `self.workflow.shutdown()` in `shutdown()`.
5. `app/ui/registry.py`: remove the `("workflow", …)` line.

### Test Bench

1. Delete `app/ui/pages/test_bench.py`, `app/core/services/test_runner.py`.
2. `project.json`: remove `"test_bench"` from `pages`.
3. `app/core/services/__init__.py`: remove the `test_runner` import and
   `SuiteSummary`, `TestRunner`, `parse_junit` from `__all__`.
4. `app/context.py`: remove `TestRunner` from the import, the `tests:` field
   and `tests=TestRunner(db, connections, parent),`.
5. `app/ui/registry.py`: remove the `("test_bench", …)` line.
6. Tests: delete `test_manager_capture_and_device_test_case` in
   `tests/integration/test_networking.py` and
   `test_runner_executes_pytest_subprocess` in `tests/integration/test_gui.py`.

### Any other page

Pages that only display existing services (`dashboard`, `devices`,
`connections`, `packet_monitor`, `database`, `logs`, `settings`) can be
removed by deleting the page file and its `BUILTIN_PAGES` line. If you forget
the registry line, the app still starts and logs "page … skipped".

The core layers (`app/core/compatibility`, `networking`, `database`,
`protocols`, `config`, `logging_setup`) are shared by everything else and are
not meant to be removed.

## 7. Keep it working while you change it

* `pytest`: tests skip pages your project no longer has, and
  `tests/unit/test_template.py::test_shipped_project_json_is_valid` checks
  that every page and plugin named in `project.json` exists.
* `python main.py --smoke-test` (with `QT_QPA_PLATFORM=offscreen` on a
  server) starts the app, opens every page and exits non-zero if a plugin
  fails to load.
