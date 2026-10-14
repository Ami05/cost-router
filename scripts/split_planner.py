#!/usr/bin/env python3
"""Advisory split planner for partitioning large dispatches (DGC-204).

Given a list of task units with file sets and token estimates, partitions them
into groups at natural boundaries (directory, low-affinity edges per DGC-201
threshold) keeping shared context together.

Each group fits within target_max tokens; returns groups with estimated totals.
Output text integrates into the DGC-203 deny message above 180K hard limit.

Never raises — fail-open patterns with empty results.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from context_estimator import (  # noqa: E402
    extract_paths_from_prompt,
    file_tokens_for_path,
)
from affinity import (  # noqa: E402
    affinity_score,
    get_affinity_threshold,
)


def _units_from_paths(paths: list[str], model: str | None = None) -> list[dict[str, Any]]:
    """Helper: convert extracted paths to task units.

    Groups paths by top-level directory, estimates tokens, and creates units.
    Byte-identical legacy behavior for extract_units_from_prompt.

    Args:
        paths: List of file paths.
        model: Model identifier for token estimation.

    Returns:
        List of unit dicts with id, goal, files, tokens.
    """
    if not paths:
        return []

    # Group paths by top-level directory prefix
    groups: dict[str, list[str]] = {}
    for path_str in paths:
        try:
            path = Path(path_str)
            # Use top-level dir as grouping key; fall back to filename if no dir
            parts = path.parts
            key = parts[0] if len(parts) > 1 else str(path)
            if key not in groups:
                groups[key] = []
            groups[key].append(path_str)
        except Exception:
            # Gracefully skip malformed paths
            continue

    # Create units from groups
    units: list[dict[str, Any]] = []
    for idx, (dir_key, file_list) in enumerate(groups.items()):
        try:
            tokens = 0
            for file_str in file_list:
                try:
                    file_path = Path(file_str)
                    file_tokens = file_tokens_for_path(file_path, model)
                    tokens += file_tokens
                except Exception:
                    # Gracefully skip files we can't estimate
                    continue

            unit = {
                "id": f"unit-{idx}",
                "goal": f"Process {dir_key} ({len(file_list)} files)",
                "files": sorted(file_list),
                "tokens": tokens,
            }
            units.append(unit)
        except Exception:
            # Skip malformed units
            continue

    return units


def extract_units_from_prompt(prompt: str, model: str | None = None) -> list[dict[str, Any]]:
    """Extract task units from a dispatch prompt.

    Each unit represents a logical work item, derived from file path references
    in the prompt. File paths are extracted, grouped by common directory prefix,
    and token estimates are computed per group.

    Args:
        prompt: Dispatch prompt text.
        model: Model identifier for token estimation (e.g., 'haiku', 'sonnet').
               If None, uses basic file-size-based estimation.

    Returns:
        List of dicts, each with:
          - id: str, unique identifier (generated from path group)
          - goal: str, descriptive goal text (snippet from prompt context)
          - files: list[str], file paths in this unit
          - tokens: int, estimated tokens for this unit's files
        Empty list if no paths extracted or on extraction failure.
    """
    try:
        paths = extract_paths_from_prompt(prompt)
    except Exception:
        return []

    return _units_from_paths(paths, model)


def extract_units_from_dispatch_json(prompt: str, model: str | None = None) -> list[dict[str, Any]]:
    """Extract task units from a JSON dispatch payload.

    Attempts to parse prompt as JSON and extract file paths from "files" and "scope"
    fields. Falls back to extract_units_from_prompt on JSON parse failure or if
    payload is not a dict.

    Args:
        prompt: JSON string or plain dispatch prompt text.
        model: Model identifier for token estimation.

    Returns:
        List of unit dicts with id, goal, files, tokens.
        Empty list if extraction fails.
    """
    try:
        payload = json.loads(prompt)
    except (json.JSONDecodeError, ValueError):
        # Not valid JSON; fall back to prompt parsing
        return extract_units_from_prompt(prompt, model=model)

    if not isinstance(payload, dict):
        # JSON is valid but not a dict; fall back
        return extract_units_from_prompt(prompt, model=model)

    paths: list[str] = []

    # Extract from "files" field (list of str, may have line suffixes)
    files_field = payload.get("files")
    if isinstance(files_field, list):
        for item in files_field:
            if isinstance(item, str):
                # Strip line suffixes like ":N" or ":N,M"
                path_clean = item.split(":")[0]
                if path_clean:
                    paths.append(path_clean)

    # Extract from "scope" field (list of str, may be directories or files)
    scope_field = payload.get("scope")
    if isinstance(scope_field, list):
        max_files = 10
        payload_max = payload.get("max_files")
        if isinstance(payload_max, int):
            max_files = payload_max

        for item in scope_field:
            if isinstance(item, str):
                path_clean = item.split(":")[0]
                try:
                    p = Path(path_clean)
                    if p.is_dir():
                        # Expand directory to files (up to max_files)
                        try:
                            dir_files = [str(f) for f in sorted(p.glob("**/*")) if f.is_file()]
                            paths.extend(dir_files[:max_files])
                        except Exception:
                            # Skip if directory expansion fails
                            pass
                    else:
                        # Add file as-is
                        if path_clean:
                            paths.append(path_clean)
                except Exception:
                    # Skip malformed paths
                    pass

    # Remove duplicates while preserving order
    seen: set[str] = set()
    unique_paths = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique_paths.append(p)

    # If we extracted no paths, fall back to prompt parsing
    if not unique_paths:
        return extract_units_from_prompt(prompt, model=model)

    return _units_from_paths(unique_paths, model=model)


def count_dispatch_paths(prompt: str) -> int:
    """Count the number of paths that would be extracted from a dispatch prompt.

    Attempts JSON parsing first; if successful and yields paths, returns path count.
    Otherwise, counts paths from legacy prompt parsing.
    Never raises; returns 0 on any error.

    Args:
        prompt: JSON string or plain dispatch prompt text.

    Returns:
        Number of unique paths extracted, or 0 on error.
    """
    try:
        payload = json.loads(prompt)
    except (json.JSONDecodeError, ValueError):
        # Not valid JSON; try legacy prompt parsing
        try:
            paths = extract_paths_from_prompt(prompt)
            return len(paths) if paths else 0
        except Exception:
            return 0

    if not isinstance(payload, dict):
        # JSON is valid but not a dict; fall back to legacy
        try:
            paths = extract_paths_from_prompt(prompt)
            return len(paths) if paths else 0
        except Exception:
            return 0

    paths_set: set[str] = set()

    # Count from "files" field
    files_field = payload.get("files")
    if isinstance(files_field, list):
        for item in files_field:
            if isinstance(item, str):
                path_clean = item.split(":")[0]
                if path_clean:
                    paths_set.add(path_clean)

    # Count from "scope" field
    scope_field = payload.get("scope")
    if isinstance(scope_field, list):
        max_files = 10
        payload_max = payload.get("max_files")
        if isinstance(payload_max, int):
            max_files = payload_max

        for item in scope_field:
            if isinstance(item, str):
                path_clean = item.split(":")[0]
                try:
                    p = Path(path_clean)
                    if p.is_dir():
                        try:
                            dir_files = [str(f) for f in sorted(p.glob("**/*")) if f.is_file()]
                            for f in dir_files[:max_files]:
                                paths_set.add(f)
                        except Exception:
                            pass
                    else:
                        if path_clean:
                            paths_set.add(path_clean)
                except Exception:
                    pass

    # If JSON yielded paths, return count
    if paths_set:
        return len(paths_set)

    # Otherwise fall back to legacy prompt parsing
    try:
        paths = extract_paths_from_prompt(prompt)
        return len(paths) if paths else 0
    except Exception:
        return 0


def partition_units(
    units: list[dict[str, Any]],
    target_max: int,
    affinity_threshold: float | None = None,
) -> dict[str, Any]:
    """Partition units into groups respecting affinity and size constraints.

    Builds groups respecting:
    1. High-affinity units are kept together (unless a single unit exceeds target_max).
    2. Each group's total tokens <= target_max.
    3. Natural boundaries: directory prefixes are split points.
    4. Deterministic and order-preserving.

    Args:
        units: List of task units (output from extract_units_from_prompt).
               Each unit dict should have: id, goal, files (list), tokens (int).
        target_max: Maximum tokens per group.
        affinity_threshold: Affinity threshold for grouping (default: from config).
                           If None, loads from affinity.get_affinity_threshold().

    Returns:
        Dict with:
          - groups: list of group dicts, each with:
              - id: str, group identifier
              - units: list of unit ids in this group
              - files: list of all file paths (union of units)
              - tokens: int, total estimated tokens
              - unsplittable: bool, True if any unit alone exceeds target_max
          - total_tokens: int, sum of all group tokens
          - total_groups: int, count of groups
          - summary: str, human-readable summary of the split
        Empty groups list if units is empty or None.
    """
    if not units or not isinstance(units, list):
        return {
            "groups": [],
            "total_tokens": 0,
            "total_groups": 0,
            "summary": "No units to partition.",
        }

    if affinity_threshold is None:
        affinity_threshold = get_affinity_threshold()

    # Validate threshold
    try:
        affinity_threshold = float(affinity_threshold)
        if not 0 <= affinity_threshold <= 1:
            affinity_threshold = 0.3
    except (ValueError, TypeError):
        affinity_threshold = 0.3

    # Greedy packing: process units in order, creating new groups when needed
    groups: list[dict[str, Any]] = []
    current_group: dict[str, Any] | None = None

    for unit in units:
        if not isinstance(unit, dict) or "id" not in unit:
            continue

        unit_tokens = unit.get("tokens", 0)
        if not isinstance(unit_tokens, int):
            unit_tokens = 0

        # If unit alone exceeds target_max, it goes in its own group (unsplittable)
        if unit_tokens > target_max:
            # Finalize current group if any
            if current_group and current_group["units"]:
                groups.append(current_group)
            # Create single-unit group with unsplittable flag
            new_group = {
                "id": f"group-{len(groups)}",
                "units": [unit["id"]],
                "files": unit.get("files", []),
                "tokens": unit_tokens,
                "unsplittable": True,
            }
            groups.append(new_group)
            current_group = None
            continue

        # Try to add unit to current group if affinity is high
        can_add_to_current = False
        if current_group and current_group["units"]:
            # Check affinity with the last unit in the group
            last_unit_id = current_group["units"][-1]
            last_unit = next((u for u in units if u.get("id") == last_unit_id), None)
            if last_unit:
                score = affinity_score(last_unit, unit)
                if score >= affinity_threshold:
                    can_add_to_current = True

        # If can add and fits: add to current group
        if can_add_to_current and (current_group["tokens"] + unit_tokens) <= target_max:
            current_group["units"].append(unit["id"])
            current_group["files"].extend(unit.get("files", []))
            current_group["tokens"] += unit_tokens
            continue

        # If can't add but current group is not empty, finalize it
        if current_group and current_group["units"]:
            groups.append(current_group)

        # Start new group with this unit
        current_group = {
            "id": f"group-{len(groups)}",
            "units": [unit["id"]],
            "files": unit.get("files", []),
            "tokens": unit_tokens,
            "unsplittable": False,
        }

    # Finalize last group
    if current_group and current_group["units"]:
        groups.append(current_group)

    # Compute totals
    total_tokens = sum(g["tokens"] for g in groups)
    unsplittable_count = sum(1 for g in groups if g.get("unsplittable", False))

    summary = f"Split into {len(groups)} groups"
    if unsplittable_count > 0:
        summary += f" ({unsplittable_count} unsplittable)"
    summary += f"; total ~{total_tokens:,} tokens."

    return {
        "groups": groups,
        "total_tokens": total_tokens,
        "total_groups": len(groups),
        "summary": summary,
    }


def split_plan_text(partition_result: dict[str, Any], hard_limit: int = 180000) -> str:
    """Generate human-readable split plan text for deny message integration.

    Args:
        partition_result: Output from partition_units.
        hard_limit: Hard token limit (for context in message).

    Returns:
        Multi-line text describing the split plan, or empty string if no groups.
    """
    groups = partition_result.get("groups", [])
    if not groups:
        return ""

    lines = [
        f"\n[Recommended split plan: {partition_result.get('summary', '')}]\n",
    ]

    for group in groups:
        group_id = group.get("id", "unknown")
        unit_ids = group.get("units", [])
        group_tokens = group.get("tokens", 0)
        unsplittable = group.get("unsplittable", False)

        units_str = ", ".join(unit_ids) if unit_ids else "no units"
        flag_str = " (unsplittable — exceeds target_max)" if unsplittable else ""
        lines.append(f"  {group_id}: {units_str} (~{group_tokens:,} tokens){flag_str}")

    lines.append(
        f"\nTotal: {partition_result.get('total_groups', 0)} groups, "
        f"~{partition_result.get('total_tokens', 0):,} tokens."
    )

    return "\n".join(lines)


def plan_split_for_dispatch(prompt: str, hard_limit: int = 180000, model: str | None = None) -> str:
    """Plan a split for a dispatch that exceeds the hard limit.

    Orchestrates the full pipeline: extract units → partition → format text.
    Supports both JSON dispatch payloads and legacy text prompts.

    Args:
        prompt: Dispatch prompt text or JSON payload.
        hard_limit: Hard token limit (context for messages).
        model: Model identifier for token estimation.

    Returns:
        Multi-line split plan text, or empty string if extraction/partition fails.
    """
    try:
        units = extract_units_from_dispatch_json(prompt, model=model)
        if not units:
            return ""

        # Assume target_max is hard_limit - 10% margin
        target_max = int(hard_limit * 0.9)
        partition = partition_units(units, target_max)

        return split_plan_text(partition, hard_limit=hard_limit)
    except Exception:
        # Fail-open: return empty string on any error
        return ""
