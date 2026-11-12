# Guides: making the workbench yours

Step-by-step guides for the changes most projects make. Each one says which
files to change, shows working code, and ends with what to check.

| I want to… | Guide |
|---|---|
| Rename the app, change its icon and colours, choose which pages show | [Branding and layout](branding-and-layout.md) |
| Add a table and a page to view and edit it, or edit my own SQLite file | [Set up a database page](database-page.md) |
| Build a page of my own (layout, services, widgets, renaming, removing) | [Build your own page](custom-page.md) |
| Run Python functions on my devices from a numbered menu | [Menu hooks](menu-hooks.md) |
| Choose another layout for that menu (console, buttons, form, sequence) | [Pick a layout](menu-templates.md) |
| Put my device's commands in the Session Workflow menu | [Set up operations for your device](device-operations.md) |
| See my frames as named fields in the Packet Monitor | [Decode your device's frames](protocol-decoder.md) |
| Package the app as an `.exe` that runs without Python | [Build a standalone executable](build-executable.md) |
| Turn a C console menu into a workflow | [examples/led_device](../../examples/led_device/README.md) |
| Port C code to Python and prove it behaves the same | [Porting guide](../PORTING_GUIDE.md) |
| Look up every option in one place, or delete a feature's code | [TEMPLATE.md](../../TEMPLATE.md) |
| Understand how the code is organised | [Architecture](../ARCHITECTURE.md) |

## Where things live

| You change | File | Committed |
|---|---|---|
| Name, icon, pages, plugins, colours | `project.json` | yes |
| Your pages and start-up code | `plugins/*.py` | yes |
| Your SQL queries | `sql/*.sql` | yes |
| Your tables | `app/core/database/schema/NNNN_*.sql` | yes |
| Your command sets | `resources/command_sets/*.json` | yes |
| Defaults for new machines | `resources/default_config.json` | yes |
| This machine's settings | `data/config.json` | no |
| Databases, logs, reports | `data/` | no |
| Experiments you don't want to commit | `plugins/local_*.py`, `*.local.json`, `*_old.py`, `scratch/` | no |

`.gitignore` is an allowlist: git ignores every file except the folders and
file types listed in it. The places above are already allowed. If you add a
new folder or kind of file (for example `.csv` test data), add a line such as
`!/tests/**/*.csv` to `.gitignore`, or git will not see it. Use
`git check-ignore -v <file>` to see which rule applies to a file.

## Tools

```bash
python tools/new_page.py <key> "<Title>" [--template NAME] [--table T] [--database F]
python tools/new_page.py --list-templates
python tools/new_migration.py "<what it does>"
python tools/device_simulator.py --port 5000 --count 3      # demo devices
```

## After any change

```bash
python main.py --smoke-test     # the profile loads and every page opens
pytest                          # includes a check that project.json names real pages and plugins
```
