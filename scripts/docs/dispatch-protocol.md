## Dispatch Protocol — JSON Schema Format

Structured dispatches at or above 200 tokens must be valid JSON matching one of 4 worker schemas: mutation, read, create, or shell. The gate hard-blocks prompts containing code blocks, failing JSON parse, or exceeding the per-worker token ceiling — and returns the schema template in the error.

**Thin dispatch exemption:** prompts < 200 tokens pass freely (e.g. `"Run: pytest tests/ -x -q"`), including plain text — JSON validation is skipped.

**Token ceilings** (`len(prompt) // 4`, enforced by `scripts/validate_dispatch_schema.py`): mutation 560 (650 when `examples` is present), read 650, create/shell 560.

**`constraints` closed vocabulary:** every word in each `constraints` item must appear in the allowlist at `config/constraint_allowlist.json` (case-insensitive; hyphens/spaces are token separators), in addition to the ≤5 items / ≤60 chars / `[a-zA-Z0-9 -,.]`-only rules below. A word outside the allowlist hard-blocks the dispatch (`schema_stuffing`, sub_reason `allowlist`) even if it otherwise fits the char/length rules.

### mutation worker
```json
{
  "worker": "mutation",
  "intent": "<one-phrase task description>",
  "files": ["scripts/path.py:line", "scripts/other.py:start,end"],
  "pattern": "<1-2 sentences: what transformation to apply>",
  "where": "<function or block name>",
  "why": "<intent and behavioral reasoning>",
  "constraints": ["<hard constraint>"],
  "examples": [{"before": "current code snippet", "after": "desired code snippet"}],
  "output": "targeted_edits"
}
```
Fields: `intent` (required), `files` (required, path:line format), `pattern` (required, ≤200 chars), `where` (optional, ≤170 chars, human-readable location), `why` (optional, ≤150 chars, intent reasoning), `constraints` (optional, max 5 items, each ≤60 chars), `examples` (optional, max 3 `{before, after}` pairs; each string ≤400 chars, no backticks; raises token ceiling to 650), `output` (required: targeted_edits | diff | summary)

**Progress markers required.** Mutation workers emit a `done:` / `completed:` / `remaining:` / `progress:` line at least once every 2-3 minutes of wall time, per the cadence in the orchestrator section below. The marker is worker output, not a payload field — it adds nothing to the token ceiling. Enforced by `_subagent_progress_gate` (`progress_ledger.max_stall_seconds`, with mutation/read overrides).

**When mutation is inappropriate:** if the transformation isn't already known — the task needs the worker to diagnose the cause before editing — don't use `mutation`. Its `pattern`/`where`/`why` fields presume the fix is already decided and have no room for open-ended investigation; forcing diagnosis into them either overflows the 200-char/token ceiling or gets silently skipped, and the worker guesses at a fix. Use `orchestrator` instead (`step` ≤1600 chars, e.g. "diagnose X, then apply the fix").

### read worker
```json
{
  "worker": "read",
  "goal": "<what to find or understand>",
  "scope": ["scripts/"],
  "return": ["function signatures", "file:line references"]
}
```
Fields: `goal` (required), `scope` (required), `return` (required), `max_files` (optional, default 10)

**Progress markers required.** Read workers emit a `done:` / `completed:` / `remaining:` / `progress:` line at least once every 2-3 minutes of wall time, per the cadence in the orchestrator section below. The marker is worker output, not a payload field — it adds nothing to the token ceiling. Enforced by `_subagent_progress_gate` (`progress_ledger.max_stall_seconds`, with mutation/read overrides).

### create worker
```json
{
  "worker": "create",
  "intent": "<what file to create and why>",
  "file_path": "scripts/new_module.py",
  "template_ref": "python_module",
  "fields": {"description": "Brief module purpose", "author": "optional"}
}
```
Fields: `intent` (required), `file_path` (required), `template_ref` (optional, e.g. "python_module", "json_config"), `fields` (optional, key-value pairs for template substitution), `constraints` (optional, max 5 items, each ≤60 chars)

