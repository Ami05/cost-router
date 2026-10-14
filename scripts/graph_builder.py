#!/usr/bin/env python3
"""Graph construction: build a symbol graph for a given file."""
from __future__ import annotations

import hashlib
from pathlib import Path

from _session import now_iso
from parsers import EXT_PARSERS, _DEFAULT_LANGUAGE


def file_content_sha256(path: Path) -> str | None:
    """sha256 hex digest of the file's bytes, or None if unreadable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        from graph_store import record_graph_error
        record_graph_error("graph_builder.file_content_sha256", exc, str(path))
        return None


def _build_graph(path: Path, path_hash: str) -> dict | None:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        from graph_store import record_graph_error
        record_graph_error("graph_builder._build_graph.read", exc, str(path))
        return None
    content = raw.decode("utf-8", errors="replace")
    content_sha256 = hashlib.sha256(raw).hexdigest()

    suffix = path.suffix.lower()
    language, parser = EXT_PARSERS.get(suffix, (_DEFAULT_LANGUAGE, None))

    imports: list[str] = []
    symbols: list[dict] = []
    if parser is not None:
        try:
            imports, symbols = parser(content)
        except Exception as exc:
            from graph_store import record_graph_error
            record_graph_error("graph_builder._build_graph.parser", exc, str(path))
            imports, symbols = [], []

    ts = now_iso()
    return {
        "path_hash": path_hash,
        "content_sha256": content_sha256,
        "language": language,
        "generated_at": ts,
        "last_read_at": ts,
        "imports": imports,
        "symbols": symbols,
    }
