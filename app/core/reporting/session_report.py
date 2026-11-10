"""Session report: what happened in one recorded workflow session.

Everything comes from the session's database rows (sessions, session_devices,
operation_results, packets, application_logs), so a report can be produced
again later for any past session.  Pass/fail means only "the response met
the operation's match rule"; the report says so.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import io
import json
from typing import Any

from ..database import Database, OperationResultRepository, SessionDeviceRepository, SessionRepository
from ..hexutil import format_hex, to_ascii

STATUSES = ("passed", "failed", "timeout", "error", "sent")


def _ts(value: float | None, with_date: bool = True) -> str:
    if value is None:
        return "-"
    stamp = dt.datetime.fromtimestamp(value)
    return stamp.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S.%f")[: None if with_date else -3]


def _duration(start: float | None, end: float | None) -> str:
    if start is None:
        return "-"
    seconds = int((end or dt.datetime.now().timestamp()) - start)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}" + ("" if end else " (running)")


def _preview(data: bytes | None, limit: int = 48) -> str:
    if data is None:
        return "-"
    raw = bytes(data)
    text = format_hex(raw[:limit]) + (" …" if len(raw) > limit else "")
    return f"{text}  |{to_ascii(raw[:limit])}|"


def collect(db: Database, session_id: int) -> dict[str, Any]:
    session = SessionRepository(db).get(session_id)
    if session is None:
        raise KeyError(f"session {session_id} not found")
    devices = SessionDeviceRepository(db).for_session(session_id)
    results = OperationResultRepository(db).for_session(session_id)

    traffic = {
        row["direction"]: {"frames": row["frames"], "bytes": row["bytes"] or 0}
        for row in db.run("report.traffic", session_id=session_id)
    }
    per_peer: dict[str, dict[str, int]] = {}
    for row in db.run("report.traffic_per_peer", session_id=session_id):
        entry = per_peer.setdefault(row["peer"], {"rx_frames": 0, "rx_bytes": 0, "tx_frames": 0, "tx_bytes": 0})
        prefix = "rx" if row["direction"] == "RX" else "tx"
        entry[f"{prefix}_frames"] = row["frames"]
        entry[f"{prefix}_bytes"] = row["bytes"] or 0

    # Collapse reconnects: one entry per peer address, first connect / last disconnect.
    device_rows: dict[str, dict[str, Any]] = {}
    for d in devices:
        row_entry = device_rows.setdefault(d["peer"], {
            "peer": d["peer"], "name": d["device_name"], "connected_at": d["connected_at"],
            "disconnected_at": d["disconnected_at"], "connections": 0,
        })
        row_entry["connections"] += 1
        row_entry["name"] = d["device_name"] or row_entry["name"]
        row_entry["disconnected_at"] = d["disconnected_at"]
    for r in results:  # devices that only appear in results (renamed later etc.)
        device_rows.setdefault(r["peer"], {"peer": r["peer"], "name": r["device_name"], "connected_at": None,
                                           "disconnected_at": None, "connections": 0})
    for peer, dev in device_rows.items():
        dev.update(per_peer.get(peer, {"rx_frames": 0, "rx_bytes": 0, "tx_frames": 0, "tx_bytes": 0}))
        own = [r for r in results if r["peer"] == peer]
        dev["results"] = own
        dev["counts"] = {s: sum(1 for r in own if r["status"] == s) for s in STATUSES}
        if not dev["name"]:
            dev["name"] = next((r["device_name"] for r in own if r["device_name"] != peer), "")

    counts = {s: sum(1 for r in results if r["status"] == s) for s in STATUSES}
    judged = counts["passed"] + counts["failed"] + counts["timeout"] + counts["error"]
    end = session["ended_at"]
    warnings = [
        dict(r) for r in db.run(
            "report.warnings", start=session["started_at"], end=end or dt.datetime.now().timestamp()
        )
    ]
    per_operation: dict[str, dict[str, int]] = {}
    for r in results:
        op = per_operation.setdefault(r["operation_name"], {s: 0 for s in STATUSES})
        op[r["status"]] += 1
    return {
        "session": session,
        "database": str(db.path),
        "generated_at": dt.datetime.now().timestamp(),
        "devices": sorted(device_rows.values(), key=lambda d: (d["connected_at"] or 0, d["peer"])),
        "results": results,
        "counts": counts,
        "judged": judged,
        "pass_rate": (counts["passed"] / judged * 100.0) if judged else None,
        "per_operation": per_operation,
        "traffic": traffic,
        "warnings": warnings,
    }


# -- Markdown -----------------------------------------------------------------
def render_markdown(data: dict[str, Any]) -> str:
    s = data["session"]
    cfg = s.get("config", {})
    c = data["counts"]
    rate = "n/a" if data["pass_rate"] is None else f"{data['pass_rate']:.1f}%"
    rx = data["traffic"].get("RX", {"frames": 0, "bytes": 0})
    tx = data["traffic"].get("TX", {"frames": 0, "bytes": 0})
    out = [
        f"# Session Report: {s['name']}",
        "",
        f"Generated {_ts(data['generated_at'])}",
        "",
        "## Session",
        "",
        "| | |",
        "|---|---|",
        f"| Session | #{s['id']} |",
        f"| Started | {_ts(s['started_at'])} |",
        f"| Ended | {_ts(s['ended_at']) if s['ended_at'] else 'still running'} |",
        f"| Duration | {_duration(s['started_at'], s['ended_at'])} |",
        f"| Server | {cfg.get('server_address', '-')} ({cfg.get('transport', '-')}) |",
        f"| Framing | `{json.dumps(cfg.get('framing', {}))}` |",
        f"| Database | `{data['database']}` |",
        f"| Traffic | RX {rx['frames']} frames / {rx['bytes']} bytes, TX {tx['frames']} frames / {tx['bytes']} bytes |",
        "",
        "## Summary",
        "",
        f"* Devices: {len(data['devices'])}",
        f"* Operations run: {len(data['results'])}",
        f"* Passed {c['passed']} · Failed {c['failed']} · Timeout {c['timeout']} · Error {c['error']} · Sent only {c['sent']}",
        f"* Pass rate (of operations that expected a response or failed): {rate}",
        "",
        "> Pass means the device's reply met the operation's match rule. "
        "It is not a protocol-level validation.",
        "",
    ]
    if data["per_operation"]:
        out += ["## By operation", "", "| Operation | Passed | Failed | Timeout | Error | Sent |", "|---|---|---|---|---|---|"]
        for name, oc in data["per_operation"].items():
            out.append(f"| {name} | {oc['passed']} | {oc['failed']} | {oc['timeout']} | {oc['error']} | {oc['sent']} |")
        out.append("")
    out += ["## Devices", ""]
    for d in data["devices"]:
        dc = d["counts"]
        out += [
            f"### {d['name'] or d['peer']}",
            "",
            f"* Address: {d['peer']}",
            f"* Connected: {_ts(d['connected_at'])} → {_ts(d['disconnected_at']) if d['disconnected_at'] else 'end of session'}"
            + (f" ({d['connections']} connections)" if d["connections"] > 1 else ""),
            f"* Traffic: RX {d['rx_frames']} frames / {d['rx_bytes']} B, TX {d['tx_frames']} frames / {d['tx_bytes']} B",
            f"* Results: {dc['passed']} passed, {dc['failed']} failed, {dc['timeout']} timeout, {dc['error']} error, {dc['sent']} sent",
            "",
        ]
        if d["results"]:
            out += ["| Time | Operation | Status | Latency | Request | Response | Note |", "|---|---|---|---|---|---|---|"]
            for r in d["results"]:
                latency = "-" if r["latency_ms"] is None else f"{r['latency_ms']:.1f} ms"
                out.append(
                    f"| {_ts(r['executed_at'], False)} | {r['operation_name']} | {r['status'].upper()} | {latency} | "
                    f"`{_preview(r['request'], 24)}` | `{_preview(r['response'], 24)}` | {r['message']} |"
                )
            out.append("")
    if data["warnings"]:
        out += ["## Warnings and errors during the session", ""]
        for w in data["warnings"]:
            out.append(f"* {_ts(w['timestamp'], False)} {w['level']} {w['message']}")
        out.append("")
    return "\n".join(out)


# -- HTML ---------------------------------------------------------------------
PALETTE = {"bg": "#14181e", "panel": "#1a2029", "border": "#2e3846", "text": "#d6dde6", "dim": "#8b97a7",
           "ok": "#2fbf71", "bad": "#e5484d", "warn": "#e0a526", "accent": "#3d8bfd"}

# Concrete colours (no CSS variables) so the page also renders in Qt's
# QTextBrowser preview; the print block switches to black on white.
_CSS = """
* {{ box-sizing: border-box; }}
body {{ margin:0; padding:28px; background:{bg}; color:{text};
       font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-size:14px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:28px 0 10px; }}
