## Hard Constraints

- **No Anthropic API key in solutions** — eval frameworks, tests, and scripts must never require an `ANTHROPIC_API_KEY` at runtime. Pre-generate and commit any Claude-generated artifacts (e.g. `scenarios.json`) so CI and local runs are fully offline.

## Environment

- **Development:** `~/cost-router` — all code changes, scripts, config, and tests happen here.
- **Production (plugin):** `~/.claude/plugins/local/cost-router` — the deployed copy. All runtime data files (`reward_log.jsonl`, `routing_events.jsonl`, `routing_state.json`, `gate_counters.json`, etc.) accumulate here.
- **Workflow:** make changes in dev → the user resyncs to production manually. Never run or dispatch a resync, and never edit production files directly.
- **Rule:** when reading data files for investigation or analysis, always use the production path. When reading or editing scripts/config/tests, always use the dev path.

## Rulings Ledger

Non-trivial routing decisions require a Ruling log line:

```
Ruling: <decision> — <why> — <cost if wrong>
```

Non-trivial: model override, BLOCKED resolution, ambiguity ruling, task split/merge. Trivial (no Ruling): mechanical dispatch, standard DONE → next task, Explore agent.

## Doc File Convention

Any `.md` file that is referenced by plugin code (scripts, hooks, gates) **must** live in `scripts/docs/` — not in `knowledge/` or project root. The directories `scripts/`, `.claude/rules/` and `skills/` are all synced to the deployed plugin; `knowledge/` is never synced and never read by code.

- `knowledge/` — plans, tickets, design notes. Never read by code. Not synced.
- `.claude/rules/` — routing rule files. Synced, but not for inline code imports.
- `skills/` — agent-invocable skill files. Synced, but not for inline code imports.
- `scripts/docs/` — reference `.md` files that scripts or gates read at runtime. **Synced.**

## Bulk Verification — P0 Rule

**NEVER ask the user to run tests or shell commands manually.** When a hook blocks inline Bash (e.g. bulk-verify gate), ALWAYS dispatch a subagent with Bash access to run the command. The hook message offers this path: `bulk-verify: subagent`. Only use `bulk-verify: user-local` if the user explicitly says they want to run it themselves.

"Blocked inline" does NOT mean "blocked entirely" — read the FULL hook message for alternative execution paths before responding.

**Why dispatch resolves the block:** the shell-gate counter (`_check_shell_gate()` in `scripts/routing_resolver.py` / `scripts/check_routing.py`) is scoped to the main session only, by design (docstring: "not subagent context") — it never counts a dispatched subagent's Bash calls. Dispatching isn't a workaround around the gate, it's the intended exit from it.

**Bulk-verification/test-runner subagents must not fix code.** A subagent dispatched to run tests may read/explore to diagnose a failure, but must not use `Edit`/`Write`/`StrReplace` to patch code and then report the run as passing. Report the failure and diagnosis back to the orchestrator instead. Use the JSON `{"worker":"shell",...}` dispatch schema — it restricts the agent to `Bash`-only and prevents silent code edits.

## Subagent Model Override — Mandatory

Every `Agent(...)` call MUST include an explicit `model` parameter. Never let workers inherit the session model.

> The canonical routing matrix lives in `config.json` under `routing_matrix`. This table is a human-readable summary — update `config.json` to change routing behavior.

| Task type | Model |
|---|---|
| Explore/search | `model: "haiku"` |
| Mechanical worker (config.json `mechanical` intent) — a decided transformation that still needs pattern-matching/location judgment (FW = fast tier = haiku, per the Role Legend; escalate to `sonnet` only when a mechanical unit fails or needs location judgment) | `model: "haiku"` |
| Delete files / bulk-edit worker (config.json `bulk-edit` intent) — pure deletion/move, zero interpretation | `model: "haiku"` |
| Known-pattern find-replace across many scattered/non-contiguous sites (pattern already decided, only location count is high) — classify as `bulk-edit`, not `mechanical`, even within a single file | `model: "haiku"` |
| Mixed-action task bundled into one dispatch, where a minority of steps (e.g. 1-2 of 7) are the actual reasoning/judgment point and the rest are exploratory reads or mechanical edits — classify by the dominant action mix, not the task's overall label | split: `model: "haiku"` worker for the exploratory/mechanical majority, `model: "sonnet"` only for the reasoning step(s) (inline by the orchestrator, or a small chained/`resume`d sonnet dispatch) — never fold the whole bundle into one `sonnet` worker just because one step needs judgment |
| Deep reasoning, architecture / ambiguity | inline on the main session. Separable research synthesis, dependency analysis or contained debugging may go to a `model: "sonnet"` subagent with a written brief — **never** `model: "opus"` on subagents |
| All other task types (mechanical orchestrator / additive / debug / ambiguous, run tests, generate reports/docs, structured output, etc.) | mirrors `config.json` `routing_matrix` default and per-intent entries — see `config.json` for current values, not this table |

