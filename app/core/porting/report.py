"""Porting status / verification report generation (Markdown)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..models import PortingStatus, ReferenceKind


def _format_utc_time(ts: float | None) -> str:
    """Format an epoch timestamp as UTC, e.g. ``2026-01-31 14:05 UTC``.

    Args:
        ts: Seconds since the Unix epoch, or None.

    Returns:
        The formatted time, or ``"-"`` for None.
    """
    if ts is None:
        return "-"
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def render_report(project: dict[str, Any], modules: list[dict[str, Any]]) -> str:
    """Render a porting status and verification report as Markdown.

    Args:
        project: Porting project row with ``name`` and the source/target roots.
        modules: Module rows, each with optional ``verification``,
            ``mappings`` and ``notes`` keys.

    Returns:
        The Markdown report.
    """
    lines = [
        f"# Porting Status Report: {project['name']}",
        "",
        f"Generated {_format_utc_time(dt.datetime.now(dt.timezone.utc).timestamp())}",
        "",
        f"* C source root: `{project.get('c_source_root') or '-'}`",
        f"* Python target root: `{project.get('python_target_root') or '-'}`",
        "",
        "## Summary",
        "",
        "| Status | Modules |",
        "|---|---|",
    ]
    for status in PortingStatus:
        count = sum(1 for m in modules if m["status"] is status)
        lines.append(f"| {status.value} | {count} |")
    lines += [
        "",
        "## Modules",
        "",
        "| C source | Python module | Status | Mapped symbols | Last verification | Passed | Failed | Binary compatible |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m in modules:
        v = m.get("verification")
        mapped = sum(1 for x in m.get("mappings", []) if x["python_symbol"])
        total = len(m.get("mappings", []))
        if v:
            binary = {True: "yes", False: "NO", None: "not checked"}[v["binary_compatible"]]
            vcols = f"{_format_utc_time(v['executed_at'])} ({v['reference_kind'].value}) | {v['passed']} | {v['failed']} | {binary}"
        else:
            vcols = "never | - | - | -"
        lines.append(
            f"| `{m['c_source_path']}` | `{m['python_module_path'] or '-'}` | {m['status'].value} | "
            f"{mapped}/{total} | {vcols} |"
        )
    for m in modules:
        v = m.get("verification")
        lines += ["", f"### {m['name']}", ""]
        if v:
            lines.append(f"* Reference: {v['reference_kind'].value}")
            if v["reference_kind"] is ReferenceKind.NONE:
                lines.append("  * Python-only tests; not evidence of equivalence with the C code.")
            lines.append(f"* Functions tested: {', '.join(v['functions_tested']) or '-'}")
            lines.append(f"* Passed / failed: {v['passed']} / {v['failed']}")
            lines.append(f"* Behavioral differences: {v['behavioral_differences'] or 'none recorded'}")
            lines.append(f"* Remaining risks: {v['remaining_risks'] or 'none recorded'}")
        else:
            lines.append("* No verification run recorded.")
        issues = [n for n in m.get("notes", []) if n["category"] != "Note"]
        if issues:
            lines.append("* Recorded issues:")
            for n in issues:
                lines.append(f"  * [{n['category']}] {n['body']}")
    lines.append("")
    return "\n".join(lines)
