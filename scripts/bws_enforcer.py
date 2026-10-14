#!/usr/bin/env python3
"""cost-router: BWS (Bounded Wave Scheduler) enforcement.

Extracted from agent_call_tracker.py. Owns wave-barrier logic, file-range
disjointness checks, concurrency-cap enforcement, warm-worker lookups, and
frontload-nudge tracking.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    DISPATCH_REGISTRY_FILE,
    load_json_state,
    session_id_of,
)
from session_state import ROUTING_STATE_FILE  # noqa: E402
from affinity import (  # noqa: E402
    affinity_score,
    get_affinity_threshold,
)

# DISPATCH-002: how soon after going idle a first resume must land to count as an
# unforeseen-follow-up rather than a deliberate later re-engagement.
_FRONTLOAD_NUDGE_WINDOW_SECONDS = 120


def _get_work_unit_intent(session_id: str) -> str:
    """Return work_unit_intent for *session_id* from routing_state.json.

    Returns the intent string if found, empty string otherwise.
    Handles missing file, malformed JSON, and missing session entry gracefully.
    """
    try:
        state = load_json_state(ROUTING_STATE_FILE)
        sessions = state.get("sessions") if isinstance(state, dict) else None
        if not isinstance(sessions, dict):
            return ""
        entry = sessions.get(session_id)
        if not isinstance(entry, dict):
            return ""
        return entry.get("work_unit_intent") or ""
    except Exception:  # noqa: BLE001
        return ""


def get_active_file_worker_map(session_id: str) -> dict[str, str]:
    """Return a mapping of file_path -> worker_agent_id for all idle, within-TTL
    workers belonging to *session_id*.

    Reads dispatch_registry.json, filters to workers that are:
    - in the given session
    - status == "idle"
    - within subagent_ttl_seconds (checked via warm_until or finished_at + ttl)

    Returns an empty dict if the registry is missing, malformed, or the session
    has no qualifying workers.  This is a read-only helper — no state mutation.
    """
    try:
        registry = load_json_state(DISPATCH_REGISTRY_FILE)
    except Exception:  # noqa: BLE001
        return {}

    sessions = registry.get("sessions") if isinstance(registry, dict) else None
    if not isinstance(sessions, dict):
        return {}

    session_data = sessions.get(session_id)
    if not isinstance(session_data, dict):
        return {}

    agents = session_data.get("agents")
    if not isinstance(agents, dict):
        return {}

    # TTL from config — fall back to 1800 s if unavailable
    from _common import SUBAGENT_TTL_SECONDS as _ttl  # noqa: PLC0415
    try:
        ttl_seconds: int = int(_ttl)
    except (ValueError, TypeError):
        ttl_seconds = 1800

    now_ts = datetime.now(tz=timezone.utc).timestamp()
    file_worker_map: dict[str, str] = {}

    for agent_id, worker in agents.items():
        if not isinstance(worker, dict):
            continue
        if worker.get("status") != "idle":
            continue

        # Determine expiry from warm_until, then finished_at + ttl, then skip
        within_ttl = False
        warm_until_raw = worker.get("warm_until")
        if warm_until_raw is not None:
            try:
                warm_ts = datetime.fromisoformat(str(warm_until_raw)).timestamp()
                within_ttl = now_ts <= warm_ts
            except (ValueError, TypeError):
                within_ttl = False
        else:
            finished_raw = worker.get("finished_at")
            if finished_raw is not None:
                try:
                    finished_ts = datetime.fromisoformat(str(finished_raw)).timestamp()
                    within_ttl = now_ts <= finished_ts + ttl_seconds
                except (ValueError, TypeError):
                    within_ttl = False

        if not within_ttl:
            continue

        touched = worker.get("files_touched") or worker.get("files") or []
        for file_path in touched:
            if isinstance(file_path, str) and file_path:
                # First writer wins — don't overwrite an existing mapping
                file_worker_map.setdefault(file_path, agent_id)

    return file_worker_map


def _first_resume_of(agents: list[Any], resume_id: str) -> bool:
    """Return True if *resume_id* has no prior resume entry in this session's agent log."""
    return not any(
        isinstance(a, dict) and a.get("resume") == resume_id for a in agents
    )


def _seconds_since_worker_finished(session_id: str, agent_id: str) -> float | None:
    """Return elapsed seconds since *agent_id* last went idle, or None if unknown."""
    try:
        registry = load_json_state(DISPATCH_REGISTRY_FILE)
        worker = registry["sessions"][session_id]["agents"][agent_id]
        finished_raw = worker.get("finished_at")
        if not finished_raw:
            return None
        finished_ts = datetime.fromisoformat(str(finished_raw)).timestamp()
        return datetime.now(tz=timezone.utc).timestamp() - finished_ts
    except Exception:  # noqa: BLE001
        return None


def get_worker_affinity_clusters(
    session_id: str,
) -> dict[int, list[tuple[str, list[str]]]]:
    """Group idle workers in a session by affinity of their touched files.

    Returns a dict mapping cluster_id (int) to list of (worker_agent_id, file_list)
    tuples. Workers with high affinity (score >= threshold) are grouped together.

    Uses get_active_file_worker_map and get_affinity_threshold.
    Deterministic: same session state produces same clusters across calls.
    """
    file_worker_map = get_active_file_worker_map(session_id)
    if not file_worker_map:
        return {}

    # Build unique worker -> file list mapping
    worker_files: dict[str, set[str]] = {}
    for file_path, worker_id in file_worker_map.items():
        if worker_id not in worker_files:
            worker_files[worker_id] = set()
        worker_files[worker_id].add(file_path)

    # Convert to list of (worker_id, file_list) for clustering
    workers = [(w_id, sorted(files)) for w_id, files in worker_files.items()]
    if not workers:
        return {}

    threshold = get_affinity_threshold()
    clusters: dict[int, list[tuple[str, list[str]]]] = {}
    worker_to_cluster: dict[str, int] = {}
    next_cluster_id = 0

    for i, (worker_i, files_i) in enumerate(workers):
        if worker_i in worker_to_cluster:
            continue

        # Start a new cluster
        cluster_id = next_cluster_id
        next_cluster_id += 1
        clusters[cluster_id] = [(worker_i, files_i)]
        worker_to_cluster[worker_i] = cluster_id

        # Build unit dicts for affinity scoring
        unit_i = {"files": files_i}

        # Greedily add subsequent workers with high affinity
        for j in range(i + 1, len(workers)):
            worker_j, files_j = workers[j]
            if worker_j in worker_to_cluster:
                continue
            unit_j = {"files": files_j}
            score = affinity_score(unit_i, unit_j)
            if score >= threshold:
                clusters[cluster_id].append((worker_j, files_j))
                worker_to_cluster[worker_j] = cluster_id

    return clusters
