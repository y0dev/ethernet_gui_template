# Using this project as a template

Everything you normally change lives in a few places:

| What | Where |
|---|---|
| Name, pages, page order, start page, plugins, SQL folders, colours | `project.json` |
| Your SQL queries | `sql/*.sql` |
| Your database tables | `app/core/database/schema/NNNN_*.sql` |
| Your pages | `plugins/*.py` |
| Per-user runtime settings (paths, server IP/port, limits) | `data/config.json` (git-ignored, edited in Settings) |

`project.json` is committed with your project; `data/config.json` is per machine.

---

## 1. Rename and re-colour

```json
{
  "app_name": "Acme Gateway Bench",
  "app_subtitle": "production test",
  "organization": "Acme",
  "theme": { "accent": "#ff8a00", "ok": "#22c55e", "background": "#101418" }
}
```

Theme keys: `background, panel, input, border, text, text_dim, accent,
accent_dim, ok, warning, error, rx, tx`. Invalid keys or colours stop start-up
with a clear message. Also rename `name` in `pyproject.toml` if you publish it.

## 2. Hide, reorder or choose the start page

```json
"pages": ["workflow", "devices", "database", "logs", "settings"],
"start_page": "workflow"
```

Built-in page keys:

| Key | Page |
|---|---|
| `workflow` | Session Workflow (server → database → devices → operations → report) |
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
the file and restart. The page receives `ctx`, which holds every service:

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
| Start again from the latest template | Move your file aside (for example to `<key>_old.py`), run `python tools/new_page.py <key> "<Title>"` again, then copy your code across. Re-running does not duplicate the `project.json` entries. |

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
  `.sql` file, click *Reload .sql files*.
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