h3 {{ font-size:14px; margin:22px 0 6px; }}
.dim {{ color:{dim}; }} code, .mono {{ font-family: Consolas, "DejaVu Sans Mono", monospace; font-size:12px; }}
table.cards {{ width:auto; margin:16px 0; border:none; background:none; border-collapse:separate; border-spacing:8px 0; }}
td.card {{ background:{panel}; border:1px solid {border}; padding:10px 16px; min-width:96px; }}
.card b {{ display:block; font-size:20px; }} .card span {{ color:{dim}; font-size:11px; }}
table {{ border-collapse:collapse; width:100%; background:{panel}; border:1px solid {border}; }}
th, td {{ text-align:left; padding:5px 8px; border-bottom:1px solid {border}; vertical-align:top; }}
th {{ color:{dim}; font-weight:600; font-size:12px; }}
.passed {{ color:{ok}; font-weight:600; }} .failed, .error {{ color:{bad}; font-weight:600; }}
.timeout {{ color:{warn}; font-weight:600; }} .sent {{ color:{dim}; font-weight:600; }}
.note {{ border-left:3px solid {accent}; padding:6px 10px; background:{panel}; color:{dim}; }}
@media print {{
  body {{ background:#fff; color:#111; }} table, .card, .note {{ background:#fff; border-color:#bbb; }}
  .dim, th, .card span {{ color:#555; }}
}}
""".format(**PALETTE)


def render_html(data: dict[str, Any]) -> str:
    e = html.escape
    s = data["session"]
    cfg = s.get("config", {})
    c = data["counts"]
    rate = "n/a" if data["pass_rate"] is None else f"{data['pass_rate']:.1f}%"
    rx = data["traffic"].get("RX", {"frames": 0, "bytes": 0})
    tx = data["traffic"].get("TX", {"frames": 0, "bytes": 0})

    def card(value: Any, label: str, cls: str = "") -> str:
        return f'<td class="card"><b class="{cls}">{e(str(value))}</b><br><span>{e(label)}</span></td>'

    parts = [
        f"<!doctype html><html><head><meta charset='utf-8'><title>Session Report: {e(s['name'])}</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>Session Report: {e(s['name'])}</h1>",
        f"<div class='dim'>Session #{s['id']} · {_ts(s['started_at'])} → "
        f"{_ts(s['ended_at']) if s['ended_at'] else 'still running'} · duration {_duration(s['started_at'], s['ended_at'])}"
        f" · generated {_ts(data['generated_at'])}</div>",
        "<table class='cards'><tr>",
        card(len(data["devices"]), "Devices"), card(len(data["results"]), "Operations"),
        card(c["passed"], "Passed", "passed"), card(c["failed"], "Failed", "failed" if c["failed"] else ""),
        card(c["timeout"], "Timeout", "timeout" if c["timeout"] else ""),
        card(c["error"], "Error", "error" if c["error"] else ""), card(rate, "Pass rate"),
        "</tr></table>",
        "<div class='note'>Pass means the device's reply met the operation's match rule "
        "(exact / starts with / contains / any). It is not a protocol-level validation.</div>",
        "<h2>Setup</h2><table>",
        f"<tr><th>Server</th><td class='mono'>{e(cfg.get('server_address', '-'))} ({e(cfg.get('transport', '-'))})</td></tr>",
        f"<tr><th>Framing</th><td class='mono'>{e(json.dumps(cfg.get('framing', {})))}</td></tr>",
        f"<tr><th>Database</th><td class='mono'>{e(data['database'])}</td></tr>",
        f"<tr><th>Traffic</th><td>RX {rx['frames']} frames / {rx['bytes']} bytes · "
        f"TX {tx['frames']} frames / {tx['bytes']} bytes</td></tr></table>",
    ]
    if data["per_operation"]:
        parts.append("<h2>By operation</h2><table><tr><th>Operation</th><th>Passed</th><th>Failed</th>"
                     "<th>Timeout</th><th>Error</th><th>Sent</th></tr>")
        for name, oc in data["per_operation"].items():
            parts.append(f"<tr><td>{e(name)}</td>" + "".join(
                f"<td class='{st if oc[st] else ''}'>{oc[st]}</td>" for st in ("passed", "failed", "timeout", "error", "sent")
            ) + "</tr>")
        parts.append("</table>")
    parts.append("<h2>Devices</h2>")
    if not data["devices"]:
        parts.append("<p class='dim'>No devices connected during this session.</p>")
    for d in data["devices"]:
        dc = d["counts"]
        until = _ts(d["disconnected_at"]) if d["disconnected_at"] else "end of session"
        parts += [
            f"<h3>{e(d['name'] or d['peer'])}"
            + (f" <span class='dim mono'>{e(d['peer'])}</span>" if d["name"] else "") + "</h3>",
            f"<div class='dim'>Connected {_ts(d['connected_at'])} → {e(until)}"
            + (f" · {d['connections']} connections" if d["connections"] > 1 else "")
            + f" · RX {d['rx_frames']} frames / {d['rx_bytes']} B · TX {d['tx_frames']} frames / {d['tx_bytes']} B"
            + f" · {dc['passed']} passed, {dc['failed']} failed, {dc['timeout']} timeout, {dc['error']} error</div>",
        ]
        if d["results"]:
            parts.append("<table><tr><th>Time</th><th>Operation</th><th>Status</th><th>Latency</th>"
                         "<th>Request</th><th>Response</th><th>Note</th></tr>")
            for r in d["results"]:
                latency = "-" if r["latency_ms"] is None else f"{r['latency_ms']:.1f} ms"
                parts.append(
                    f"<tr><td class='mono'>{_ts(r['executed_at'], False)}</td><td>{e(r['operation_name'])}</td>"
                    f"<td class='{r['status']}'>{r['status'].upper()}</td><td>{latency}</td>"
                    f"<td class='mono'>{e(_preview(r['request'], 24))}</td>"
                    f"<td class='mono'>{e(_preview(r['response'], 24))}</td><td>{e(r['message'])}</td></tr>"
                )
            parts.append("</table>")
    if data["warnings"]:
        parts.append("<h2>Warnings and errors during the session</h2><table>")
        for w in data["warnings"]:
            parts.append(f"<tr><td class='mono'>{_ts(w['timestamp'], False)}</td>"
                         f"<td class='{'error' if w['level'] != 'WARNING' else 'timeout'}'>{e(w['level'])}</td>"
                         f"<td>{e(w['message'])}</td></tr>")
        parts.append("</table>")
    parts.append("</body></html>")
    return "\n".join(parts)


def render_csv(data: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["executed_at", "device", "peer", "operation", "match_mode", "status", "latency_ms",
                     "request_hex", "expected_hex", "response_hex", "message"])
    for r in data["results"]:
        writer.writerow([
            _ts(r["executed_at"]), r["device_name"], r["peer"], r["operation_name"], r["match_mode"], r["status"],
            "" if r["latency_ms"] is None else f"{r['latency_ms']:.2f}",
            format_hex(bytes(r["request"]), ""), format_hex(bytes(r["expected"]), "") if r["expected"] else "",
            format_hex(bytes(r["response"]), "") if r["response"] is not None else "", r["message"],
        ])
    return buffer.getvalue()
