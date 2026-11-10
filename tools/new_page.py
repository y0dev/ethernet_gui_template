"""Create a new page as a plugin from a template and enable it in project.json.

    python tools/new_page.py calibration "Calibration"
    python tools/new_page.py calibration "Calibration" --template query_form
    python tools/new_page.py calibration "Calibration" --template my_templates/report.py.tmpl
    python tools/new_page.py calibration "Calibration" --no-enable
    python tools/new_page.py calibration "Calibration" --table calibration
    python tools/new_page.py plant "Plant data" --table readings --database data/plant.db
    python tools/new_page.py --list-templates

Writes plugins/<key>.py from a page template and adds it to project.json ->
plugins and pages.  Built-in templates live in tools/page_templates/; put
your own in page_templates/ at the project root (a file with the same name
as a built-in replaces it) or pass a path to any template file.

Templates are plain Python with these placeholders: $key (page key), $title
(navigation title), $cls (class name), $table (--table) and $database
(--database).  Write $$ for a literal dollar sign.  --table on its own picks
the "db_table" template: a page to view and edit that table.  An optional first line "# template: <description>" is shown by
--list-templates and left out of the generated page.

Updating the page later: the tool only creates the starting file and never
overwrites it.  Edit plugins/<key>.py directly and restart the application.
Change the navigation title in register(), the order in project.json ->
pages, and hide the page by removing "plugins.<key>" from project.json ->
plugins.  To start again from a template, move your file aside and run the
tool again; existing project.json entries are not duplicated.  See
TEMPLATE.md, sections "Add a page" and "Update a generated page".
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parents[1]
BUILTIN_TEMPLATES = Path(__file__).resolve().parent / "page_templates"
TEMPLATE_SUFFIX = ".py.tmpl"
DEFAULT_TEMPLATE = "table"
TABLE_TEMPLATE = "db_table"
DESCRIPTION_PREFIX = "# template:"


class PageTemplateError(ValueError):
    """A page template is missing, malformed or produces invalid Python."""


def template_dirs(root: Path) -> list[Path]:
    """List the folders searched for named templates, highest priority first.

    Args:
        root: Project root.

    Returns:
        The project's ``page_templates/`` folder, then the built-in folder.
    """
    return [root / "page_templates", BUILTIN_TEMPLATES]


def available_templates(root: Path) -> dict[str, Path]:
    """Find every named template, letting project templates override built-ins.

    Args:
        root: Project root.

    Returns:
        Template name to file, sorted by name.
    """
    found: dict[str, Path] = {}
    for directory in reversed(template_dirs(root)):
        if directory.is_dir():
            for path in directory.glob(f"*{TEMPLATE_SUFFIX}"):
                found[path.name[: -len(TEMPLATE_SUFFIX)]] = path
    return dict(sorted(found.items()))


def resolve_template(name_or_path: str, root: Path) -> Path:
    """Find a template by name, or accept a path to a template file.

    Args:
        name_or_path: Template name such as ``query_form``, or a file path.
        root: Project root, used for named templates and relative paths.

    Returns:
        The template file.

    Raises:
        PageTemplateError: If no template matches.
    """
    templates = available_templates(root)
    if name_or_path in templates:
        return templates[name_or_path]
    for candidate in (Path(name_or_path), root / name_or_path):
        if candidate.is_file():
            return candidate
    raise PageTemplateError(
        f"unknown template {name_or_path!r}; available: {', '.join(templates) or 'none'} "
        "(or pass a path to a template file)"
    )


def split_description(text: str) -> tuple[str, str]:
    """Separate the optional ``# template:`` description line from a template.

    Args:
        text: Template file contents.

    Returns:
        The description (empty if there is none) and the template body.
    """
    first, newline, rest = text.partition("\n")
    if first.startswith(DESCRIPTION_PREFIX):
        return first[len(DESCRIPTION_PREFIX):].strip(), rest
    return "", text


def describe_template(path: Path) -> str:
    """Read a template's one-line description.

    Args:
        path: Template file.

    Returns:
        The description, or ``""`` if the template has none.
    """
    return split_description(path.read_text(encoding="utf-8"))[0]


def class_name_for(key: str) -> str:
    """Build the page class name from a page key.

    Args:
        key: Page key, e.g. ``"calibration_log"``.

    Returns:
        The class name, e.g. ``"CalibrationLogPage"``.
    """
    return "".join(part.capitalize() for part in key.split("_")) + "Page"


def _literal_text(text: str) -> str:
    """Make text safe to place inside a double-quoted Python string.

    Args:
        text: User-supplied text.

    Returns:
        The text with backslashes turned into ``/`` and ``"`` into ``'``.
    """
    return text.replace("\\", "/").replace('"', "'")


def render_page(template_path: Path, key: str, title: str, table: str = "", database: str = "") -> str:
    """Fill a template's placeholders and check that the result is valid Python.

    Args:
        template_path: Template file.
        key: Page key.
        title: Navigation title.
        table: Value for ``$table``.
        database: Value for ``$database``, a database file path.

    Returns:
        The page module source.

    Raises:
        PageTemplateError: If the template uses an unknown placeholder or the
            generated page is not valid Python.
    """
    _description, body = split_description(template_path.read_text(encoding="utf-8"))
    values = {"key": key, "title": _literal_text(title), "cls": class_name_for(key),
              "table": _literal_text(table), "database": _literal_text(database)}
    try:
        source = Template(body).substitute(values)
    except (KeyError, ValueError) as exc:
        raise PageTemplateError(
            f"{template_path.name}: bad placeholder {exc} (use $key, $title, $cls, $table, $database; $$ for '$')"
        ) from exc
    try:
        ast.parse(source, filename=str(template_path))
    except SyntaxError as exc:
        raise PageTemplateError(f"{template_path.name}: generated page is not valid Python: {exc}") from exc
    return source


def enable_in_project(project_file: Path, key: str) -> str:
    """Add the page's plugin and page key to ``project.json``.

    The page is inserted before ``settings`` when the profile lists pages.
    Entries that already exist are not duplicated.

    Args:
        project_file: Path of ``project.json``; created if missing.
        key: Page key.

    Returns:
        The plugin module name, e.g. ``"plugins.calibration"``.
    """
    data = json.loads(project_file.read_text(encoding="utf-8")) if project_file.exists() else {}
    module = f"plugins.{key}"
    plugins = data.setdefault("plugins", [])
    if module not in plugins:
        plugins.append(module)
    pages = data.get("pages")
    if isinstance(pages, list) and key not in pages:
        index = pages.index("settings") if "settings" in pages else len(pages)
        pages.insert(index, key)
    project_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return module


def print_templates(root: Path) -> None:
    """Print every available template with its description and location.

    Args:
        root: Project root.
    """
    for name, path in available_templates(root).items():
        origin = "built-in" if path.parent == BUILTIN_TEMPLATES else str(path.parent)
        default = " (default)" if name == DEFAULT_TEMPLATE else ""
        print(f"{name:<16} {describe_template(path) or '-'}{default}  [{origin}]")


def main(argv: list[str] | None = None) -> int:
    """Generate a plugin page from a template and optionally enable it.

    Args:
        argv: Command-line arguments without the program name; defaults to
            ``sys.argv[1:]``.

    Returns:
        The process exit code: 0 on success, 2 for invalid input, an
        existing file or a broken template.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("key", nargs="?", help="page key (identifier, e.g. calibration)")
    parser.add_argument("title", nargs="?", help="navigation title")
    parser.add_argument("--template", "-t", default=None,
                        help=f"template name or path to a {TEMPLATE_SUFFIX} file (default: {DEFAULT_TEMPLATE}, "
                             f"or {TABLE_TEMPLATE} when --table is given)")
    parser.add_argument("--table", default="", help="table for $table, e.g. for a page that edits it")
    parser.add_argument("--database", default="",
                        help="SQLite file for $database, relative to the project root (default: the app database)")
    parser.add_argument("--list-templates", action="store_true", help="show the available templates and exit")
    parser.add_argument("--no-enable", action="store_true", help="do not edit project.json")
    parser.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.list_templates:
        print_templates(root)
        return 0
    if not args.key or not args.title:
        parser.print_usage()
        print("error: key and title are required (or use --list-templates)")
        return 2
    if not args.key.isidentifier():
        print(f"error: key {args.key!r} must be an identifier (letters, digits, underscore)")
        return 2
    target = root / "plugins" / f"{args.key}.py"
    if target.exists():
        print(f"error: {target} already exists")
        return 2
    template_name = args.template or (TABLE_TEMPLATE if args.table else DEFAULT_TEMPLATE)
    try:
        template = resolve_template(template_name, root)
        source = render_page(template, args.key, args.title, args.table, args.database)
    except (PageTemplateError, OSError) as exc:
        print(f"error: {exc}")
        return 2
    target.parent.mkdir(exist_ok=True)
    (target.parent / "__init__.py").touch()
    target.write_text(source, encoding="utf-8")
    print(f"created {target.relative_to(root)} from template '{template_name}'")
    if not args.no_enable:
        module = enable_in_project(root / "project.json", args.key)
        print(f"enabled in project.json: plugins += {module}, pages += {args.key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
