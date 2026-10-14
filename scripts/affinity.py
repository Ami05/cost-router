"""Context-affinity scoring for task clustering (DGC-201).

Provides affinity_score(a, b) combining file-set overlap (Jaccard) and shared
directory prefix to determine whether two task units should be grouped together
in a dispatch batch.

Affinity scores range from 0 to 1:
- 0: disjoint file sets, no shared directory prefix
- 1: identical file sets and deepest directory overlap
- threshold (default 0.3): minimum affinity to keep tasks grouped without split

The affinity score is deterministic and measurable against recorded dispatches.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _get_affinity_config() -> dict[str, Any]:
    """Load dispatch_context.affinity from config.json.

    Returns an empty dict if config cannot be loaded; caller handles gracefully.
    """
    try:
        config_path = Path(__file__).resolve().parent.parent / "config.json"
        if not config_path.exists():
            return {}
        with open(config_path, "r") as f:
            config = json.load(f)
        affinity = config.get("dispatch_context", {}).get("affinity", {})
        return affinity
    except (json.JSONDecodeError, IOError, OSError):
        return {}


def get_affinity_threshold() -> float:
    """Get the affinity threshold from config.

    Returns: float in [0, 1], default 0.3.
    """
    affinity = _get_affinity_config()
    threshold = affinity.get("threshold", 0.3)
    try:
        threshold_float = float(threshold)
        if 0 <= threshold_float <= 1:
            return threshold_float
        return 0.3
    except (ValueError, TypeError):
        return 0.3


def _jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    """Compute Jaccard similarity of two sets.

    Jaccard(A, B) = |A ∩ B| / |A ∪ B|

    Returns 1.0 if both sets are empty (vacuous truth).
    Returns 0.0 if sets are disjoint.
    """
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    if union == 0:
        return 0.0
    return intersection / union


def _shared_dir_prefix(files_a: list[str], files_b: list[str]) -> float:
    """Compute shared directory prefix depth.

    For each pair of files (one from A, one from B), compute the longest common
    directory prefix. Return the maximum normalized depth: shared_parts / total_parts
    of the deeper file.

    Examples:
      - ["scripts/foo.py"] vs ["scripts/bar.py"] -> shared: ["scripts"], depth 1/2 = 0.5
      - ["scripts/sub/foo.py"] vs ["scripts/bar.py"] -> shared: ["scripts"], depth 1/2 = 0.5
      - ["tests/test.py"] vs ["scripts/foo.py"] -> no shared prefix, 0.0

    Returns 0.0 if no files, or if no shared directory prefix found.
    """
    if not files_a or not files_b:
        return 0.0

    max_shared_depth = 0.0
    for file_a in files_a:
        path_a = Path(file_a)
        parts_a = path_a.parts[:-1]  # exclude filename
        for file_b in files_b:
            path_b = Path(file_b)
            parts_b = path_b.parts[:-1]  # exclude filename

            # Find longest common prefix
            shared_len = 0
            for pa, pb in zip(parts_a, parts_b):
                if pa == pb:
                    shared_len += 1
                else:
                    break

            if shared_len > 0:
                deeper_depth = max(len(parts_a), len(parts_b))
                if deeper_depth > 0:
                    normalized = shared_len / deeper_depth
                    max_shared_depth = max(max_shared_depth, normalized)

    return max_shared_depth


def affinity_score(
    unit_a: dict[str, Any] | None,
    unit_b: dict[str, Any] | None,
) -> float:
    """Compute affinity score between two task units.

    Combines:
    1. Jaccard similarity of file sets (weight 0.7)
    2. Shared directory prefix depth (weight 0.3)

    Args:
        unit_a, unit_b: dict with optional "files" list or "file_paths" key.
                        None is treated as empty file set.

    Returns:
        float in [0, 1]: affinity score. Higher = more similar context.
    """
    if unit_a is None or unit_b is None:
        return 0.0

    # Extract file lists from units
    files_a = unit_a.get("files") or unit_a.get("file_paths") or []
    files_b = unit_b.get("files") or unit_b.get("file_paths") or []

    if not isinstance(files_a, list):
        files_a = []
    if not isinstance(files_b, list):
        files_b = []

    # Normalize to strings
    files_a_str = [str(f) for f in files_a]
    files_b_str = [str(f) for f in files_b]

    # Compute Jaccard similarity
    jaccard = _jaccard_similarity(set(files_a_str), set(files_b_str))

    # Compute shared directory prefix depth
    shared_prefix = _shared_dir_prefix(files_a_str, files_b_str)

    # Weighted combination: 70% Jaccard, 30% shared prefix
    score = 0.7 * jaccard + 0.3 * shared_prefix

    return min(1.0, max(0.0, score))


def is_high_affinity(unit_a: dict[str, Any] | None, unit_b: dict[str, Any] | None) -> bool:
    """Check if two units have affinity above threshold.

    Returns True if affinity_score(unit_a, unit_b) >= threshold.
    """
    score = affinity_score(unit_a, unit_b)
    threshold = get_affinity_threshold()
    return score >= threshold
