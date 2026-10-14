---
name: dispatch-context-template
description: Reference for the _common.py/_model_utils.py exported API available to hook scripts (config access, state I/O, model/pricing helpers). Use when writing or editing a hook script that needs config_get, load_json_state, or model-tier/pricing helpers.
---

# Dispatch Context Template
<!-- version: 1.0 — matches scripts/_common.py -->

## _common.py Exported API

Key functions available to all hook scripts (import from _common or _model_utils):

### Config Access
- `config_get(path: str, default: Any = None, flat_key: str | None = None) -> Any` — Read nested config value with optional flat-key backward compatibility
- `config_int(path: str, default: int = 0, flat_key: str | None = None) -> int` — Read integer config value with type coercion
- `config_float(path: str, default: float = 0.0, flat_key: str | None = None) -> float` — Read float config value with type coercion
- `load_plugin_config() -> dict[str, Any]` — Load and cache configuration from config.json

### State I/O
- `load_json_state(path: Path) -> dict[str, Any]` — Read JSON state file; returns {} on missing/corrupt
- `save_json_state(path: Path, state: dict[str, Any]) -> None` — Atomically write JSON state (atomic rename, never raises)
- `append_event(record: dict[str, Any]) -> None` — Append usage-log record with deduplication lock; never raises
- `read_events() -> list[dict[str, Any]]` — Read all events from usage.jsonl; fail-open to []

### Session & Hashing
- `session_id_of(payload: dict[str, Any]) -> str` — Extract best-effort session identifier from payload or env
- `hash_target(value: str | None) -> str | None` — Truncated SHA256 hash for repeat detection
- `read_stdin_json() -> dict[str, Any]` — Read and parse hook JSON payload from stdin; never raises

### Time & Gate Results
- `now_iso() -> str` — Return ISO 8601 timestamp in UTC
- `format_ts(ts: str | None) -> str` — Render ISO timestamp as fixed-width UTC string
- `gate_allow(message: str | None = None) -> GateResult` — Build allow result with optional nudge
- `gate_deny(message: str) -> GateResult` — Build deny result with reason
- `emit_gate_result(result: GateResult, is_claude: bool) -> dict[str, Any]` — Convert GateResult to host-specific JSON

## Event Shapes

**Corrected against actual code.** This section previously described 5 types, two of which (`routing_verdict`, `gate_result`) do not exist anywhere in `scripts/`, and a third (`reward_signal`) had the wrong field names. `usage.jsonl` actually carries well over a dozen distinct `type` values across the various hooks; what follows is the subset that's been directly verified against the emitting code, not an exhaustive list. When in doubt, grep the script that emits the event you care about rather than trusting this file alone.

### intent (classify_intent.py)
```json
{
  "type": "intent",
  "session_id": "string",
  "intent": "qa_simple|qa_with_reads|qa_architecture|agentic_action",
  "tier": "fast|reasoning|unknown",
  "model": "string",
  "bytes": 1024
}
```
Note: no `ts` field is written here (unlike most other event types) and no `target_hash`/`direction`/`est_tokens` — those belong to the prompt/tool/response events `estimate_tokens.py` writes, not to `intent`.

### reward_signal (reward_signal.py, detect_revert.py)
Two call sites, two slightly different shapes — there is no `score`/`delta` field on either:
```json
{
  "type": "reward_signal",
  "session_id": "string",
  "signal": "under_route",
  "verdict_role": "FW",
  "escalated_to": "reasoning",
  "note": "fast-path verdict followed by reasoning-tier dispatch in same session"
}
```
```json
{
  "type": "reward_signal",
  "session_id": "string",
  "signal": "quality_failure",
  "command": "git checkout -- path/to/file (truncated to 200 chars)",
  "note": "git revert detected after edits in same session"
}
```
Separately, `reward_signal.py` also writes one entry per routing verdict to `data/reward_log.jsonl` (not `usage.jsonl`) with `rule_id`/`routing_role`/`actual_model`/`reward_value`/`timestamp`/`session_id` — that's the input to `confidence_score.py`, and it's a different file from the `usage.jsonl` events above.

### routing_declared (check_routing.py — NOT `routing_verdict`)
The actual per-turn routing-role event, written by both the `scan` and `gate` event handlers so later `PreToolUse` calls can look it up without re-scanning the transcript:
```json
{
  "type": "routing_declared",
  "session_id": "string",
  "turn_id": "string",
  "role": "DIRECT|FW|FO|RB|FV"
}
```
There is no `reason` or `model` field on this event, and no event type literally named `routing_verdict` exists in the codebase — the role/reason line the model emits in chat is called a "routing verdict" in prose, but what gets logged is `routing_declared`.

### session_summary (session_summary.py)
Much richer than a 4-field summary — includes (non-exhaustively) `est_input_tokens`, `est_output_tokens`, `est_context_tokens`, `est_cost_usd`, `subagent_dispatch_count`, `subagent_unattributed_count`, `tool_call_count`, `tier_mix`, `reward`, `has_real_tokens`, `session_type`, `explore_without_haiku`, `total_agents_spawned`, `agent_model_waste_usd`, plus `started_at`/`ended_at`. Read `summarize_session()` in `session_summary.py` directly for the current full field list rather than trusting a hardcoded example here — it has grown repeatedly and will likely grow again.

### There is no `gate_result` event type
Gate allow/deny decisions are returned directly as the hook's stdout JSON (`_allow`/`_deny` in `check_routing.py`, `emit_gate_result`/`gate_allow`/`gate_deny` in `_common.py`) — they are not separately logged to `usage.jsonl` as a `gate_result` event. What *is* logged to `usage.jsonl` around gate decisions are narrower, purpose-specific events like `gate_violation` (Explore-agent model-tier violations), `dispatch_grouping_violation`, and `routing_no_reason` — each with its own fields, not a generic `gate_name`/`decision` shape. Gate-level pass/fail *counters* (not events) live in `data/routing_state.json`'s `counters` object instead (e.g. `gate_allow_routed`, `gate_deny_no_verdict`, `gate_deny_dispatch_volume`, `fail_open` — see `_bump()` calls throughout `check_routing.py` for the current full set).

## Hook Chain Pattern

Stop hook chain order:
```
estimate_tokens
  ↓
check_routing --event scan
  ↓
reward_signal
  ↓
confidence_score
  ↓
auto_apply
  ↓
session_summary
  ↓
graph_gc
  ↓
compact_log
```

Each hook receives stdin payload with the session context from prior hooks.

## Test Monkeypatch Pattern

Standard isolation fixture used in all tests to prevent data directory pollution:

```python
@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(_common, "DATA_DIR", data_dir)
    monkeypatch.setattr(_common, "USAGE_LOG", data_dir / "usage.jsonl")
    monkeypatch.setattr(_common, "DISPATCH_REGISTRY_FILE", data_dir / "dispatch_registry.json")
    return data_dir
```

This fixture:
1. Creates a temporary data directory
2. Patches _common module-level path constants
3. Returns the temp dir for test assertions
4. Auto-teardown via pytest's tmp_path

## Usage Instructions for FO Dispatchers

Instead of embedding file contents inline in the dispatch prompt, cite this template:

```
FILES: skills/dispatch-context-template/SKILL.md, scripts/your_target.py
SCOPE: [task description]

Workers will read the template from disk on first use to avoid ~3,000 tokens
of repeated invariant context per additional dispatch worker.
```

This pattern eliminates redundant context in dispatch prompts while preserving
all necessary reference material for workers to understand the hook infrastructure.
