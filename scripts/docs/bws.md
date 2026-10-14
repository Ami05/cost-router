## Bounded Wave Scheduler (BWS)

**Rule Scope Declaration — Mandatory (TICKET-042).** Every new BWS/cost/visibility rule must state, at the point it is introduced, which side it binds: main session, subagent, or both. Absent an explicit statement, the rule **defaults to subagent-inclusive** (binds both main session and subagent). This exists to prevent a repeat of the shell-gate asymmetry (TICKET-013): cost/visibility rules were silently main-session-only while RC-3's wave barrier was explicitly extended to subagents, with no consistent policy governing which category applied by default. This is a documentation/process policy, not a code-enforced gate.

- **Concurrency cap.** At most `concurrency.max_inflight_subagents` (currently `3`) subagents may be in flight at once. A dispatch that would exceed the cap is **denied** with an instruction to queue the unit — never silently serialized or dropped.
- **File range disjointness.** `files[]` entries use `path:start,end` ranges. Disjoint ranges within the same file are eligible to run concurrently. A bare path with no range is **fail-closed**: treated as the whole file, and serializes against any other unit touching that file.
- **Superstep barrier (BWS-T2-002).** Wave N+1 never dispatches until every wave N unit has returned. Verification units run only after the barrier, as a single shell-worker dispatch. When `concurrency.serialize_verify` is `true` (currently `true`) and any subagent is still active, a verification-shaped dispatch is **denied** until the wave fully drains.
- **Barrier check is subagent-inclusive (RC-3).** `scripts/check_routing.py` (~line 3100, "RC-3: Task/Agent dispatches from subagents must check wave barrier") extends this check to subagents — a subagent fanning out its own Task/Agent dispatches is held to the same wave barrier as the orchestrator, not exempt. Contrast with the shell-gate above, which is explicitly main-session-only by design.
- **Progress markers / stall counter (BWS-T2-003).** Workers must emit a `done:` / `completed:` / `remaining:` / `progress:` line per finished sub-unit. Consecutive calls with no marker increment a stall counter; at `progress_ledger.max_stall_markers` misses (currently `6`) the gate **denies** and tells the worker to return a partial report so the orchestrator can resume or replan. A fresh dispatch or a resume clears the counter. A `null`/absent config value disables stall detection entirely.
- **Wall-clock stall check (TICKET-037).** `progress_ledger.max_stall_seconds` (currently `180`) denies independently of tool-call count once that many seconds have elapsed since the last progress marker — this is what actually binds orchestrator workers on design/refactor steps to the "one marker per 2-3 minutes" cadence, since those steps can go long stretches of wall time between tool calls and would never trip the count-based check alone. Either the count-based or time-based threshold tripping triggers the same partial-report deny; both reset together on a marker, fresh dispatch, or resume. A `null`/absent value disables the time-based check only.
- **Stall resume stays inside the TTL.** Any stall-triggered resume must land inside the existing `resume_ttl_seconds` (300s) constraint above — never raise it to accommodate a stalled worker. Replan fresh instead.
- **Unit-size floor (BWS-T2-001) is protocol-only.** No wave object exists in code to gate on this, so there is nothing to enforce automatically — use judgment (or `invariant_ratio`) to merge trivial units before fan-out manually. A `null` value for this setting, like for `max_stall_markers`, means "not enforced by design," not "misconfigured."

## DGC-104 Stall Kill and DGC-103 Strict Profile

**Strict stall profile (DGC-103).** `progress_ledger.strict_profile.<worker>` defines per-worker heartbeat thresholds: `max_stall_markers` (e.g. 3 misses), `max_stall_seconds` (e.g. 90s), and `heartbeat_seconds` (e.g. 30s). A miss is counted each time elapsed time since the last progress marker exceeds `heartbeat_seconds`. When `miss_count` reaches `max_stall_markers` or elapsed reaches `max_stall_seconds`, the gate denies (enforce mode) or warns only (warn mode), as controlled by `progress_ledger.mode`. The stall-kill escalation (DGC-104) then begins: `checkpoint_requested_ts` is set, and each tool call increments `grace_denied_calls`. After `progress_ledger.kill.grace_denied_calls` denials (default 3), all tool calls are denied with `stall_killed=True`. A progress marker in the next assistant text clears `checkpoint_requested_ts`, `stall_killed`, and the miss/stall counters.

