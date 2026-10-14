#!/usr/bin/env python3
"""cost-router Stop-hook coordinator: compact session data into scenario_library.json.

This module is the hook entry point only.  All rotation and trimming logic
lives in log_compactor.py.  This file:
  1. Reads stdin (session_id).
  2. Manages the checkpoint timestamp (for --checkpoint mode).
  3. Delegates session merge + usage-log compaction to log_compactor.compact_logs().
  4. Sequences policy_manager, shadow_evaluator, proposal_writer, policy_gc calls.
  5. Emits the required JSON response for Claude Code.

Usage:
    python3 compact_log.py                  # Stop hook: full session merge
    python3 compact_log.py --checkpoint     # UserPromptSubmit: partial window
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    USAGE_LOG,  # noqa: F401
    USAGE_ARCHIVE,  # noqa: F401
    now_iso,
    policy_auto_apply_enabled,
    read_stdin_json,
    save_json_state,
    session_id_of,
    _warn,
)
from log_compactor import (  # noqa: E402
    compact_logs,
    compact_session,
    compact_usage_log,
    load_library,
    WORKING_SET_BYTES,
    ARCHIVE_ROTATE_BYTES,
)

CHECKPOINT_STATE_FILE = DATA_DIR / "checkpoint_state.json"


# ---------------------------------------------------------------------------
# Checkpoint state I/O
# ---------------------------------------------------------------------------


def _load_checkpoint_ts() -> str | None:
    """Return the last checkpoint timestamp from checkpoint_state.json, or None."""
    try:
        state = json.loads(CHECKPOINT_STATE_FILE.read_text(encoding="utf-8"))
        return state.get("last_checkpoint_ts")
    except (OSError, json.JSONDecodeError):
        return None


def _save_checkpoint_ts(ts: str) -> None:
    """Persist *ts* as the new last checkpoint timestamp. Fails open (PROD-8).

    Uses save_json_state for atomic write (temp file + os.replace) to prevent
    corruption if the process dies mid-write.
    """
    save_json_state(CHECKPOINT_STATE_FILE, {"last_checkpoint_ts": ts})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compact session data into bounded scenario_library.json."
    )
    parser.add_argument(
        "--checkpoint",
        action="store_true",
        help="UserPromptSubmit mode: compact a partial window rather than a full session.",
    )
    args = parser.parse_args()

    import policy_manager  # noqa: PLC0415 — outside try to surface ImportError

    try:
        payload = read_stdin_json()
        session_id = session_id_of(payload)

        window_start: str | None = None
        if args.checkpoint:
            window_start = _load_checkpoint_ts()
            _save_checkpoint_ts(now_iso())

        # Delegate compaction to log_compactor; usage-log compaction is
        # included automatically when window_start is None (full Stop run).
        compact_logs(session_id, window_start=window_start)

        # Evaluate routing proposals after updating scenario library.
        import shadow_evaluator  # noqa: PLC0415
        import proposal_writer   # noqa: PLC0415
        import policy_gc         # noqa: PLC0415

        try:
            # Auto-apply is opt-in (policy.auto_apply_enabled, default false).
            if policy_auto_apply_enabled():
                policy_manager.evaluate_proposals()
        except (OSError, ValueError, KeyError):
            pass

        try:
            shadow_evaluator.main()
        except (OSError, ValueError, KeyError):
            pass
        try:
            proposal_writer.main()
        except (OSError, ValueError, KeyError):
            pass

        try:
            report = policy_gc.generate_report()
            if "proposed for retirement" in report or "OVER CAP" in report:
                print("[cost-router GC]", file=__import__("sys").stderr)
        except (OSError, ValueError, KeyError) as e:
            _warn("policy_gc_failed", {"file": "compact_log.py", "error": str(e)})

        if args.checkpoint:
            try:
                library = load_library()
                recent: list[dict] = []
                for sessions in library.values():
                    recent.extend(sessions)
                recent.sort(
                    key=lambda s: s.get("summary", {}).get("started_at") or "",
                    reverse=True,
                )
                recent = recent[:10]
                policy_manager.check_rollback(recent)
            except (OSError, ValueError, KeyError):
                pass

        # PROD-8: --checkpoint fires on UserPromptSubmit/beforeSubmitPrompt,
        # a bare {} was this script's own outlier on that event.
        print(json.dumps({"continue": True} if args.checkpoint else {}))
    except Exception:  # noqa: BLE001 — hook must fail open
        print(json.dumps({"continue": True} if args.checkpoint else {}))

    # Clean up warnings-alerted sentinel so next session alerts fresh.
    try:
        (DATA_DIR / ".warnings_alerted").unlink(missing_ok=True)
    except OSError:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
