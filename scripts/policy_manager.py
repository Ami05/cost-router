#!/usr/bin/env python3
"""cost-router: confidence-tiered, reversible auto-apply of routing proposals.

Facade module — re-exports the public API from policy_applier, policy_store,
and policy_validator. Implementation lives in those modules.

State: policy_proposals.json, policy_changelog.jsonl. CLI: --status, --list,
--undo TOKEN. Proposals accumulate evidence, shadow-validate, then auto-apply
or roll back via undo_token.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Re-export public API
from policy_applier import (  # noqa: F401, E402
    accumulate_evidence,
    apply_proposal,
    check_rollback,
    evaluate_proposals,
    undo,
)
from policy_store import (  # noqa: F401, E402
    CHANGELOG_FILE,
    COUNTERFACTUAL_LOG_FILE,
    DATA_DIR,
    PROPOSALS_FILE,
    RULE_FILE,
    append_counterfactual_log,
    load_changelog,
    load_proposals,
    save_changelog,
    save_proposals,
)
from policy_validator import (  # noqa: F401, E402
    _derive_proposed_row,
    _detect_blast_radius,
    derive_proposal_defaults,
)

# Legacy aliases for callers that used the private _load/_save names
_load_proposals = load_proposals
_save_proposals = save_proposals
_load_changelog = load_changelog
_save_changelog = save_changelog
_append_counterfactual_log = append_counterfactual_log


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_proposals(proposals: list[dict[str, Any]], label: str) -> None:
    print(f"{label} ({len(proposals)}):")
    if not proposals:
        print("  (none)")
        return
    for p in proposals:
        status = p.get("status", "?")
        key = p.get("key", "")
        count = p.get("evidence_count", 0)
        blast = p.get("blast_radius", "?")
        shadow = p.get("shadow_validated", False)
        token = p.get("undo_token") or "-"
        row = p.get("proposed_row", "")
        print(f"  [{status:22s}] key={key}  evidence={count}  blast={blast}"
              f"  shadow_ok={shadow}  token={token}")
        print(f"    row: {row}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage cost-router routing proposals."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--status", action="store_true", help="Show all proposals."
    )
    group.add_argument(
        "--list", action="store_true", help="Show queued proposals only."
    )
    group.add_argument(
        "--undo", metavar="TOKEN", help="Roll back an applied proposal by token."
    )
    args = parser.parse_args()

    if args.status:
        proposals = load_proposals()
        _print_proposals(proposals, "All proposals")

    elif args.list:
        proposals = load_proposals()
        queued = [p for p in proposals if p.get("status") == "queued"]
        _print_proposals(queued, "Queued proposals")

    elif args.undo:
        ok = undo(args.undo)
        if ok:
            print(f"Rolled back proposal with token {args.undo}.")
        else:
            print(
                f"No applied proposal found with token {args.undo}.",
                file=sys.stderr,
            )
            return 1

    else:
        parser.print_help()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
