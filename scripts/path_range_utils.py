"""Path safety and range validation utilities.

Extracted from estimate_dispatch_context.py.
Provides project-root resolution, parent-traversal guards, and
line-range overlap helpers used by the dispatch context estimator
and the grouping enforcement gate.

Stdlib-only; fail-open on malformed paths.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import config_get  # noqa: E402


# ---------------------------------------------------------------------------
# Project root
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    for env_key in ("CURSOR_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        value = os.environ.get(env_key)
        if value:
            return Path(value).resolve()
    return Path.cwd().resolve()


# ---------------------------------------------------------------------------
# Safe path resolution
# ---------------------------------------------------------------------------


def _has_parent_traversal(path_str: str) -> bool:
    parts = re.split(r"[/\\]", path_str)
    return ".." in parts


def resolve_safe_path(path_str: str, root: Path | None = None) -> Path | None:
    """Resolve *path_str* under project root; reject .. and outside-root paths."""
    if not path_str or not isinstance(path_str, str):
        return None
    cleaned = path_str.strip().strip("'\"")
    if not cleaned or _has_parent_traversal(cleaned):
        return None
    base = root if root is not None else _project_root()
    candidate = Path(cleaned)
    try:
        resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
        resolved.relative_to(base)
    except (ValueError, OSError):
        return None
    return resolved


# ---------------------------------------------------------------------------
# Line-range parsing and overlap
# ---------------------------------------------------------------------------

_RANGE_SUFFIX_RE = re.compile(r':(\d+)(?:[-,](\d+))?$')


def _split_path_and_range(fp: str) -> tuple[str, tuple[int, int] | None]:
    m = _RANGE_SUFFIX_RE.search(fp)
    if not m:
        return (fp, None)
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    return (fp[: m.start()], (start, end))


def _ranges_overlap(a: tuple[int, int] | None, b: tuple[int, int] | None) -> bool:
    if a is None or b is None:
        return True
    return a[0] <= b[1] and b[0] <= a[1]


def _any_ref_overlap(refs_a, refs_b) -> bool:
    for path_a, range_a in refs_a:
        for path_b, range_b in refs_b:
            if path_a == path_b and _ranges_overlap(range_a, range_b):
                return True
    return False


def _region_aware_overlap_enabled() -> bool:
    return bool(config_get("dispatch_context.region_aware_overlap", False))
