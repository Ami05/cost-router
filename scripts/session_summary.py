#!/usr/bin/env python3
"""Re-export shim — kept for backward compatibility.

Hook ingestion:  session_hook.py
Report renderer: cost_report_renderer.py

Any script that imported from session_summary directly will continue to work.
"""

from __future__ import annotations

from session_hook import (  # noqa: F401
    _derive_session_type,
    _read_project_usage,
    _rollup_daily_signal,
    _upsert_project_registry,
    main,
    session_time_bounds,
    summarize_session,
)
from cost_report_renderer import (  # noqa: F401
    _render_receipt,
    _render_warnings,
    read_signal_stats,
    render_report,
    render_shadow_eval_stats,
    render_signal_stats,
)
from _config import DATA_DIR, USAGE_LOG, WARNINGS_LOG  # noqa: F401
from _events import read_events  # noqa: F401
from _fileio import load_json_state, save_json_state  # noqa: F401
from _model_utils import load_pricing, price_for  # noqa: F401
from _session import now_iso  # noqa: F401


def main() -> int:
    """Entry point for session_summary script with --warnings flag support."""
    import sys
    from session_hook import main as session_hook_main

    if "--warnings" in sys.argv:
        print(_render_warnings())
        return 0
    return session_hook_main()


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
