#!/usr/bin/env python3
"""Text-format parsers: Markdown, JSON, shell, YAML."""
from __future__ import annotations

import re


_MD_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_JSON_KEY_RE = re.compile(r'^\s{0,2}"([^"]+)"\s*:', re.MULTILINE)
_SHELL_FUNC_RE = re.compile(r"^\s*(?:function\s+)?([\w.-]+)\s*\(\)\s*\{?", re.MULTILINE)
_YAML_KEY_RE = re.compile(r"^(\s*)([\w.-]+):(?:\s|$)", re.MULTILINE)


def _append_symbol(symbols: list[dict], kind: str, name: str, line_start: int) -> None:
    symbols.append(
        {
            "kind": kind,
            "name": name,
            "line_start": line_start,
            "line_end": line_start,
            "docstring": None,
        }
    )


def _parse_markdown(content: str) -> tuple[list[str], list[dict]]:
    symbols: list[dict] = []
    for match in _MD_HEADING_RE.finditer(content):
        line_start = content[: match.start()].count("\n") + 1
        _append_symbol(symbols, "heading", match.group(1).strip(), line_start)
    return [], symbols


def _parse_json_fallback(content: str) -> tuple[list[str], list[dict]]:
    symbols: list[dict] = []
    for match in _JSON_KEY_RE.finditer(content):
        line_start = content[: match.start()].count("\n") + 1
        _append_symbol(symbols, "key", match.group(1), line_start)
    return [], symbols


def _parse_shell(content: str) -> tuple[list[str], list[dict]]:
    symbols: list[dict] = []
    for match in _SHELL_FUNC_RE.finditer(content):
        line_start = content[: match.start()].count("\n") + 1
        _append_symbol(symbols, "function", match.group(1), line_start)
    return [], symbols


def _parse_yaml(content: str) -> tuple[list[str], list[dict]]:
    symbols: list[dict] = []
    for match in _YAML_KEY_RE.finditer(content):
        indent = len(match.group(1))
        if indent > 2:
            continue
        line_start = content[: match.start()].count("\n") + 1
        _append_symbol(symbols, "key", match.group(2), line_start)
    return [], symbols
