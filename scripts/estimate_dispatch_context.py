#!/usr/bin/env python3
"""Pre-dispatch context estimator — CLI + importable API.

Re-export shim. Implementation split into three focused modules:
  - path_range_utils      : project-root resolution, safe-path guards, line-range helpers
  - context_estimator     : token estimation, handoff classification, registry helpers
  - dispatch_budget_gate  : budget nudge/deny messages, grouping enforcement

All public symbols are re-exported here so existing importers
(check_routing.py, agent_call_tracker.py, gates.py, etc.) continue to work
without changes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

# -- path_range_utils ---------------------------------------------------------
from path_range_utils import (  # noqa: F401, E402
    _any_ref_overlap,
    _has_parent_traversal,
    _project_root,
    _ranges_overlap,
    _region_aware_overlap_enabled,
    _split_path_and_range,
    resolve_safe_path,
)

# -- context_estimator --------------------------------------------------------
from context_estimator import (  # noqa: F401, E402
    EST_LABEL,
    _CODE_EXTS,
    _SYMBOL_EXTS,
    _dispatch_registry_file,
    _registry_workers,
    _session_agents,
    _worker_frozen_in_plan_mode,
    cc_resume_suggest,
    classify_handoff,
    classify_prompt_composition,
    content_kind_for_path,
    content_kind_for_prompt,
    effective_prompt_text,
    estimate_dispatch_context,
    extract_file_paths,
    extract_paths_from_prompt,
    extract_shell_command,
    file_tokens_for_path,
    load_dispatch_registry,
    mark_registry_busy,
    mark_registry_idle,
    prompt_bloat_ratio,
    suggest_resume,
    verify_template_hash,
)

# -- dispatch_budget_gate -----------------------------------------------------
from dispatch_budget_gate import (  # noqa: F401, E402
    DISPATCH_GROUP_OVERRIDE_RE,
    _grouping_enforce_enabled,
    _has_dispatch_group_override,
    _task_prompt_from_input,
    budget_deny_message,
    budget_nudge_message,
    check_grouping_enforcement,
    grouping_deny_message,
)


# ---------------------------------------------------------------------------
# CLI (kept in shim — depends on estimate_dispatch_context + suggest_resume)
# ---------------------------------------------------------------------------


def _parse_files_arg(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Estimate subagent dispatch context (est.).")
    parser.add_argument("--json", action="store_true", help="Emit JSON on stdout")
    parser.add_argument("--prompt", default="", help="Task prompt text")
    parser.add_argument("--files", default="", help="Comma-separated file paths")
    parser.add_argument("--model", default=None, help="Dispatched model name")
    parser.add_argument("--resume", action="store_true", help="Skip infra cold-write component")
    parser.add_argument(
        "--handoff",
        choices=("auto", "zero", "paths-only", "inline"),
        default="auto",
        help="Handoff profile for prompt sizing",
    )
    parser.add_argument("--suggest-resume", action="store_true", help="Suggest a warm worker to resume")
    parser.add_argument("--type", dest="subagent_type", default=None, help="Filter resume by subagent_type")
    args = parser.parse_args(argv)

    file_paths = _parse_files_arg(args.files)

    if args.suggest_resume:
        suggestion = suggest_resume(
            subagent_type=args.subagent_type,
            files=file_paths or None,
            handoff=args.handoff,
        )
        if args.json:
            print(json.dumps(suggestion))
        else:
            print(json.dumps(suggestion, indent=2))
        return 0

    result = estimate_dispatch_context(
        task_prompt=args.prompt,
        file_paths=file_paths or None,
        model=args.model,
        resume=args.resume,
        handoff=args.handoff,
    )

    if args.json:
        print(json.dumps(result))
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