### shell worker
```json
{
  "worker": "shell",
  "intent": "<what command accomplishes and why>",
  "command": "pytest tests/e2e/ -x -q",
  "expect": "exit_code"
}
```
Fields: `intent` (required), `command` (required), `expect` (required: exit_code | stdout | stderr | all), `constraints` (optional, max 5 items, each ≤60 chars)

### orchestrator worker
```json
{
  "worker": "orchestrator",
  "intent": "<one-phrase overall goal>",
  "step": "<single-phase description of implementation work. max_phases_per_dispatch: 1.>",
  "model": "sonnet"
}
```
Fields: `intent` (required), `step` (required, ≤1600 chars, single-phase implementation description), `model` (optional, default sonnet), `constraints` (optional, max 5 items, each ≤60 chars)

**Token ceiling: 1200** (vs 560 for atomic workers — FO dispatch allows space for complex single-phase work).

Use this for complex single-phase orchestration. The receiving sonnet agent executes the work unit directly or fans out subordinate workers as needed.

**Progress markers required (TICKET-037).** Orchestrator workers executing design or refactor steps must emit a `done:` / `completed:` / `remaining:` / `progress:` line at least once every 2-3 minutes of wall time — the same cadence shell workers follow under BWS-T2-003 below. This matters more for orchestrator steps than for shell/mutation workers: design/refactor work makes far fewer tool calls per minute, so the call-count stall check alone can go a long time without tripping. `progress_ledger.max_stall_seconds` (default `180`) enforces this independent of tool-call count — see BWS-T2-003.

### BAD vs GOOD

BAD (blocked — code block + prose):
```
Read scripts/gates.py. At line 49 you'll find:
```python
try:
    from classify_intent import ...
```
Move the import outside the try block.
```

GOOD (passes gate):
```json
{"worker":"mutation","intent":"fix lazy import in gates.py","files":["scripts/gates.py:49"],"pattern":"Move import outside try block. Narrow except Exception to specific types.","output":"targeted_edits"}
```

## Continuation Manifest (DGC-105)

All workers must include a fenced `continuation-manifest` JSON block in the final report when resuming chained work. This block reports progress and pending items for orchestrator resumption.

**Block format** (in worker final report):
```continuation-manifest
{
  "status": "COMPLETE|PARTIAL|UNKNOWN",
  "done": ["item-id-1", "item-id-2"],
  "remaining": [{"id": "item-id-3", "goal": "...", "files": ["..."]}],
  "tokens_used": 45000,
  "phase": "implementation",
  "depth": 0
}
```

**Schema fields:**
- `status` (required): COMPLETE (all work done), PARTIAL (work remains, manifest included), UNKNOWN (no manifest, worker will be asked again).
- `remaining` (required if PARTIAL): list of work items, max 10 items (per `continuation.max_remaining_items`). Compacted by parse_manifest to stay under 2000 chars (per `continuation.max_compact_chars`).
- `depth` (optional): integer incremented on each resume; max 3 per `continuation.max_depth`. Prevents infinite loop escalation.
- `done`, `tokens_used`, `phase`: optional tracking fields.

**Depth field (DGC-206):** All dispatch schemas (mutation, read, create, shell, orchestrator) accept an optional `"depth": <integer>` field. When present:
- The field is incremented on each resume of a chained work unit.
- It is included in the telemetry row (`subagent_stop_telemetry`) for tracking continuation depth.
- On resume, it is passed through to the worker's `compact_continuation` context so the worker knows how many times it has been resumed.
- Loop guard checks `depth >= continuation.max_depth` (default 3) and denies further resumption until the user acknowledges.

**Hook limitations (DGC-206, DGC-105):** Hooks invoked during dispatch lifecycle (e.g., `event_subagent_stop`, manifest validation) operate within script scope and may not:
- Spawn new agents or Tasks directly. The orchestrator is responsible for reading manifest state and redispatching remaining work.
- Call SubagentStop or TaskStop. The hook may only return advisory output and context for the main session.
- Mutate dispatch payloads beyond adding telemetry fields. Manifest injection and validation are advisory; the payload must be complete before the hook runs.

In practice, hooks validate manifest lineage, parse PARTIAL status, and build context reminders; the orchestrator decides whether and how to redispatch based on that context.
