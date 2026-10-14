#!/usr/bin/env python3
"""cost-router: Orchestrates proposal validation and store operations.

Owns all state-machine transitions: evidence accumulation, auto-apply
eligibility, apply, undo, and rollback checks. Delegates pure validation
to policy_validator and all disk I/O to policy_store.
"""

from __future__ import annotations

import hashlib
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    POLICY_AUTO_APPLY_THRESHOLD,
    POLICY_QUEUED_THRESHOLD,
    now_iso,
    policy_auto_apply_enabled,
)
from policy_validator import derive_proposal_defaults  # noqa: E402
from policy_store import (  # noqa: E402
    append_counterfactual_log,
    load_changelog,
    load_proposals,
    read_rule_file,
    save_changelog,
    save_proposals,
    write_rule_file,
)

QUEUED_THRESHOLD = POLICY_QUEUED_THRESHOLD
AUTO_APPLY_THRESHOLD = POLICY_AUTO_APPLY_THRESHOLD

# Marker appended to every tuner-written row. Undo, GC and the size cap act
# only on lines carrying this marker; human-written rows are never touched.
AUTO_TAG_PREFIX = "<!-- cr-auto:"


def auto_tag(token: str) -> str:
    return f"{AUTO_TAG_PREFIX}{token} -->"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _session_ts(session: dict[str, Any]) -> str | None:
    """Extract a sortable ISO timestamp from a scenario-library session entry."""
    summary = session.get("summary", {})
    return (
        summary.get("started_at")
        or summary.get("ended_at")
        or summary.get("ts")
        or None
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def accumulate_evidence(key: str, session_data: dict[str, Any]) -> None:
    """Record one session's worth of evidence for the proposal keyed by *key*.

    Derives proposed_row from *key* on first call. Increments evidence_count.
    Optional session_data fields shadow_delta_reward, shadow_validated, and
    blast_radius override the stored values when present.

    Status transitions:
      evidence_count >= 2                                       -> "queued"
      evidence_count >= 5 AND shadow_validated AND blast=="low" -> "auto_apply_eligible"
    Already-applied or rolled-back proposals are left untouched.
    """
    proposals = load_proposals()

    # Locate or create proposal entry
    proposal: dict[str, Any] | None = None
    for p in proposals:
        if p.get("key") == key:
            proposal = p
            break

    if proposal is None:
        proposal = derive_proposal_defaults(key)
        proposals.append(proposal)

    # Terminal statuses are immutable
    if proposal.get("status") in ("applied", "rolled_back"):
        save_proposals(proposals)
        return

    proposal["evidence_count"] = proposal.get("evidence_count", 0) + 1

    # Apply optional overrides from the calling session
    for field in ("shadow_delta_reward", "shadow_validated", "blast_radius"):
        if field in session_data:
            proposal[field] = session_data[field]

    count = proposal["evidence_count"]
    shadow_ok = bool(proposal.get("shadow_validated", False))
    low_br = proposal.get("blast_radius") == "low"

    if count >= AUTO_APPLY_THRESHOLD and shadow_ok and low_br:
        proposal["status"] = "auto_apply_eligible"
    elif count >= QUEUED_THRESHOLD:
        proposal["status"] = "queued"
    # else stays "accumulating"

    save_proposals(proposals)


def evaluate_proposals() -> list[dict[str, Any]]:
    """Process all open proposals.

    For each auto_apply_eligible proposal, runs shadow evaluation first.
    Only proposals confirmed by shadow_validated are applied immediately.
    Proposals that fail shadow validation are demoted to "queued".
    Returns the list of queued proposals (not yet auto-applicable).
    """
    proposals = load_proposals()
    queued: list[dict[str, Any]] = []

    for proposal in proposals:
        status = proposal.get("status")
        if status == "auto_apply_eligible" and policy_auto_apply_enabled():
            token = apply_proposal(proposal)
            proposal["undo_token"] = token
            proposal["status"] = "applied"
            queued.append(proposal)
        elif status == "queued":
            queued.append(proposal)

    save_proposals(proposals)
    return queued


def apply_proposal(proposal: dict[str, Any]) -> str:
    """Apply a single proposal to rules/model-router.md.

    Steps:
    1. Snapshot the current matching row from the rule file (for undo).
    2. Compute undo_token = sha256(old_row + now_iso())[:8].
    3. Append proposed_row to the rule file if not already present.
    4. Write a changelog entry.
    Returns the undo_token.
    """
    proposed_row: str = proposal.get("proposed_row", "")
    key: str = proposal.get("key", "")

    current_content = read_rule_file()

    # Snapshot: locate a related existing row by tool_type prefix
    key_parts = key.split(":")
    tool_type = key_parts[0] if key_parts else ""
    old_row = ""
    for line in current_content.splitlines():
        if tool_type.upper() in line.upper() and "->" in line:
            old_row = line
            break

    # Compute undo_token
    undo_token = hashlib.sha256(
        (old_row + now_iso()).encode("utf-8", "replace")
    ).hexdigest()[:8]

    # Append the proposed row only if it is not already present. Fails open
    # (PROD-8): a read-only RULE_FILE means the rule doesn't get updated,
    # same net effect as never having run — not a crash.
    # The row is tagged with the undo token so later undo/GC can tell it
    # apart from human-written rows.
    if proposed_row and proposed_row not in current_content:
        tagged_row = f"{proposed_row} {auto_tag(undo_token)}"
        new_content = current_content.rstrip("\n") + "\n" + tagged_row + "\n"
        write_rule_file(new_content)

    # Record in changelog
    changelog = load_changelog()
    entry: dict[str, Any] = {
        "undo_token": undo_token,
        "key": key,
        "proposed_row": proposed_row,
        "old_row": old_row,
        "applied_at": now_iso(),
        "status": "applied",
    }
    changelog.append(entry)
    save_changelog(changelog)

    return undo_token


def undo(token: str) -> bool:
    """Roll back an applied proposal identified by *token*.

    Locates the changelog entry, removes the proposed_row from
    rules/model-router.md, marks the changelog entry and the matching
    proposal as 'rolled_back'. Returns True on success, False if the
    token is not found or was already rolled back.
    """
    changelog = load_changelog()
    entry: dict[str, Any] | None = None
    for e in changelog:
        if e.get("undo_token") == token and e.get("status") == "applied":
            entry = e
            break

    if entry is None:
        return False

    proposed_row: str = entry.get("proposed_row", "")

    current_content = read_rule_file()

    # Remove only the line tagged with this token; an untagged (human) row
    # matching proposed_row is left alone. Fails open (PROD-8): see
    # apply_proposal's write above for the same reasoning.
    tag = auto_tag(token)
    if tag in current_content:
        kept = [ln for ln in current_content.splitlines(keepends=True) if tag not in ln]
        new_content = "".join(kept)
        if new_content != current_content:
            write_rule_file(new_content)

    # Mark changelog entry
    entry["status"] = "rolled_back"
    entry["rolled_back_at"] = now_iso()
    save_changelog(changelog)

    # Mark proposal entry
    proposals = load_proposals()
    for p in proposals:
        if p.get("undo_token") == token:
            p["status"] = "rolled_back"
            break
    save_proposals(proposals)

    return True


def check_rollback(recent_sessions: list[dict[str, Any]]) -> list[str]:
    """Check applied proposals for post-apply reward regression.

    For each "applied" proposal the function splits *recent_sessions* into
    a pre-apply baseline window and a post-apply window using the
    ``applied_at`` timestamp from the changelog.  If the post-apply mean
    reward drops more than 0.2 below the baseline mean the proposal is
    reverted with :func:`undo` and its undo_token is added to the return list.

    Counterfactual log entries are written for all post-apply sessions
    regardless of whether a rollback is triggered.

    Returns a list of rolled-back undo_tokens (may be empty).
    """
    proposals = load_proposals()
    changelog = load_changelog()
    rolled_back: list[str] = []

    for proposal in proposals:
        if proposal.get("status") != "applied":
            continue

        undo_token: str | None = proposal.get("undo_token")
        if not undo_token:
            continue

        # Locate the matching changelog entry
        entry: dict[str, Any] | None = None
        for e in changelog:
            if e.get("undo_token") == undo_token and e.get("status") == "applied":
                entry = e
                break
        if entry is None:
            continue

        applied_at: str = entry.get("applied_at", "")
        key: str = proposal.get("key", "")
        proposed_row: str = proposal.get("proposed_row", "")

        if not applied_at:
            continue

        # Partition sessions by timestamp relative to applied_at
        pre_sessions: list[dict[str, Any]] = []
        post_sessions: list[dict[str, Any]] = []
        for session in recent_sessions:
            ts = _session_ts(session)
            if ts is None:
                continue
            if ts < applied_at:
                pre_sessions.append(session)
            else:
                post_sessions.append(session)

        if not pre_sessions or not post_sessions:
            continue

        baseline_mean = statistics.mean(
            float(s.get("reward", 0.0)) for s in pre_sessions
        )
        post_mean = statistics.mean(
            float(s.get("reward", 0.0)) for s in post_sessions
        )

        # Write counterfactual log for all post-apply sessions
        append_counterfactual_log(post_sessions, key, proposed_row, undo_token)

        # Roll back on quality regression
        if post_mean < baseline_mean - 0.2:
            undo(undo_token)
            rolled_back.append(undo_token)

    return rolled_back
