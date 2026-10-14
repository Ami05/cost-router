#!/usr/bin/env python3
"""TypeScript / JavaScript regex-based parser."""
from __future__ import annotations

import re

from parsers.java_parser import (
    _collect_leading_annotations,
    _extract_calls_regex,
    _leading_annotation_start_line,
    _line_of,
    _skip_balanced_body,
    _split_type_list,
)
from parsers.python_parser import _disambiguate_qualname, _resolve_calls_intra_file


_TS_JS_IMPORT_FROM_RE = re.compile(
    r"""^\s*import\s+(?:type\s+)?[\w*{}\s,]*\s+from\s+['"]([^'"]+)['"]""", re.MULTILINE
)
_TS_JS_REQUIRE_RE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")

_TS_JS_CLASS_DECL_RE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)"
    r"(?:\s+extends\s+([\w.]+(?:<[^{]*?>)?))?"
    r"(?:\s+implements\s+([\w.,\s<>]+?))?"
    r"\s*\{",
    re.MULTILINE,
)

_TS_JS_METHOD_RE = re.compile(
    r"^[ \t]*(?:(?:public|private|protected|static|readonly|async|abstract|override|get|set)\s+)*"
    r"\*?\s*([\w$]+)\s*\(([^;{}]*)\)\s*(?::\s*[^\{;]+?)?\s*\{",
    re.MULTILINE,
)

_TS_JS_FUNC_DECL_RE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s+(\w+)\s*\(([^;{}]*)\)\s*(?::\s*[^\{;]+?)?\s*\{",
    re.MULTILINE,
)

_TS_JS_ARROW_CONST_RE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::\s*[^=]+?)?=\s*"
    r"(?:async\s+)?(?:\(([^)]*)\)|(\w+))\s*(?::\s*[^=>{]+?)?=>\s*\{",
    re.MULTILINE,
)

_TS_JS_FUNC_EXPR_CONST_RE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?function\*?\s*\(([^;{}]*)\)\s*\{",
    re.MULTILINE,
)

_TS_JS_CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
_TS_JS_NEW_CALL_RE = re.compile(r"\bnew\s+([A-Za-z_$][\w$.]*)\s*\(")
_TS_JS_CONTROL_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "throw", "else",
    "do", "try", "finally", "new", "typeof", "instanceof", "function",
}


def _parse_ts_js(content: str) -> tuple[list[str], list[dict]]:
    try:
        imports: list[str] = []
        for match in _TS_JS_IMPORT_FROM_RE.finditer(content):
            name = match.group(1)
            if name not in imports:
                imports.append(name)
        for match in _TS_JS_REQUIRE_RE.finditer(content):
            name = match.group(1)
            if name not in imports:
                imports.append(name)

        symbols: list[dict] = []
        seen_qualnames: dict[str, int] = {}
        class_decls: list[dict] = []

        for match in _TS_JS_CLASS_DECL_RE.finditer(content):
            open_pos = match.end() - 1
            close_pos = _skip_balanced_body(content, open_pos)
            decl_line = _line_of(content, match.start())
            line_start = _leading_annotation_start_line(content, decl_line)
            decorators = _collect_leading_annotations(content, match.start())
            base_classes = []
            if match.group(2):
                base_classes.append(match.group(2).strip())
            base_classes.extend(_split_type_list(match.group(3)))
            name = match.group(1)
            qualname = _disambiguate_qualname(name, seen_qualnames)
            class_decls.append({"name": name, "open_pos": open_pos, "close_pos": close_pos})
            symbols.append(
                {
                    "kind": "class",
                    "name": name,
                    "qualname": qualname,
                    "line_start": line_start,
                    "line_end": _line_of(content, close_pos),
                    "docstring": None,
                    "signature": name,
                    "decorators": decorators,
                    "base_classes": base_classes,
                }
            )

        def _enclosing_class(pos: int) -> dict | None:
            best = None
            for decl in class_decls:
                if decl["open_pos"] < pos < decl["close_pos"]:
                    if best is None or decl["open_pos"] > best["open_pos"]:
                        best = decl
            return best

        for match in _TS_JS_METHOD_RE.finditer(content):
            enclosing = _enclosing_class(match.start())
            if enclosing is None:
                continue
            method_name = match.group(1)
            if method_name in _TS_JS_CONTROL_KEYWORDS:
                continue
            method_open = match.end() - 1
            method_close = _skip_balanced_body(content, method_open)
            params = match.group(2).strip()
            decl_line = _line_of(content, match.start())
            line_start = _leading_annotation_start_line(content, decl_line)
            decorators = _collect_leading_annotations(content, match.start())
            base_qualname = f"{enclosing['name']}.{method_name}"
            qualname = _disambiguate_qualname(base_qualname, seen_qualnames)
            calls_out_raw = _extract_calls_regex(
                content, method_open + 1, method_close,
                _TS_JS_CALL_RE, _TS_JS_NEW_CALL_RE, _TS_JS_CONTROL_KEYWORDS,
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
                    "decorators": decorators,
                    "calls_out_raw": calls_out_raw,
                }
            )

        for match in _TS_JS_FUNC_DECL_RE.finditer(content):
            if _enclosing_class(match.start()) is not None:
                continue
            func_open = match.end() - 1
            func_close = _skip_balanced_body(content, func_open)
            name = match.group(1)
            params = match.group(2).strip()
            decl_line = _line_of(content, match.start())
            line_start = _leading_annotation_start_line(content, decl_line)
            qualname = _disambiguate_qualname(name, seen_qualnames)
            calls_out_raw = _extract_calls_regex(
                content, func_open + 1, func_close,
                _TS_JS_CALL_RE, _TS_JS_NEW_CALL_RE, _TS_JS_CONTROL_KEYWORDS,
            )
            symbols.append(
                {
                    "kind": "function",
                    "name": name,
                    "qualname": qualname,
                    "line_start": line_start,
                    "line_end": _line_of(content, func_close),
                    "docstring": None,
                    "signature": f"{name}({params})",
                    "decorators": [],
                    "calls_out_raw": calls_out_raw,
                }
            )

        for pattern in (_TS_JS_ARROW_CONST_RE, _TS_JS_FUNC_EXPR_CONST_RE):
            for match in pattern.finditer(content):
                if _enclosing_class(match.start()) is not None:
                    continue
                open_pos = match.end() - 1
                close_pos = _skip_balanced_body(content, open_pos)
                name = match.group(1)
                if pattern is _TS_JS_ARROW_CONST_RE:
                    params = match.group(2) if match.group(2) is not None else (match.group(3) or "")
                else:
                    params = match.group(2) or ""
                decl_line = _line_of(content, match.start())
                line_start = _leading_annotation_start_line(content, decl_line)
                qualname = _disambiguate_qualname(name, seen_qualnames)
                calls_out_raw = _extract_calls_regex(
                    content, open_pos + 1, close_pos,
                    _TS_JS_CALL_RE, _TS_JS_NEW_CALL_RE, _TS_JS_CONTROL_KEYWORDS,
                )
                symbols.append(
                    {
                        "kind": "function",
                        "name": name,
                        "qualname": qualname,
                        "line_start": line_start,
                        "line_end": _line_of(content, close_pos),
                        "docstring": None,
                        "signature": f"{name}({params.strip()})",
                        "decorators": [],
                        "calls_out_raw": calls_out_raw,
                    }
                )

        _resolve_calls_intra_file(symbols)
        return imports, symbols
    except Exception:
        return [], []
