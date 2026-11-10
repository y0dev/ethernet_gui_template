from __future__ import annotations

from pathlib import Path

import pytest

from app.core.models import NoteCategory, PortingStatus, ReferenceKind
from app.core.porting import scan_source
from app.core.services import PortingRuleError, PortingService

SAMPLE = """\
#include "proto.h"
#include <stdint.h>
/* #include "commented_out.h" */
#define MAX_LEN 64
#define SQ(x) ((x) * (x))
typedef struct { uint8_t a; } pkt_t;
typedef void (*cb_t)(int);
struct node { int v; struct node *next; };
enum mode { MODE_A, MODE_B };
static uint32_t counter = 0;
const char *names[3];
static const char *greeting = "brace { in string";
int helper(int a);
static int helper(int a) { return a + 1; }
uint16_t crc16(const uint8_t *buf, size_t len)
{
    uint16_t c = 0xFFFF; // { in comment
    for (size_t i = 0; i < len; i++) { c = helper(c) ^ buf[i]; }
    if (len) { memset(0, 0, 0); }
    return c;
}
"""


def test_scanner_inventory():
    inv = scan_source(SAMPLE, "src/crc.c")
    assert inv.includes_local == ["proto.h"]
    assert inv.includes_system == ["stdint.h"]
    kinds = {(s.kind, s.name) for s in inv.symbols}
    assert {
        ("macro", "MAX_LEN"), ("function_macro", "SQ"), ("typedef", "pkt_t"),
        ("typedef", "cb_t"), ("struct", "node"), ("enum", "mode"), ("global", "counter"),
        ("global", "names"), ("global", "greeting"), ("prototype", "helper"),
        ("function", "helper"), ("function", "crc16"),
    } <= kinds
    funcs = {s.name: s for s in inv.by_kind("function")}
    assert funcs["helper"].is_static and not funcs["crc16"].is_static
    assert funcs["crc16"].line == 15
    assert inv.calls["crc16"] == {"helper", "memset"}
    assert inv.warnings == []


def test_scanner_reports_unbalanced_braces():
    assert scan_source("void f(void) { if (1) {", "x.c").warnings


@pytest.fixture
def porting(db):
    return PortingService(db)


@pytest.fixture
def c_tree(tmp_path: Path) -> Path:
    root = tmp_path / "csrc"
    (root / "drivers").mkdir(parents=True)
    (root / "drivers" / "crc.c").write_text(SAMPLE)
    (root / "drivers" / "crc.h").write_text("uint16_t crc16(const uint8_t *, size_t);\n")
    (root / "main.c").write_text('#include "crc.h"\nint main(void) { return crc16(0, 0); }\n')
    return root


def test_import_sources(porting, c_tree):
    project = porting.create_project("fw", str(c_tree))
    summary = porting.import_sources(project, c_tree)
    assert summary.added == ["drivers/crc.c", "main.c"]
    assert summary.headers_found == 1
    modules = {m["c_source_path"]: m for m in porting.list_modules(project)}
    assert all(m["status"] is PortingStatus.NOT_ANALYZED for m in modules.values())
    crc = porting.module_detail(modules["drivers/crc.c"]["id"])
    assert crc["header_paths"] == ["drivers/crc.h"]
    deps = {(d["kind"], d["depends_on"]) for d in crc["dependencies"]}
    assert ("include", "proto.h") in deps and ("external_call", "memset") in deps
    symbols = {(m["c_kind"], m["c_symbol"]) for m in crc["mappings"]}
    assert ("function", "crc16") in symbols and ("prototype", "helper") not in symbols
    again = porting.import_sources(project, c_tree)
    assert again.added == [] and len(again.already_tracked) == 2


def test_verified_requires_real_comparison(porting, c_tree):
    project = porting.create_project("fw")
    module = porting.add_module(project, "drivers/crc.c")
    with pytest.raises(PortingRuleError, match="no verification"):
        porting.set_status(module, PortingStatus.VERIFIED)
    with pytest.raises(PortingRuleError, match="Python replacement"):
        porting.set_status(module, PortingStatus.PORTED)
    porting.update_module(module, python_module_path="app/core/compatibility/crc.py")
    porting.set_status(module, PortingStatus.PORTED)

    porting.record_verification(module, ReferenceKind.NONE, ["crc16"], 10, 0, None)
    with pytest.raises(PortingRuleError, match="did not compare"):
        porting.set_status(module, PortingStatus.VERIFIED)

    porting.record_verification(module, ReferenceKind.C_HARNESS, ["crc16"], 9, 1, True)
    with pytest.raises(PortingRuleError, match="failing"):
        porting.set_status(module, PortingStatus.VERIFIED)

    porting.record_verification(module, ReferenceKind.C_HARNESS, ["crc16"], 10, 0, False)
    with pytest.raises(PortingRuleError, match="binary"):
        porting.set_status(module, PortingStatus.VERIFIED)

    porting.record_verification(module, ReferenceKind.GOLDEN_VECTORS, ["crc16"], 10, 0, True)
    porting.set_status(module, PortingStatus.VERIFIED)
    assert porting.get_module(module)["status"] is PortingStatus.VERIFIED

    porting.record_verification(module, ReferenceKind.C_HARNESS, ["crc16"], 9, 1, True)
    assert porting.get_module(module)["status"] is PortingStatus.VERIFICATION_REQUIRED


def test_notes_mappings_and_report(porting):
    project = porting.create_project("fw", "/c", "/py")
    module = porting.add_module(project, "net/frame.c", python_module_path="core/frame.py")
    porting.set_mapping(module, "frame_encode", "encode_frame")
    porting.set_mapping(module, "frame_decode", "")
    porting.add_note(module, NoteCategory.BEHAVIORAL_DIFFERENCE, "raises instead of returning -1")
    with pytest.raises(ValueError):
        porting.add_note(module, NoteCategory.NOTE, "  ")
    with pytest.raises(ValueError):
        porting.update_module(module, test_coverage=150)
    porting.record_verification(module, ReferenceKind.GOLDEN_VECTORS, ["frame_encode"], 4, 0, True,
                                remaining_risks="decode not yet compared")
    report = porting.report(project)
    assert "| `net/frame.c` | `core/frame.py` | Not Analyzed | 1/2 |" in report
    assert "raises instead of returning -1" in report
    assert "decode not yet compared" in report
