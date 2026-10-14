#!/usr/bin/env python3
"""Parser registry for build_file_graph symbol extraction."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from parsers.python_parser import _parse_python
from parsers.java_parser import _parse_java
from parsers.ts_js_parser import _parse_ts_js
from parsers.html_parser import _parse_html
from parsers.text_parser import _parse_markdown, _parse_json_fallback, _parse_shell, _parse_yaml

_DEFAULT_LANGUAGE = "text"

EXT_PARSERS: dict[str, tuple[str, Callable[[str], tuple[list[str], list[dict]]]]] = {
    ".py": ("python", _parse_python),
    ".java": ("java", _parse_java),
    ".ts": ("typescript", _parse_ts_js),
    ".tsx": ("typescript", _parse_ts_js),
    ".js": ("javascript", _parse_ts_js),
    ".jsx": ("javascript", _parse_ts_js),
    ".html": ("html", _parse_html),
    ".htm": ("html", _parse_html),
    ".md": ("markdown", _parse_markdown),
    ".mdc": ("markdown", _parse_markdown),
    ".json": ("json", _parse_json_fallback),
    ".sh": ("shell", _parse_shell),
    ".yml": ("yaml", _parse_yaml),
    ".yaml": ("yaml", _parse_yaml),
}


def _language_for(path: Path) -> str:
    return EXT_PARSERS.get(path.suffix.lower(), (_DEFAULT_LANGUAGE, None))[0]