## DGC-201 Context-Affinity Scoring for Task Clustering

**Affinity threshold (DGC-201).** `dispatch_context.affinity.threshold` (default `0.3`) determines whether two task units should be grouped together in a dispatch batch. The affinity score combines two signals:

1. **Jaccard similarity** (70% weight): file-set overlap. `|A ∩ B| / |A ∪ B|`. Units with identical files score 1.0; disjoint files score 0.0.
2. **Shared directory prefix** (30% weight): normalized common directory depth. Units in the same directory (e.g., both in `scripts/`) score higher than units in unrelated directories (e.g., `scripts/` vs `tests/`).

**Score calculation.** `affinity_score(unit_a, unit_b) = 0.7 * jaccard + 0.3 * shared_prefix`, clamped to [0, 1]. The predicate `is_high_affinity(a, b)` returns `True` if `affinity_score >= threshold`.

**Clustering.** Functions `get_pending_dispatch_clusters()` (in `dispatch_budget_gate.py`) and `get_worker_affinity_clusters()` (in `bws_enforcer.py`) partition units/workers into clusters using a greedy algorithm: each unit joins a cluster if affinity with that cluster's seed unit is at or above the threshold, preserving grouping decisions for related work.

**Measurable criterion.** Threshold justification: units with affinity >= 0.3 exhibit >= 30% file-set overlap and/or meaningful shared directory structure. This threshold is calibrated to avoid splitting tasks that legitimately share context (e.g., `scripts/dispatch/foo.py` and `scripts/dispatch/bar.py` both in `dispatch/` subdir) while still allowing splits at low-overlap boundaries (e.g., `scripts/` vs `tests/`).

**Determinism.** Scoring is deterministic: identical units always produce the same score across calls, independent of file order. The clustering algorithm is stable: the same unit list produces the same cluster assignments.

## DGC-204 Advisory Split Planner

**Split planner (DGC-204).** When dispatch token estimates exceed `hard_limit` (180K), the budget gate calls `plan_split_for_dispatch()` to partition the work. The planner:

1. **Extracts units:** Parses the dispatch prompt, groups file paths by common directory prefix, and estimates tokens per group.
2. **Partitions units:** Uses a greedy algorithm respecting affinity edges: units with affinity >= threshold (from DGC-201) are kept together unless a single unit exceeds `target_max` (0.9 × hard_limit, 162K at default). Natural boundaries (directory depth, low-affinity edges) guide splits.
3. **Returns groups:** Each group fits within `target_max` tokens. Unsplittable units (single unit > limit) are flagged and included anyway.
4. **Integrates into denial:** The split plan text is embedded in the hard-deny message above 180K, advising the user to dispatch the groups sequentially or batch related groups together.

**Result:** A deterministic, human-readable split plan that preserves grouping of related work while respecting token limits.

## DGC-206 Manifest-Driven Redispatch and Loop Guard

**Manifest redispatch (DGC-206).** When a subagent returns a PARTIAL status:

1. **Stop hook injects reminder:** `event_subagent_stop()` parses the continuation manifest from the subagent's final report, extracts the `remaining` items, and builds a compact reminder for the orchestrator. The reminder includes depth, remaining unit count, and compacted goal/file metadata (≤200 chars).
2. **Manifest validation:** When a new Task is submitted, the hook (via `_validate_task_manifest_lineage()`) checks that the Task's text references a remaining unit id from the manifest. This prevents accidental dispatch of unrelated work.
3. **Orchestrator redispatch:** The orchestrator reads the compact continuation context and redispatches each remaining unit as a new Task, incrementing the `depth` field for each. Hooks cannot enqueue dispatches directly; only the orchestrator fan-out creates new pending work.

**Loop guard (DGC-206).** The loop guard (`_check_continuation_loop_guard()`) denies new Tasks when:
- `depth >= continuation.max_depth` (default 3): prevents infinite escalation.
- `no_progress_halts` is true and manifest items are identical to the previous dispatch: signals stalled redispatch cycle.

Both conditions return a denial reason; the user must acknowledge (e.g., by explicitly resuming or manually breaking the cycle) before the next redispatch is allowed.

