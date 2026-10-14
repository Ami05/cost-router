#!/usr/bin/env python3
"""HTML regex-based parser."""
from __future__ import annotations

import re

from parsers.java_parser import _line_of
from parsers.python_parser import _disambiguate_qualname
from parsers.ts_js_parser import _parse_ts_js


_HTML_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_HTML_STYLE_RE = re.compile(r"<style\b([^>]*)>(.*?)</style\s*>", re.IGNORECASE | re.DOTALL)
_HTML_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_HTML_HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1\s*>", re.IGNORECASE | re.DOTALL)
_HTML_ID_ELEMENT_RE = re.compile(r"<(\w+)\b[^>]*\bid=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_HTML_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_HTML_SRC_ATTR_RE = re.compile(r"""\bsrc=["']([^"']+)["']""", re.IGNORECASE)


def _html_strip_tags(text: str) -> str:
    return _HTML_TAG_STRIP_RE.sub("", text).strip()


def _parse_html(content: str) -> tuple[list[str], list[dict]]:
    """Lightweight regex-based HTML extractor — not a real DOM/AST parser."""
    try:
        imports: list[str] = []
        symbols: list[dict] = []
        seen_qualnames: dict[str, int] = {}

        title_match = _HTML_TITLE_RE.search(content)
        if title_match:
            text = _html_strip_tags(title_match.group(1)) or "title"
            symbols.append(
                {
                    "kind": "title",
                    "name": text,
                    "qualname": _disambiguate_qualname(text, seen_qualnames),
                    "line_start": _line_of(content, title_match.start()),
                    "line_end": _line_of(content, title_match.end()),
                    "docstring": None,
                }
            )

        for match in _HTML_HEADING_RE.finditer(content):
            level = match.group(1)
            text = _html_strip_tags(match.group(2)) or f"h{level}"
            symbols.append(
                {
                    "kind": f"h{level}",
                    "name": text,
                    "qualname": _disambiguate_qualname(text, seen_qualnames),
                    "line_start": _line_of(content, match.start()),
                    "line_end": _line_of(content, match.end()),
                    "docstring": None,
                }
            )

        for match in _HTML_ID_ELEMENT_RE.finditer(content):
            tag, elem_id = match.group(1), match.group(2)
            if tag.lower() in ("script", "style"):
                continue
            base_qualname = f"{tag}#{elem_id}"
            line_start = _line_of(content, match.start())
            symbols.append(
                {
                    "kind": "element",
                    "name": f"#{elem_id}",
                    "qualname": _disambiguate_qualname(base_qualname, seen_qualnames),
                    "line_start": line_start,
                    "line_end": line_start,
                    "docstring": None,
                    "signature": f'<{tag} id="{elem_id}">',
                }
            )

        for match in _HTML_SCRIPT_RE.finditer(content):
            attrs, body = match.group(1), match.group(2)
            line_start = _line_of(content, match.start())
            line_end = _line_of(content, match.end())
            src_match = _HTML_SRC_ATTR_RE.search(attrs)
            if src_match:
                src = src_match.group(1)
                if src not in imports:
                    imports.append(src)
                symbols.append(
                    {
                        "kind": "script",
                        "name": src,
                        "qualname": _disambiguate_qualname(f"script:{src}", seen_qualnames),
                        "line_start": line_start,
                        "line_end": line_end,
                        "docstring": None,
                        "signature": f'<script src="{src}">',
                    }
                )
                continue
            if not body.strip():
                continue
            line_offset = _line_of(content, match.start(2)) - 1
            try:
                inner_imports, inner_symbols = _parse_ts_js(body)
            except Exception:
                inner_imports, inner_symbols = [], []
            for name in inner_imports:
                if name not in imports:
                    imports.append(name)
            for sym in inner_symbols:
                sym["line_start"] += line_offset
                sym["line_end"] += line_offset
                symbols.append(sym)

        for match in _HTML_STYLE_RE.finditer(content):
            symbols.append(
                {
                    "kind": "style",
                    "name": "<style>",
                    "qualname": _disambiguate_qualname("style", seen_qualnames),
                    "line_start": _line_of(content, match.start()),
                    "line_end": _line_of(content, match.end()),
                    "docstring": None,
                }
            )

        symbols.sort(key=lambda s: s.get("line_start", 0))
        return imports, symbols
    except Exception:
        return [], []
