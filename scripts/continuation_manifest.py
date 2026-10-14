#!/usr/bin/env python3
"""Continuation manifest parsing, validation, and compaction for chained subagent work.

Parse fenced JSON blocks from subagent final reports; validate against schema;
generate compact context for the next dispatch; detect stall (no progress).
Never raises — parse_manifest returns status=UNKNOWN with reasons on any error.
"""
from __future__ import annotations

import json
import re
from typing import Any

from _config import config_get


def parse_manifest(report_text: str) -> dict[str, Any]:
    """Extract and validate the last fenced continuation-manifest JSON block.

    Args:
        report_text: Subagent final report text.

    Returns:
        dict with at minimum: status (COMPLETE|PARTIAL|UNKNOWN), reason (str if UNKNOWN).
        If valid, also: done[], remaining[], tokens_used, phase, depth.
        Never raises.
    """
    if not report_text or not isinstance(report_text, str):
        return {"status": "UNKNOWN", "reason": "empty or non-string report"}

    # Locate all fenced blocks; last wins.
    pattern = r"```continuation-manifest\s*\n(.*?)\n```"
    matches = list(re.finditer(pattern, report_text, re.DOTALL))

    if not matches:
        return {"status": "UNKNOWN", "reason": "no fenced continuation-manifest block"}

    last_match = matches[-1]
    json_text = last_match.group(1).strip()

    # Parse JSON.
    try:
        manifest = json.loads(json_text)
    except json.JSONDecodeError as e:
        return {"status": "UNKNOWN", "reason": f"invalid JSON: {e}"}

    if not isinstance(manifest, dict):
        return {"status": "UNKNOWN", "reason": "fenced block is not a JSON object"}

    # Validate required fields and schema.
    status = manifest.get("status", "UNKNOWN")
    if status not in ("COMPLETE", "PARTIAL", "UNKNOWN"):
        status = "UNKNOWN"

    # Assign UNKNOWN if status field missing or invalid.
    if "status" not in manifest or manifest["status"] not in ("COMPLETE", "PARTIAL"):
        manifest["status"] = "UNKNOWN"
        manifest["reason"] = "status field missing or invalid"

    # Truncate remaining to max_remaining_items limit.
    max_items = config_get("continuation.max_remaining_items", 10)
    remaining = manifest.get("remaining", [])
    if isinstance(remaining, list) and len(remaining) > max_items:
        manifest["remaining"] = remaining[:max_items]
        if "reason" not in manifest:
            manifest["reason"] = f"remaining truncated to {max_items} items"

    return manifest


def compact_continuation(manifest: dict[str, Any]) -> str:
    """Generate a compact context string for the next dispatch.

    Bounded by max_compact_chars limit; includes remaining goals and files only,
    excludes done[] and history.

    Args:
        manifest: Parsed manifest dict (output of parse_manifest).

    Returns:
        Compact JSON string (remaining goals + files only), bounded by config limit.
    """
    max_chars = config_get("continuation.max_compact_chars", 2000)

    remaining = manifest.get("remaining", [])
    if not remaining:
        return ""

    # Build a minimal object: id, goal, files for each remaining item.
    compact = {
        "status": manifest.get("status", "UNKNOWN"),
        "remaining": [
            {
                "id": item.get("id"),
                "goal": item.get("goal", ""),
                "files": item.get("files", []),
            }
            for item in remaining
            if isinstance(item, dict)
        ],
        "depth": manifest.get("depth", 0),
    }

    json_str = json.dumps(compact, separators=(",", ":"))

    # Truncate if needed.
    if len(json_str) > max_chars:
        # Progressively strip files, then items.
        compact["remaining"] = [
            {k: v for k, v in item.items() if k != "files"} for item in compact["remaining"]
        ]
        json_str = json.dumps(compact, separators=(",", ":"))

        if len(json_str) > max_chars:
            # Keep only first N items.
            for n in range(len(compact["remaining"]), 0, -1):
                compact["remaining"] = compact["remaining"][:n]
                json_str = json.dumps(compact, separators=(",", ":"))
                if len(json_str) <= max_chars:
                    break

    return json_str


def no_progress(prev: dict[str, Any] | None, curr: dict[str, Any] | None) -> bool:
    """Check whether remaining work ids are unchanged (stall detection).

    Returns True if both manifests have identical non-empty remaining ids (work not
    progressing). An empty current remaining list means done, so returns False.

    Args:
        prev: Previous manifest (from last dispatch).
        curr: Current manifest (from latest report).

    Returns:
        True if no progress detected.
    """
    prev_ids = None
    curr_ids = None

    if prev and isinstance(prev, dict):
        remaining = prev.get("remaining")
        if not isinstance(remaining, list):
            remaining = []
        prev_ids = sorted(
            [str(item.get("id")) for item in remaining if isinstance(item, dict) and "id" in item]
        )

    if curr and isinstance(curr, dict):
        remaining = curr.get("remaining")
        if not isinstance(remaining, list):
            remaining = []
        curr_ids = sorted(
            [str(item.get("id")) for item in remaining if isinstance(item, dict) and "id" in item]
        )

    # Nothing remaining now means the work finished, not stalled.
    if not curr_ids:
        return False

    return prev_ids == curr_ids


def is_manifest_valid(manifest: dict[str, Any] | None) -> bool:
    """Check whether a manifest is valid (COMPLETE or PARTIAL status).

    Args:
        manifest: Parsed manifest dict (output of parse_manifest).

    Returns:
        True if status is COMPLETE or PARTIAL; False if UNKNOWN.
    """
    if not manifest or not isinstance(manifest, dict):
        return False
    return manifest.get("status") in ("COMPLETE", "PARTIAL")


def get_remaining_ids(manifest: dict[str, Any] | None) -> list[str]:
    """Extract remaining item IDs from a manifest.

    Args:
        manifest: Parsed manifest dict (output of parse_manifest).

    Returns:
        List of remaining item IDs as strings. Empty list if no manifest or no remaining items.
    """
    if not manifest or not isinstance(manifest, dict):
        return []

    remaining = manifest.get("remaining", [])
    if not isinstance(remaining, list):
        return []

    ids = []
    for item in remaining:
        if isinstance(item, dict) and "id" in item:
            ids.append(str(item["id"]))
    return ids


def validate_manifest_lineage(
    manifest: dict[str, Any] | None, task_text: str | None
) -> tuple[bool, str]:
    """Validate that task_text references at least one remaining item from manifest.

    DGC-206 step 2: hook validates that the next Task matches a remaining item from the manifest.
    This check ensures continuity across manifests.

    Args:
        manifest: Parsed manifest dict (output of parse_manifest).
        task_text: The Task prompt text to validate against remaining items.

    Returns:
        Tuple of (is_valid: bool, reason: str). is_valid=True if task_text contains
        at least one remaining item ID or goal. reason describes the outcome.
    """
    if not manifest or not isinstance(manifest, dict):
        return (False, "No valid manifest to validate against")

    if not task_text:
        return (False, "No task_text to validate")

    remaining = manifest.get("remaining", [])
    if not isinstance(remaining, list) or not remaining:
        return (False, "Manifest has no remaining items")

    # Check if any remaining item ID is mentioned in task_text
    for item in remaining:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if item_id and str(item_id) in task_text:
            return (True, f"Task references remaining item {item_id}")
        # Also check goal text as a fallback
        goal = item.get("goal", "")
        if goal and len(goal) > 5 and goal in task_text:
            return (True, f"Task references goal: {goal[:50]}...")

    return (False, "Task does not reference any remaining items from manifest")
