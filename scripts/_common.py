#!/usr/bin/env python3
"""Backward-compatibility shim — re-exports every symbol from the domain modules.

Direct imports from _common still work; new code should import from the
specific module (_config, _io, _events, _gate, _session).
"""
from __future__ import annotations

from _config import *  # noqa: F401, F403
from _config import _warn, _config_cache, DATA_DIR, _invalidate_config_cache, _WARNED_THIS_PROCESS, _load_config_from_disk  # noqa: F401
from _session import *  # noqa: F401, F403
from _fileio import *  # noqa: F401, F403
from _fileio import _debug_log  # noqa: F401 — underscore names skipped by *
from _events import *  # noqa: F401, F403
from _gate import *  # noqa: F401, F403
from _model_utils import (  # noqa: E402, F401
    FAST_MARKERS,
    PRICING_FILE,
    corrected_estimate_tokens,
    est_cold_write_cost_usd,
    estimate_tokens,
    infra_tokens,
    load_pricing,
    model_context_window,
    model_of,
    price_for,
    tier_of_model,
    token_correction_factor,
)
