#!/usr/bin/env python3
"""Java regex-based parser and shared structural helpers."""
from __future__ import annotations

import re

from parsers.python_parser import _disambiguate_qualname, _resolve_calls_intra_file


# ── shared structural helpers ─────────────────────────────────────────────────

def _skip_balanced_body(content: str, open_pos: int) -> int:
    """Return the index of the '}' matching the '{' at open_pos."""
    depth = 0
    i = open_pos
    n = len(content)
    in_string: str | None = None
    in_line_comment = False
    in_block_comment = False
    while i < n:
        ch = content[i]
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and content[i + 1: i + 2] == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue
        if ch == "/" and content[i + 1: i + 2] == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and content[i + 1: i + 2] == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in ('"', "'", "`"):
            in_string = ch
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _line_of(content: str, pos: int) -> int:
    return content.count("\n", 0, pos) + 1


def _leading_annotation_start_line(content: str, decl_line: int) -> int:
    """Scan backwards from decl_line while prior non-blank lines are single-line annotations."""
    lines = content.splitlines()
    idx = decl_line - 2
    earliest = decl_line
    ann_re = re.compile(r"^\s*@\w+(\([^)]*\))?\s*$")
    while idx >= 0:
        line = lines[idx]
        if not line.strip():
            idx -= 1
            continue
        if ann_re.match(line):
            earliest = idx + 1
            idx -= 1
            continue
        break
    return earliest


def _collect_leading_annotations(content: str, decl_start: int) -> list[str]:
    decl_line = _line_of(content, decl_start)
    earliest = _leading_annotation_start_line(content, decl_line)
    if earliest == decl_line:
        return []
    lines = content.splitlines()
    names: list[str] = []
    ann_re = re.compile(r"@(\w+)")
    for line in lines[earliest - 1: decl_line - 1]:
        for match in ann_re.finditer(line):
            names.append(f"@{match.group(1)}")
    return names


def _split_type_list(raw: str | None) -> list[str]:
    """Split a comma-separated type list respecting generic '<...>' nesting."""
    if not raw:
        return []
    raw = raw.strip()
    if not raw:
        return []
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in raw:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            if current.strip():
                parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


# ── Java regex constants ──────────────────────────────────────────────────────

_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;", re.MULTILINE)

_JAVA_TYPE_DECL_RE = re.compile(
    r"^[ \t]*(?:(?:public|private|protected|static|final|abstract|strictfp)\s+)*"
    r"(class|interface|enum|record)\s+(\w+)"
    r"(?:\s*<[^{]*?>)?"
    r"(?:\s*\([^)]*\))?"
    r"(?:\s+extends\s+([\w.<>,\s\[\]]+?))?"
    r"(?:\s+implements\s+([\w.<>,\s\[\]]+?))?"
    r"\s*\{",
    re.MULTILINE,
)

_JAVA_MEMBER_RE = re.compile(
    r"^[ \t]*(?:(?:public|private|protected|static|final|abstract|synchronized|native|default|strictfp)\s+)*"
    r"(?:<[^>]*>\s*)?"
    r"(?:[\w.$\[\]]+(?:<[^{;]*?>)?\s+)?"
    r"(\w+)\s*\(([^;{}]*)\)"
    r"(?:\s*throws\s+[\w.,\s]+)?"
    r"\s*\{",
    re.MULTILINE,
)

_JAVA_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_JAVA_NEW_CALL_RE = re.compile(r"\bnew\s+([A-Za-z_][\w.]*)\s*\(")
_JAVA_CONTROL_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "synchronized", "return",
    "throw", "else", "do", "try", "finally", "assert", "new", "instanceof",
}

_RECEIVER_KEYWORDS = ("this", "self")


def _extract_calls_regex(
    content: str,
    start: int,
    end: int,
    call_re: re.Pattern,
    new_call_re: re.Pattern,
    control_keywords: set[str],
) -> list[str]:
    calls: list[str] = []
    segment = content[start:end]
    for match in new_call_re.finditer(segment):
        name = match.group(1)
        if name not in calls:
            calls.append(name)
    for match in call_re.finditer(segment):
        name = match.group(1)
        if name in control_keywords:
            continue
        preceding = segment[: match.start()]
        call_name = name
        for keyword in _RECEIVER_KEYWORDS:
            if preceding.endswith(f"{keyword}."):
                call_name = f"{keyword}.{name}"
                break
        if call_name not in calls:
            calls.append(call_name)
    return calls


def _parse_java(content: str) -> tuple[list[str], list[dict]]:
    try:
        imports: list[str] = []
        for match in _JAVA_IMPORT_RE.finditer(content):
            name = match.group(1)
            if name not in imports:
                imports.append(name)

        symbols: list[dict] = []
        seen_qualnames: dict[str, int] = {}
        type_decls: list[dict] = []

        for match in _JAVA_TYPE_DECL_RE.finditer(content):
            open_pos = match.end() - 1
            close_pos = _skip_balanced_body(content, open_pos)
            decl_line = _line_of(content, match.start())
            line_start = _leading_annotation_start_line(content, decl_line)
            annotations = _collect_leading_annotations(content, match.start())
            base_classes = _split_type_list(match.group(3)) + _split_type_list(match.group(4))
            name = match.group(2)
            qualname = _disambiguate_qualname(name, seen_qualnames)
            type_decls.append({"name": name, "open_pos": open_pos, "close_pos": close_pos})
            symbols.append(
                {
                    "kind": "class",
                    "name": name,
                    "qualname": qualname,
                    "line_start": line_start,
                    "line_end": _line_of(content, close_pos),
                    "docstring": None,
                    "signature": name,
                    "decorators": annotations,
                    "base_classes": base_classes,
                }
            )

        def _innermost_enclosing(pos: int) -> dict | None:
            best = None
            for decl in type_decls:
                if decl["open_pos"] < pos < decl["close_pos"]:
                    if best is None or decl["open_pos"] > best["open_pos"]:
                        best = decl
            return best

        for match in _JAVA_MEMBER_RE.finditer(content):
            enclosing = _innermost_enclosing(match.start())
            if enclosing is None:
                continue
            method_open = match.end() - 1
            method_close = _skip_balanced_body(content, method_open)
            method_name = match.group(1)
            params = match.group(2).strip()
            decl_line = _line_of(content, match.start())
            line_start = _leading_annotation_start_line(content, decl_line)
            annotations = _collect_leading_annotations(content, match.start())
            base_qualname = f"{enclosing['name']}.{method_name}"
            qualname = _disambiguate_qualname(base_qualname, seen_qualnames)
            calls_out_raw = _extract_calls_regex(
                content, method_open + 1, method_close,
                _JAVA_CALL_RE, _JAVA_NEW_CALL_RE, _JAVA_CONTROL_KEYWORDS,
            )
            symbols.append(
                {
                    "kind": "method",
                    "name": method_name,
                    "qualname": qualname,
                    "line_start": line_start,
                    "line_end": _line_of(content, method_close),
                    "docstring": None,
                    "signature": f"{method_name}({params})",
                    "decorators": annotations,
                    "calls_out_raw": calls_out_raw,
                }
            )

        _resolve_calls_intra_file(symbols)
        return imports, symbols
    except Exception:
        return [], []