The `enforce_explicit_model` gate (pre_task) will DENY any Agent/Task dispatch without an explicit model, and permanently DENY `model: "opus"` on subagent dispatches (no config toggle, no prompt override). If denied, add the appropriate model parameter and retry.

*(Note: the mixed-action row above is a classification judgment call at dispatch time, not a code-enforced gate — `routing_matrix` has no per-step granularity. The `enforce_explicit_model` gate still checks the resulting `model` param on whichever dispatch(es) you end up issuing, but it can't detect that a bundle should have been split in the first place.)*

## Dispatch Protocol — JSON Schema Format

Full spec (worker schemas, token ceilings, BAD vs GOOD) lives in `scripts/docs/dispatch-protocol.md`; read it before any structured dispatch of 200+ tokens.
Payloads must be valid JSON (mutation, read, create, shell, orchestrator); `constraints` words must appear in `config/constraint_allowlist.json`.
Enforced by `scripts/validate_dispatch_schema.py`; thin dispatches under 200 tokens are exempt.

## Cost Optimization — Orchestration Patterns

Scope: "Subagents are cheap", "Compact early", "Explore once, implement once" and "Structured output belongs on sonnet subagents" bind the main session only; every other bullet here binds subagents too.

- **Subagents are cheap, orchestration turns are expensive.** Each main-session turn reloads full context (~$0.20-0.30). Subagents start fresh with small context.
- **Chain sequential-but-mechanical steps in one worker — except test/verification runs.** If steps don't need orchestrator judgment between them (e.g., delete files → edit references), put them all in one agent prompt instead of bouncing back for each step. Never chain a test/lint/verification run into a sonnet mutation or design worker's prompt — split it into its own haiku shell-worker dispatch instead.
- **Write complete test/harness scripts before spawning.** Enumerate all variants upfront and validate assumptions against the real source before dispatching — not after seeing wrong results.
- **Front-load probes into the original dispatch.** Include test/probe instructions in the initial prompt, not as a follow-up resume that adds a round-trip.
- **Batch zero-worker-type checks into existing work.** Fold checks that need no specific worker type into whichever harness is running instead of spawning separately.
- **Parallel is for independent work, sequential chaining is for dependent work.** If step C depends on A+B completing: run A and B in parallel, then chain C into whichever finishes last — don't return to orchestrator just to dispatch C.
- **Compact early.** When context hits 150%+, the per-turn cost inflates. Run `/compact` at the first warning, not after multiple turns.
- **Explore once, implement once.** Do all file exploration in a single Haiku agent before planning. Don't launch second explore rounds during implementation.
- **Do not re-narrate subagent results.** After receiving any subagent result, output ≤3 sentences or a `Key findings:` bullet list. Do NOT re-render tables, summaries, or reports the agent already produced.
  - BAD: Re-writing the agent's full table in your own words.
  - GOOD: `Key findings: (1) 53 dispatch violations, (2) model telemetry gap, (3) 0 real denials.`
- **Structured output belongs on sonnet subagents.** Use a sonnet subagent only to generate a long artifact from already decided content; evidence gathering goes to haiku Explore; judgment stays inline.

## Bounded Wave Scheduler (BWS)

Concurrency cap, superstep wave barrier, and progress/stall markers (plus file-range disjointness and scope-declaration rules) live in `scripts/docs/bws.md`.
