"""Lightweight C source inventory used to seed the Porting Workspace.

This is an inventory aid, NOT a C parser.  It strips comments and string
literals, tracks brace depth and uses patterns to list top-level symbols.  It
does not run the preprocessor, so symbols produced by macros, symbols inside
``#if`` branches that are not built, and unusual declaration styles may be
missed or misreported.  Its output marks a module "Analyzed" only after an
engineer has reviewed it; the scanner never changes a module's status.

For precise analysis, use the compiler itself (``gcc -E``, ``-fdump-*``,
``ctags``, or libclang) on the real build configuration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

C_EXTENSIONS = (".c",)
HEADER_EXTENSIONS = (".h",)

_INCLUDE = re.compile(r'^\s*#\s*include\s*([<"])([^>"]+)[>"]', re.MULTILINE)
_DEFINE = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)(\()?", re.MULTILINE)
_KEYWORDS = {
    "if", "for", "while", "switch", "return", "sizeof", "else", "do", "case",
    "goto", "typedef", "struct", "union", "enum",
}
_FUNC_HEAD = re.compile(
    r"(?P<prefix>(?:[A-Za-z_][\w\s\*]*?))\b(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^;{}()]*(?:\([^()]*\)[^;{}()]*)*)\)\s*$",
    re.DOTALL,
)
_TAGGED = re.compile(r"\b(struct|union|enum)\s+([A-Za-z_]\w*)\s*\{")
_TYPEDEF_NAME = re.compile(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*;\s*$")
_FUNC_PTR_TYPEDEF = re.compile(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)")


@dataclass
class CSymbol:
    name: str
    kind: str          # function, prototype, macro, function_macro, struct,
                       # union, enum, typedef, global
    line: int
    is_static: bool = False
    signature: str = ""


@dataclass
class CFileInventory:
    path: str
    includes_local: list[str] = field(default_factory=list)
    includes_system: list[str] = field(default_factory=list)
    symbols: list[CSymbol] = field(default_factory=list)
    calls: dict[str, set[str]] = field(default_factory=dict)  # function -> called names
    warnings: list[str] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[CSymbol]:
        return [s for s in self.symbols if s.kind == kind]


def strip_comments_and_strings(source: str) -> str:
    """Blank out comments and string/char literals, keeping line numbers."""
    out = []
    i = 0
    n = len(source)
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "*":
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append(re.sub(r"[^\n]", " ", source[i:end]))
            i = end
        elif c == "/" and nxt == "/":
            end = source.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
        elif c in "\"'":
            j = i + 1
            while j < n and source[j] != c and source[j] != "\n":
                if source[j] == "\\" and j + 1 < n and source[j + 1] != "\n":
                    j += 1
                j += 1
            if j < n and source[j] == c:
                out.append(c + " " * (j - i - 1) + c)
                i = j + 1
            else:  # unterminated literal: blank to end of line, keep the newline
                out.append(" " * (j - i))
                i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _blank_preprocessor(text: str) -> str:
    """Remove preprocessor lines (including continuations) after recording them."""
    lines = text.split("\n")
    result = []
    continuing = False
    for line in lines:
        if continuing or line.lstrip().startswith("#"):
            continuing = line.rstrip().endswith("\\")
            result.append("")
        else:
            result.append(line)
    return "\n".join(result)


def scan_source(text: str, path: str = "") -> CFileInventory:
    inventory = CFileInventory(path=path)
    clean = strip_comments_and_strings(text)

    # Include names live inside quotes (blanked in ``clean``), so match the
    # original text and keep only lines that are still directives in ``clean``
    # (i.e. not inside a comment).  Same length, so offsets line up.
    for match in _INCLUDE.finditer(text):
        hash_pos = text.index("#", match.start())
        if clean[hash_pos] != "#":
            continue
        name = match.group(2)
        target = inventory.includes_local if match.group(1) == '"' else inventory.includes_system
        if name not in target:
            target.append(name)

    for match in _DEFINE.finditer(clean):
        kind = "function_macro" if match.group(2) else "macro"
        inventory.symbols.append(
            CSymbol(match.group(1), kind, clean.count("\n", 0, match.start()) + 1)
        )

    code = _blank_preprocessor(clean)
    _scan_top_level(code, inventory)
    return inventory


def _scan_top_level(code: str, inventory: CFileInventory) -> None:
    depth = 0
    statement_start = 0
    current_function: str | None = None
    function_body_start = 0
    i = 0
    n = len(code)
    while i < n:
        c = code[i]
        if c == "{":
            if depth == 0:
                head = code[statement_start:i]
                current_function = _classify_block_head(head, statement_start, code, inventory)
                function_body_start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                inventory.warnings.append("unbalanced braces; inventory may be incomplete")
                depth = 0
            if depth == 0:
                if current_function:
                    inventory.calls[current_function] = _find_calls(code[function_body_start:i])
                    current_function = None
                    statement_start = i + 1
                # a struct/enum block continues until ';'
        elif c == ";" and depth == 0:
            _classify_declaration(code[statement_start:i + 1], statement_start, code, inventory)
            statement_start = i + 1
        i += 1
    if depth != 0:
        inventory.warnings.append("unterminated block at end of file")


def _line_of(code: str, offset: int, text: str) -> int:
    stripped = len(text) - len(text.lstrip())
    return code.count("\n", 0, offset + stripped) + 1


def _classify_block_head(head: str, offset: int, code: str, inv: CFileInventory) -> str | None:
    compact = " ".join(head.split())
    if not compact:
        return None
    tagged = _TAGGED.search(compact + "{")
    if tagged and "(" not in compact.split(tagged.group(0)[:-1])[0]:
        inv.symbols.append(CSymbol(tagged.group(2), tagged.group(1), _line_of(code, offset, head)))
        return None
    if compact.startswith(("struct", "union", "enum", "typedef")) or compact.endswith("="):
        return None
    match = _FUNC_HEAD.search(compact)
    if match and match.group("name") not in _KEYWORDS and match.group("prefix").strip():
        name = match.group("name")
        prefix_words = match.group("prefix").split()
        inv.symbols.append(
            CSymbol(
                name, "function", _line_of(code, offset, head),
                is_static="static" in prefix_words, signature=compact,
            )
        )
        return name
    return None


def _classify_declaration(stmt: str, offset: int, code: str, inv: CFileInventory) -> None:
    compact = " ".join(stmt.split())
    if not compact or compact == ";":
        return
    line = _line_of(code, offset, stmt)
    if compact.startswith("typedef"):
        fp = _FUNC_PTR_TYPEDEF.search(compact)
        name_match = fp or _TYPEDEF_NAME.search(compact)
        if name_match:
            inv.symbols.append(CSymbol(name_match.group(1), "typedef", line, signature=compact))
        return
    if compact.startswith("}"):
        return  # tail of a struct/enum definition: '} name;'
    if compact.startswith("extern \"C\""):
        return
    words = compact.split()
    is_static = "static" in words
    if "(" in compact and "=" not in compact.split("(")[0]:
        match = _FUNC_HEAD.search(compact.rstrip(";").strip())
        if match and match.group("name") not in _KEYWORDS and match.group("prefix").strip():
            inv.symbols.append(
                CSymbol(match.group("name"), "prototype", line, is_static, compact)
            )
            return
    name_part = compact.split("=")[0].rstrip(";").strip()
    name_match = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*$", name_part)
    if name_match and len(name_part.split()) >= 2 and name_match.group(1) not in _KEYWORDS:
        inv.symbols.append(
            CSymbol(name_match.group(1), "global", line, is_static, compact)
        )


def _find_calls(body: str) -> set[str]:
    calls = set()
    for match in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", body):
        if match.group(1) not in _KEYWORDS:
            calls.add(match.group(1))
    return calls


def scan_file(path: Path, root: Path | None = None) -> CFileInventory:
    text = path.read_text(encoding="utf-8", errors="replace")
    rel = str(path.relative_to(root)) if root else str(path)
    return scan_source(text, rel.replace("\\", "/"))


def discover_sources(root: Path) -> tuple[list[Path], list[Path]]:
    """All ``.c`` and ``.h`` files under ``root`` (sorted, deterministic)."""
    sources = sorted(p for p in root.rglob("*") if p.suffix in C_EXTENSIONS and p.is_file())
    headers = sorted(p for p in root.rglob("*") if p.suffix in HEADER_EXTENSIONS and p.is_file())
    return sources, headers
