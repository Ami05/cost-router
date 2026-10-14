# cost-router

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE) [![CI](https://github.com/Ami05/cost-router/actions/workflows/ci.yml/badge.svg)](https://github.com/Ami05/cost-router/actions/workflows/ci.yml) [![Works on](https://img.shields.io/badge/works%20on-Cursor%20%2B%20Claude%20Code-blueviolet)](#quick-start)

**Routes agent work to the cheapest capable model that can actually do it, and shows you the (est.) cost — for both Cursor and Claude Code, from one shared codebase.**

## Before / after

**Without cost-router:** every mutation — Edit, Write, Shell, Task — runs on whatever model the session happens to be using, with no record of why, and nothing catching a session that re-reads the same 2,000-line file for the tenth time on a reasoning model.

**With cost-router, a mutation is gated on an explicit routing decision:**

```
Agent: (attempts Edit — no Routing: verdict emitted yet this turn)
Gate:  ❌ denied — no routing verdict for this turn
Agent: Routing: FW — mechanical rename, single file
Agent: (Edit call)
Gate:  ✅ allowed
```

That's not a style nudge — it's an enforced `PreToolUse` gate. Every prompt/tool/response is logged locally (hashed, never raw text) so `/cost-report` shows real session cost and `tune-routing` can propose Decision Table edits from actual usage, not guesses.

## Quick start

**One-command local install (Claude Code + Cursor):**
```bash
git clone https://github.com/Ami05/cost-router cost-router
cd cost-router
chmod +x install.sh && ./install.sh
```
Then open a new Claude Code or Cursor session and run `/cost-report` to verify.

**Claude Code — self-hosted marketplace (no review, available today):**
```
/plugin marketplace add Ami05/cost-router
/plugin install cost-router@cost-router
```

**Cursor — no self-hosted path exists on this platform** (Cursor's only distribution mechanism is a reviewed submission at `cursor.com/marketplace/publish` — see [Distribution](#distribution--publishing) below). Until that's filed and approved, use local dev install below.

**Local dev / no marketplace:** see [Install](#install) below for `--plugin-dir`, manual copy, and cross-machine export/import.

Everything the plugin needs lives inside this folder. Copy `cost-router/` anywhere — another project, another machine, its own Git repo — and it works unchanged, on either platform.

## How it works

The rest of this README covers, in order: how one codebase serves both platforms, why numbers are estimated, the full file layout, dev workflow, install details, usage, the hook wiring on each platform, the adaptive-routing pipeline, known platform-level limitations (prompt caching, context compaction), privacy, configuration, and testing.

## One codebase, two platforms

`skills/`, `scripts/`, `commands/`, `rules/`, and `data/` are shared verbatim between Cursor and Claude Code — nothing is duplicated. Only the small platform-specific pieces differ:

| Component | Cursor | Claude Code |
|---|---|---|
| Manifest | `.cursor-plugin/plugin.json` | `.claude-plugin/plugin.json` |
| Hook config | `hooks/hooks-cursor.json` | `hooks/hooks.json` |
| Always-on routing rule | `.claude/rules/model-router.md` (dev) / `rules/model-router.md` (installed plugin, synced by `resync/resync.sh`) is canonical; the optional auto-apply pipeline (`policy_manager.py`, off by default via `policy.auto_apply_enabled`) reads/writes only marker-tagged rows in this copy and never touches human-written rows. Since Cursor can only load an `alwaysApply` rule from `.cursor/rules/`, not a plugin subfolder, the resync merge feeds it: `~/.cursor/hooks/register-global-cost-router.sh` mirrors the canonical rule's **body** into each workspace's `.cursor/rules/model-router.mdc` on every `workspaceOpen`/`sessionStart`, preserving that file's own Cursor-specific frontmatter (`alwaysApply: true`) — no per-project script needed | Claude Code loads the same rule natively from `.claude/rules/model-router.md` — no injection hook or script is involved, so there is one source of truth for the wording |
| Adaptive routing loop | Partial — `compact_log.py --checkpoint` runs on `beforeSubmitPrompt` (rollback-checking only); the full-session `compact_log.py` call that actually applies new proposals is **not currently wired** into `hooks-cursor.json`'s `stop` list | Full — `reward_signal.py` and `confidence_score.py` run on `Stop`, and full-session `compact_log.py` (which calls `policy_manager.evaluate_proposals()`) also runs on `Stop` |

Cost-tracking hook parity (prompt/tool/response capture → `data/usage.jsonl`) is implemented for both platforms — `scripts/estimate_tokens.py` and `scripts/session_summary.py` are shared, dispatched from each platform's own hook config with its own event names. One known gap: Claude Code only includes a `model` field on `SessionStart` payloads (not guaranteed even there), so Claude-sourced events are always tagged `tier: "unknown"` — token/cost totals and repeat-read detection are unaffected, but fast/reasoning escalation detection only has full signal on Cursor today. The adaptive-routing gap above is the mirror image of this: it's fuller on Claude Code than on Cursor.

## Why estimated, not exact, numbers

Cursor hook payloads do not currently include real prompt/completion token counts or billing data (confirmed platform limitation, no ETA as of writing). Rather than skip cost visibility entirely, this plugin estimates tokens locally from text captured at hook boundaries, using a simple heuristic tokenizer (~4 chars/token, with per-model correction factors in `config.json` → `token_estimation.correction`) and an editable pricing table. Every number this plugin reports is labeled **(est.)** — treat it as directional, not a substitute for your actual bill.

## What's inside

```
cost-router/
├── .cursor-plugin/plugin.json   Cursor plugin manifest (points hooks at hooks-cursor.json)
├── .claude-plugin/plugin.json   Claude Code plugin manifest
├── CLAUDE.md                    Documents the routing-gate role/tool matrix and the
│                                 "doc file convention" (see below)
├── config.json                  Central tunables — thresholds, penalties, caps, windows —
│                                 for every gate/script below; most are also env-var overridable
├── VERSION                      Single source of truth for the plugin version
├── rules/model-router.md        Always-on routing rule (installed copy of .claude/rules/model-router.md,
│                                 loaded natively by Claude Code; Cursor is fed by the resync merge).
│                                 This file is also a WRITE target: policy_manager.py
│                                 appends/removes rows here automatically — see "Adaptive routing" below
├── skills/                      SHARED — identical SKILL.md format on both platforms
│   ├── cost-report/SKILL.md     Renders the usage/cost report
│   ├── tune-routing/SKILL.md    Feedback loop: reviews the log, and documents the auto-apply/undo flow
│   └── dispatch-context-template/  Context budget template for subagent dispatch
├── hooks/
│   ├── hooks-cursor.json        Cursor hook config
│   ├── hooks.json               Claude Code hook config
│   └── run.sh                   Shared dispatcher — resolves plugin root, works from either platform
├── scripts/                     SHARED — pure stdlib Python, platform-agnostic. ~50 scripts;
│   │                             grouped by function below rather than listed file-by-file.
│   ├── _common.py, _model_utils.py      Shared helpers: tokenizer, pricing lookup, log I/O,
│   │                                     config.json readers, gate-result formatting, Cursor/Claude
│   │                                     payload-shape detection
│   ├── gates.py                          Unified gate dispatcher — see "How the pieces connect"
│   ├── estimate_tokens.py, session_summary.py, pricing.json   Cost tracking + /cost-report
│   ├── check_routing.py, classify_intent.py, compact_log.py   Core routing
│   │                                     gate, intent classification,
│   │                                     session compaction into scenario_library.json
│   ├── check_read_repeat.py, build_file_graph.py, file_graph_gate.py, graph_gc.py,
│   │   file_graph_refresh.py, mark_graph_stale.py   Repeat-read detection
│   │                                     and the per-file symbol-graph ("node map") system that
│   │                                     lets large-file reads be denied in favor of a targeted
│   │                                     offset/limit read
│   ├── file_hash_guard.py                Stale-file guard: warns before an Edit/Write if the file
│   │                                     changed on disk since the model last read it; also feeds
│   │                                     revert detection (see below)
│   ├── detect_resume.py, dispatch_context_gate.py, agent_call_tracker.py,
│   │   estimate_dispatch_context.py      Resume/batch-work gate (forces Task dispatch for
│   │                                     multi-item prompts), dispatch-prompt inline-file-body
│   │                                     gate, Explore-agent/model-tier + dispatch-budget gate
│   ├── analyze_routing.py                Feedback-loop pattern detection (escalations, repeat
│   │                                     reads, zero-dispatch sessions) that powers /cost-report's
│   │                                     nudges and the `tune-routing` skill
│   ├── reward.py, reward_signal.py, detect_revert.py, confidence_score.py,
│   │   shadow_log.py, policy_manager.py, policy_gc.py
│   │                                           Adaptive routing pipeline — see "Adaptive
│   │                                     routing" below, its own section given how much it does
│   ├── bump-version.sh                   Updates VERSION and both plugin manifests atomically
│   ├── doc_lint.py / readme_coverage_lint.py  Two similarly-named lint scripts currently coexist —
│   │                                          worth consolidating, not a functional split
│   └── docs/                            Reference .md files read by scripts/gates at runtime
│       │                                 (per CLAUDE.md's "doc file convention" — see below)
│       └── routing-gates.md              Implemented routing checks, file:line, triggers, thresholds
├── commands/
│   ├── cost-report.md            SHARED — /cost-report slash command
│   └── tune-routing.md           SHARED — /tune-routing slash command
├── data/                         Created on first run — gitignored, never committed. Holds
│                                 usage.jsonl plus ~15 adaptive-routing state files (see
│                                 "Storage & Privacy")
├── tests/                        pytest suite, one file per script/feature (60+ files) — see
│                                 tests/readme.md for conventions
├── docs/                         Reserved for supplementary docs (dev-only planning notes under
│                                 docs/superpowers/ are gitignored, not shipped)
├── resync/resync.sh              Dev → installed-copy sync script (see "Runtime vs dev source")
├── cursor_claude/                 Reference copies of the generated ~/.cursor/hooks.json and
│                                 ~/.claude/settings.json fragments this plugin registers
├── .github/workflows/            CI (includes a LICENSE-file presence check — see Roadmap/backlog)
└── .gitignore
```

**Doc file convention (from `CLAUDE.md`):** any `.md` referenced by plugin code at runtime must live in `scripts/docs/`, because only `scripts/` gets resynced to the installed copy.

## Runtime vs dev source

`cost-router-v4/` (this folder) is the **canonical dev source** — all edits happen here, tests run against it, and it is never directly referenced by runtime hooks.

The **runtime** is the installed plugin copy at `~/.cursor/plugins/local/cost-router` (Cursor) and `~/.claude/plugins/local/cost-router` (Claude Code). These are what sessions actually load, and each has its own independent `data/` directory — adaptive-routing state (proposals, confidence scores, changelogs) does **not** cross over between the two installs even though the code is shared.

**Dev workflow:**

```
edit cost-router-v4/   →   bash resync/resync.sh   →   reload Cursor / Claude Code
```

`resync.sh` rsyncs the dev workspace to both installed locations. Nothing outside `cost-router-v4/` should ever be edited directly — changes there would be overwritten on the next resync.

**Rule body sync (Cursor):** `~/.cursor/hooks/register-global-cost-router.sh` runs on every `workspaceOpen`/`sessionStart` and automatically syncs the installed plugin's `rules/model-router.md` body into any workspace that already has a `.cursor/rules/model-router.mdc` (which stays as is), preserving its `alwaysApply: true` frontmatter. No per-project script is needed — any workspace gets rule updates automatically after a resync + reload. Because `policy_manager.py` now edits `rules/model-router.md` directly (see below), this sync path is also how an auto-applied routing change reaches a given Cursor workspace.

## Platform Limitations

| Platform | Limitation |
|---|---|
| **Claude Code** | The `model` field is only included in `SessionStart` hook payloads. Every tool call after that is tagged `tier: unknown`. As a result, escalation detection and tier cost breakdown in `/cost-report` are blind for Claude Code sessions. Token totals, gate enforcement, and repeat-read blocking are unaffected. |
| **Cursor** | Hook payloads do not include real token counts or billing data. All cost figures are heuristic estimates (`~4 chars/token`). The adaptive auto-apply pipeline (reward → confidence → auto-apply) is not wired into Cursor's `stop` hooks today — evidence accumulates but proposals are not auto-applied on Cursor. |

These are platform constraints, not bugs. `/cost-report` will show a notice when >50% of events in a Claude Code session are `tier: unknown`.

## Install

### Cursor

- **Local dev:** copy (do not symlink) this folder to `~/.cursor/plugins/local/cost-router/`, enable third-party plugins, reload Cursor.
- **Enterprise:** org admin must enable **Allow Local Plugin Imports**, or use the `workspaceOpen` hook pattern (`register-global-cost-router.sh`, described above under "Rule body sync").
- **Fully portable:** nothing outside `cost-router/` is read or written by the plugin itself. Hook scripts use paths relative to the plugin root.

### Claude Code

- **Local dev/testing:** run `claude --plugin-dir /path/to/cost-router` — loads for that session only. Run `/reload-plugins` after edits.
- **Persistent local install (another machine):** clone or copy the repo, then run:
  ```bash
  bash install.sh
  ```
  Merges hooks into `~/.claude/settings.json` idempotently — hooks fire automatically for every `claude` session. No wrapper or `--plugin-dir` flag needed.
- Verify with `/context` (model-router rule loaded from `.claude/rules`), then the cost-report skill after a few tool calls.
- Usage data is written to `~/.claude/plugins/local/cost-router/data/usage.jsonl`.
- If you have a legacy `SessionStart` hook in `~/.claude/settings.json` injecting `~/.claude/skills/model-router.md`, disable it — Claude Code now loads the rule natively from `.claude/rules/model-router.md`.

## Usage

- `/cost-report` — see estimated token/cost usage for recent sessions, on **either** platform (both write to the same `data/usage.jsonl` format).
- Ask the agent to "tune routing" (or invoke the `tune-routing` skill) periodically to review data-driven suggestions for the decision table — and to check on proposals that are queued or already auto-applied (see below).
- The always-on routing rule is loaded natively by Claude Code from `.claude/rules` and fed to Cursor by the resync merge — no extra step needed either way.
- `python3 scripts/policy_manager.py --status` / `--list` / `--undo TOKEN` — inspect or reverse adaptive-routing changes directly from the CLI.

## How the pieces connect

Both platforms now route almost every hook event through a small number of **unified dispatchers** (`gates.py` for permission decisions, plus a handful of always-run observational scripts) rather than wiring each individual check straight into the hook config. `gates.py`'s `GATE_REGISTRY` chains several gate functions per event and short-circuits on the first deny; every gate in the chain fails open on its own exception.

```
GATE_REGISTRY (scripts/gates.py):
  pre_read  -> check_read_repeat, build_file_graph, routing "read" bookkeeping,
               file_hash_guard (read), file_graph_gate (deny full reload of a
               large file that already has a symbol/"node" map — use offset/limit)
  pre_write -> check_routing "gate" (the routing gate, below), file_hash_guard (pre_edit)
  pre_task  -> explore-model-tier gate, dispatch_context_gate (inline-body +
               handoff-profile enforcement on Task prompts)
```

### Cursor (`hooks/hooks-cursor.json`)

```
beforeSubmitPrompt ─┐
postToolUse         ├──► estimate_tokens.py ──► data/usage.jsonl
afterAgentResponse ─┘

beforeSubmitPrompt ──► check_routing.py --event reset, compact_log.py --checkpoint
                        (rollback-checking only — see "Adaptive routing"),
                        classify_intent.py, detect_resume.py

postToolUse (Write|StrReplace|Edit) ──► mark_graph_stale.py, file_hash_guard.py --event post_edit

beforeReadFile (read_file) ──► estimate_tokens.py, check_read_repeat.py, build_file_graph.py,
                                check_routing.py --event read, file_hash_guard.py --event read

preToolUse (Write|StrReplace|Edit|Shell|Task|Delete) ──► check_routing.py --event gate
                        (the routing gate — see below)
preToolUse (Write|StrReplace|Edit) ──► file_hash_guard.py --event pre_edit
preToolUse (Agent|Task) ──► agent_call_tracker.py

afterAgentResponse, afterAgentThought ──► check_routing.py --event scan
subagentStart, subagentStop ──► check_routing.py --event subagent_start/_stop

stop ──► session_summary.py ──► appends a per-session summary row to the same log
     └─► graph_gc.py, file_graph_refresh.py
         (Note: reward_signal.py / confidence_score.py and the
          full-session compact_log.py call are NOT in Cursor's `stop` list today —
          see the "Adaptive routing loop" row in "One codebase, two platforms")

data/usage.jsonl ──► cost-report skill (session_summary.py --report)
                 └─► tune-routing skill (analyze_routing.py) ──► proposed edits to
                                                                   rules/model-router.md
```

### Claude Code (`hooks/hooks.json`)

```
(Always-on rule: loaded natively from .claude/rules/model-router.md — no SessionStart injection hook)

UserPromptSubmit ──► estimate_tokens.py --event prompt ──► data/usage.jsonl
UserPromptSubmit ──► check_routing.py --event reset, compact_log.py --checkpoint,
                      classify_intent.py, detect_resume.py,
                      read_gate.py --event reset

PostToolUse ──► estimate_tokens.py --event tool ──► data/usage.jsonl
PostToolUse (Task) ──► read_gate.py --event reset
PostToolUse (Write|Edit|MultiEdit) ──► mark_graph_stale.py, file_hash_guard.py --event post_edit
PostToolUse (Bash|Shell) ──► detect_revert.py   (git checkout/restore/reset after an edit in the
                                                  same session -> reward_signal "quality_failure")

PreToolUse (Read|read_file) ──► gates.py --gate pre_read, read_gate.py
PreToolUse (Write|Edit|Bash|Shell|StrReplace|Task|Delete|NotebookEdit) ──► gates.py --gate pre_write
    (this is where the routing gate — check_routing.py's event_gate — actually runs on
     Claude Code: it's one link in gates.py's pre_write chain, not a standalone hook entry)
PreToolUse (Agent|Task) ──► agent_call_tracker.py

SubagentStart, SubagentStop ──► check_routing.py --event subagent_start / subagent_stop

Stop ──► estimate_tokens.py --event response ──► data/usage.jsonl
     └─► check_routing.py --event scan   (final scan of the assistant's last turn)
     └─► reward_signal.py, confidence_score.py   (adaptive routing — see below)
     └─► session_summary.py ──► appends a per-session summary row to the same log
     └─► graph_gc.py
     └─► compact_log.py   (full-session merge into scenario_library.json, then calls
                            policy_manager.evaluate_proposals() and policy_manager.check_rollback()
                            — this is what actually writes to rules/model-router.md)
```

Both platforms' hooks funnel into the exact same `data/usage.jsonl` schema, so `/cost-report`, `analyze_routing.py`, and the `tune-routing` nudge logic work identically regardless of which platform produced the data — a report can even mix sessions from both in the same rolling window (the adaptive-routing pipeline is the exception, per the table above).

### The routing gate, the turn-tagged verdict cache, and sub-agent exemption

- **The gate** (`check_routing.py`'s `event_gate`, reached via `gates.py --gate pre_write` on Claude Code and directly via `preToolUse` on Cursor) denies a mutating tool call (Write/Edit/Bash/Task/etc.) unless the agent has emitted a `Routing:` verdict line somewhere in its own text *for the current turn*. This is what the "Routing: <ROLE> — <reason>" checkpoint in the model-router rule actually enforces — it isn't just a style convention, an unrecognized turn without a verdict gets denied. `CLAUDE.md` documents the exact role → allowed-tool matrix (`DIRECT`/`FW` may call Edit/Write/Shell directly; `FO`/`RB` may only call `Task`).
- **The verdict cache is turn-tagged, not session-tagged.** Each session tracks a `turn_id` and a `verdict`/`verdict_turn_id` pair. A verdict emitted on turn N only satisfies the gate for turn N — a new user turn resets it, so the agent has to re-commit to a routing decision every turn rather than one verdict silently covering an entire session.
- **Sub-agent exemption.** When a `Task` dispatches a sub-agent, that sub-agent's own tool calls would otherwise hit the same gate with no verdict of its own to point to. `SubagentStart` marks the correlation key (session/conversation id) as having an active sub-agent; the gate checks this and exempts sub-agent tool calls for the TTL of that marker; `SubagentStop` clears it and (on Claude Code, confirmed; Cursor unverified) reads the sub-agent's own transcript for real token usage rather than falling back to a heuristic estimate.

**Known gap (unchanged):** Claude Code only includes a `model` field on `SessionStart` payloads (not guaranteed even there). Every Claude-sourced event in the log is therefore `tier: "unknown"`; token/cost totals and repeat-read detection are fully accurate, but the escalation-detection signal in `tune-routing` currently only has full data from Cursor sessions.

## Adaptive routing: reward signals, confidence, auto-apply, and rollback

**This is the biggest change since the single-codebase phase, and it changes a safety-relevant claim:** earlier versions of this plugin only ever *proposed* rule-table edits for a human to approve. That is no longer true for Claude Code. There are now two related, only-partially-overlapping pipelines that can edit `rules/model-router.md` (or a staging table) without a human clicking "yes" in the moment — both are reversible, both are gated by evidence thresholds and blast-radius checks, and both roll back automatically on regression, but neither waits for a chat confirmation before the first apply.

**Pipeline 1 — proposal accumulation (`policy_manager.py`), live on Claude Code today:**

1. Each completed session is compacted (`compact_log.py`, `Stop` hook) into `data/scenario_library.json` under a key like `Write:fast:escalated`, tagged with a computed `reward` (`reward.py` — penalizes manual overrides, fast→reasoning escalations, errors, and same-file reverts; rewards clean completions; penalizes cost and over-routing).
2. `policy_manager.accumulate_evidence()` turns repeated keys into a proposed rule row (e.g. `Write(spec_complete) -> FW`), classifies its blast radius (`high` for anything touching Task/FO routing or reversing a reasoning→fast decision, `low` otherwise), and promotes it from `accumulating` → `queued` (≥2 sessions) → `auto_apply_eligible` (evidence count ≥ policy.auto_apply_threshold in config.json, low blast radius).
3. `compact_log.py` then calls `policy_manager.evaluate_proposals()`, which, only when `policy.auto_apply_enabled` is set (off by default), **applies each eligible proposal** — appending a marker-tagged row to `rules/model-router.md` automatically and recording an 8-hex `undo_token` in `data/routing_changelog.json`.
4. `policy_manager.check_rollback()` (called from `compact_log.py --checkpoint`, so this part *does* run on both platforms) compares pre-/post-apply mean reward and calls `undo()` — removing the row and marking the changelog entry `rolled_back` — if quality drops by more than 0.2.
5. You can inspect or reverse any of this yourself: `python3 scripts/policy_manager.py --status`, `--list` (queued only), `--undo TOKEN`.

**How this relates to the manual workflow:** the `tune-routing` skill's numbered workflow (steps 1–4) is advisory and human-confirmed. The auto-apply pipeline above is a separate mechanism, off by default via `policy.auto_apply_enabled`. When enabled it edits only marker-tagged rows in `rules/model-router.md`, never human-written rows, and every change is reversible by undo token. The two apply to different evidence tiers and do not conflict.

If you want to be sure auto-apply stays off, leave `policy.auto_apply_enabled` unset (the default); to also remove the trigger, the auto-apply hook to remove from `hooks/hooks.json`'s `Stop` list is the `compact_log.py` call that isn't `--checkpoint` (Pipeline 1's trigger, via `policy_manager.evaluate_proposals()`); `confidence_score.py` and `reward_signal.py` can stay since they only ever write observational logs.

## Prompt caching — not a lever this plugin controls

cost-router injects static, repeated content every session (the routing rule loaded natively from `.claude/rules`, plus the remaining skill files). Anthropic's prompt caching (`cache_control`, up to a 90% discount on cache reads) is the single highest-leverage cost lever generally available for exactly this kind of stable, repeated context — so it's worth stating plainly what cost-router can and can't do about it, rather than leaving it unaddressed.

**Checked directly against both platforms' current docs and hook APIs, not assumed:**

- **Claude Code:** `cache_control` is a raw Anthropic Messages API parameter set on the actual HTTP request to `api.anthropic.com` — a plugin communicates with the host only via stdin/stdout JSON at hook boundaries (`additionalContext`, `hookSpecificOutput`, etc.) and never constructs or touches that request directly, so there is no hook-level surface to set it even in principle. This isn't a gap, though: Claude Code's own docs state plugin-provided content (skills, commands, hooks) is **never** cache-invalidated — it's appended after the existing cached conversation, so the next request still reads everything before it from cache. In practice, cost-router's injected rule and skill files already ride on Claude Code's native caching without any action needed on the plugin's part.
- **Cursor:** the same architectural constraint applies — hooks and rules communicate with Cursor over stdio/file content, not a raw API request, so there is no `cache_control` surface exposed to a plugin or an `alwaysApply` rule either. Cursor's own hooks and rules docs don't describe any per-rule caching control. Unlike Claude Code, Cursor's docs don't explicitly state whether rule/hook content benefits from caching automatically on Cursor's own backend — that's simply not publicly documented, so this is left as an open question about Cursor's internals, not something cost-router can resolve or control from a plugin.

**Bottom line:** prompt caching is real, and worth knowing about, but it is a host/platform-level lever, not a plugin-level one, on both platforms cost-router supports today. Nothing here needed fixing in cost-router itself — there was nothing to fix, only something to confirm and document so it isn't silently assumed to be a gap.

## Context compaction — audited against Anthropic's native Context Compaction API

Claude Code has dedicated `PreCompact`/`PostCompact` hook events, plus a `SessionStart` matcher for `source: compact`. cost-router currently wires neither for rule injection (the rule loads natively from `.claude/rules`) — no `PreCompact`/`PostCompact` hooks exist in this repo today.

**What's actually at risk, checked concretely rather than assumed:**

- **`/cost-report`'s dollar totals (`session_summary.py`):** correctly **not** reset by a compaction event, and shouldn't be — tokens spent before a compaction were real API calls that cost real money; summing them into the session total is the right behavior, not a bug.

## Storage & Privacy

cost-router logs only hashes, byte counts, and coarse model tiers locally to `data/usage.jsonl` — never raw prompt/response text, file paths (with one named exception), or code. Full details, including the two named exceptions to the hash-only rule and the org insight sharing policy, are in [SECURITY.md](SECURITY.md).


## Configuration

`config.json` at the plugin root is the central home for tunable thresholds across every gate and script described above — dispatch/session limits, reward penalties/bonuses, confidence and rollback thresholds, context-budget percentages, token-estimation correction factors, and more. Most individual values also have an environment-variable override (e.g. `COST_ROUTER_NUDGE_COOLDOWN_HOURS`, `CONFIDENCE_WINDOW`, `CONFIDENCE_THRESHOLD`, `ROLLBACK_WINDOW`, `ROLLBACK_ESCALATION_FACTOR`, `ROLLBACK_COST_FACTOR`, `ROLLBACK_THRESHOLD`, `MIN_SAMPLES`, `RECOVERY_THRESHOLD`, `REVERT_CHANGE_ID`) — check the relevant script's `_get_*_env` helpers for the exact name before assuming one exists.

## Estimation method & limits

- Heuristic tokenizer: `len(text) / 4`, rounded, with per-model correction multipliers (`config.json` → `token_estimation.correction`) for prose/code/symbol-heavy text. No external dependency, consistent with how the rest of this plugin avoids third-party packages.
- `scripts/pricing.json` is a community-maintained, approximate $/1K-token table, matched by substring against the model name reported in hook payloads. Edit it to match current list prices.
- Ignores prompt-caching discounts, so real billing is very likely lower than the estimate shown here.
- Model name/tier detection depends on the hook payload including a `model` field. Cursor includes one on most events; Claude Code only includes one on `SessionStart` (not guaranteed even there), so Claude-sourced events are tagged `tier: "unknown"` and priced at the pricing table's `default` entry **unless** a real Claude Code project transcript is found for that session (see `_read_project_usage` in `session_summary.py`), in which case the actual per-model input/output/cache token counts from the transcript override the heuristic entirely and the session is real, not estimated. `/cost-report`'s table marks every row that fell back to the heuristic path with `†` — see the note beneath the table — so a stakeholder report never silently blends real and estimated numbers.

## Known limitations

- **A `Routing:` verdict placed only inside encrypted/redacted thinking is invisible to the gate.** The gate extracts assistant text from the transcript's `text`/`thinking` content blocks; a redacted-thinking block carries no readable string field, so a verdict that only ever appeared there can't be detected. The model-router rule already requires the verdict in visible assistant text for this reason.
- **A rare false-deny is possible on a turn's very first mutation**, if the host hasn't flushed the assistant's message to the transcript file before `PreToolUse` fires. This self-heals on retry.
- **The auto-apply pipelines (see "Adaptive routing") are Claude-Code-heavier than Cursor today.** A workspace running only Cursor accumulates evidence and can have proposals rolled back, but won't see new low/medium-blast-radius rows auto-applied until the full-session `compact_log.py` call is also wired into `hooks-cursor.json`'s `stop` list.
- **Docs are consistent on auto-apply:** `skills/tune-routing/SKILL.md` and this README both describe the manual workflow as advisory and auto-apply as a separate pipeline, off by default via `policy.auto_apply_enabled`, editing only marker-tagged rows.

## Testing

A pytest suite lives in `tests/` — one file per script/feature (60+ files as of this writing; see `tests/readme.md` for naming conventions and history). Shell scripts (`hooks/run.sh`) are intended to be covered by `bats` + `shellcheck` + `bash -n` per the same conventions doc. Every test runs against a temp `DATA_DIR`; none touch a real `~/.cursor` or `~/.claude` install.

## The feedback loop (`tune-routing`)

`scripts/analyze_routing.py` looks for three signals in the log:

1. **Escalation pattern** — a `fast`-tier event followed shortly (within a few events, same session) by a `reasoning`-tier event on the same target. This suggests the task should have been routed straight to the reasoning model instead of trying the fast model first.
2. **Repeat large-file reads** — the same file (matched by hash, not path) read more than once in a session without offset/limit. Measured via the read hooks above, with live nudge/deny enforcement after repeat 2/3 respectively.
3. **Zero subagent dispatch** — a session with ≥12 tool calls and no `Task` call anywhere. This means the model-router mechanism never engaged for that session.

These feed both the manual `tune-routing` workflow (propose a row, human confirms, apply) and the automatic pipelines described in "Adaptive routing" above — they're two consumers of the same underlying evidence, not two different measurements.

### When you get nudged to run it

`/cost-report` checks a defined trigger every time it runs and appends a one-line suggestion when warranted:

| Signal (new evidence since the last nudge) | Threshold |
|---|---|
| Fast→reasoning escalations | ≥ 2 |
| Redundant file re-reads | ≥ 3 |
| Sessions with ≥12 tool calls and zero `Task` (subagent) dispatches | ≥ 1 |
| Sessions with no strong signal yet (periodic check-in) | ≥ 15 |

Any one of these triggers a nudge. The plugin never nudges again within a cooldown window of the last nudge (default 2 hours, override via `COST_ROUTER_NUDGE_COOLDOWN_HOURS`). State lives in `data/tune_state.json`.

## Distribution & Publishing

cost-router is distributed through the following channels.

### Claude Code — self-hosted marketplace

Install directly from the public repo:
```
/plugin marketplace add Ami05/cost-router
/plugin install cost-router@cost-router
```

### Claude Code — official directory

cost-router is also being submitted to Anthropic's official `claude-plugins-official` directory, which makes it available alongside the self-hosted marketplace above.

### Cursor

cost-router is distributed through the Cursor marketplace (`cursor.com/marketplace`).

## License

MIT — see [LICENSE](LICENSE).

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md). See [CONTRIBUTING.md](CONTRIBUTING.md) for how to contribute, and [SECURITY.md](SECURITY.md) for the plugin's data/privacy boundary and how to report a vulnerability.

## Roadmap

- **Phase 1 (done)** — routing rule/skill + cost estimation hooks + `/cost-report` (Cursor).
- **Phase 2 (done)** — `tune-routing` feedback skill (manual, human-confirmed edits).
- **Phase 3 (done)** — single shared codebase for Cursor + Claude Code.
- **Phase 4 (done)** — full Claude Code cost-tracking hook parity with Cursor.
- **Phase 5 (done)** — large-file-read interception (`build_file_graph.py`/`file_graph_gate.py`), stale-file guard (`file_hash_guard.py`), resume/batch-work gate (`detect_resume.py`), dispatch-context-budget and inline-body gates (`dispatch_context_gate.py`, `agent_call_tracker.py`, `estimate_dispatch_context.py`).
- **Phase 6 (in progress, Claude-Code-ahead-of-Cursor)** — adaptive routing: reward scoring, shadow validation, and automatic rollback (see "Adaptive routing" above). Remaining work: wire the full-session `compact_log.py` auto-apply trigger into Cursor's `stop` hooks for parity; reconcile `skills/tune-routing/SKILL.md`'s stale "advisory only" note.
- **Phase 7 (future, not in this build)** — publish to both the Cursor and Claude Code marketplaces; optional companion dashboard extension (out of scope for a "plugin" on either platform — plugins cannot render UI).
