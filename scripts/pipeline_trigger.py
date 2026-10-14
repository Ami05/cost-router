#!/usr/bin/env python3
"""pipeline_trigger.py — Threshold-triggered feedback loop orchestrator.

Runs as a Stop hook. After each session, checks if enough shadow route data
has accumulated to warrant running the full feedback pipeline:

    shadow_evaluator → proposal_writer → policy_manager → policy_gc --apply

Trigger conditions (both must be true):
  1. Total shadow_routes.jsonl entries >= min_shadow_routes (config)
  2. New entries since last trigger >= cooldown_min_new_routes (config)

Fail-open: any failure is logged and silently swallowed. Never blocks session end.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (
    DATA_DIR,
    config_get,
    load_json_state,
    now_iso,
    pipeline_trigger_gc_apply,
    read_stdin_json,
    save_json_state,
)

SHADOW_ROUTES_FILE = DATA_DIR / "shadow_routes.jsonl"
TRIGGER_STATE_FILE = DATA_DIR / "pipeline_trigger_state.json"
SCRIPTS_DIR = Path(__file__).resolve().parent

_PIPELINE_STEPS = [
    "shadow_evaluator.py",
    "proposal_writer.py",
    "policy_manager.py",
    "policy_gc.py",
]


def _count_shadow_routes() -> int:
    """Return total line count of shadow_routes.jsonl. Returns 0 if file missing."""
    try:
        with SHADOW_ROUTES_FILE.open("r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _run_pipeline() -> str:
    """Run the feedback pipeline chain. Returns 'ok' or 'failed_at_<script>'."""
    cwd = SCRIPTS_DIR.parent
    for script in _PIPELINE_STEPS:
        args = ["python3", f"scripts/{script}"]
        if script == "policy_gc.py" and pipeline_trigger_gc_apply():
            args.append("--apply")
        try:
            result = subprocess.run(
                args,
                cwd=cwd,
                timeout=120,
                capture_output=True,
            )
            if result.returncode != 0:
                stem = script.replace(".py", "")
                return f"failed_at_{stem}"
        except Exception:  # noqa: BLE001
            stem = script.replace(".py", "")
            return f"failed_at_{stem}"
    return "ok"


def main() -> int:
    try:
        read_stdin_json()  # consume stdin (fail-open if absent)

        state = load_json_state(TRIGGER_STATE_FILE)
        total_routes = _count_shadow_routes()

        min_shadow_routes = config_get("pipeline_trigger.min_shadow_routes", 20)
        cooldown = config_get("pipeline_trigger.cooldown_min_new_routes", 10)

        routes_at_last = state.get("routes_at_last_trigger", 0)
        new_routes = total_routes - routes_at_last

        has_ever_triggered = bool(state.get("last_triggered_at"))
        should_trigger = (total_routes >= min_shadow_routes) and (new_routes >= cooldown)

        # Bootstrap: first run skips the cooldown check but still respects
        # the minimum-routes data-quality gate (min_shadow_routes).
        if not should_trigger and not has_ever_triggered and total_routes >= min_shadow_routes:
            should_trigger = True

        if not should_trigger:
            print("{}")
            return 0

        chain_result = _run_pipeline()

        save_json_state(
            TRIGGER_STATE_FILE,
            {
                "last_triggered_at": now_iso(),
                "routes_at_last_trigger": total_routes,
                "last_chain_result": chain_result,
            },
        )
    except Exception:  # noqa: BLE001
        pass

    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
