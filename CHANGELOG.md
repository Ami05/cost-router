# Changelog

All notable changes to cost-router are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.0.0] — 2026-10-14

### Added

- First 1.0.0 release of cost-router, consolidating the routing gates, cost estimation, and adaptive routing feedback loop from 0.1.x into a stable version.

## [0.1.0] — 2026-08-23

### Added

- **Routing gate enforcement** (`PreToolUse` hooks for Edit/Write/Shell/Task on both platforms) — every mutation requires an explicit `Routing: <ROLE> — <reason>` verdict in the agent's current turn text. Unrecognized turns are denied.
- **Routing roles**: DIRECT, FW (FastWorker), FO (FastOrchestrator), RB (ReasoningBrain) — each with defined tool-call permissions enforced by the gate.
- **Re-read detection and blocking** (`check_read_repeat.py`) — nudge on repeat 2, deny on repeat 3 for the same file within a session.
- **Session dispatch gate** — inline mutation counter; nudge at 3 Edit/Write/Shell calls without Task dispatch, hard deny at 6.
- **`/cost-report` skill** — per-session token and cost estimates, tier mix, subagent routing quality, dispatch context quality, role distribution, and policy changelog receipt.
- **`/tune-routing` skill** — feedback loop: reviews usage log for escalation patterns, repeat reads, and zero-dispatch sessions; proposes Decision Table updates.
- **Dual platform support** — Claude Code and Cursor from a single shared codebase (`scripts/`, `skills/`, `rules/`, `data/`). Only manifests and hook configs differ.
- **Cost estimation** — heuristic tokenizer (~4 chars/token with per-model correction factors), editable pricing table (`scripts/pricing.json`) with per-model input/output/cache pricing for haiku, sonnet, and opus.
- **Real token override** — on Claude Code, reads the project transcript (`~/.claude/projects/`) for actual per-model token counts when available; `/cost-report` marks each row `†` when falling back to the heuristic.
- **Adaptive routing pipeline (Claude Code, Phase 6)**:
  - `reward.py` — scores sessions (escalation, override, error, revert, cost, clean-completion bonuses).
  - `reward_signal.py` + `confidence_score.py` — accumulate per-rule reward signal into `data/confidence_scores.json`.
  - `policy_manager.py` — accumulates scenario evidence, shadow-validates proposals, auto-appends rows to `rules/model-router.md` with undo tokens.
  - Auto-rollback — reverts applied changes when post-apply escalation rate or cost regresses.
- **Shadow mode logging** (`shadow_log.py`) — logs counterfactual route alternatives to `data/shadow_routes.jsonl` each session for future policy evaluation.
- **Large-file-read gate** (`file_graph_gate.py`, `build_file_graph.py`) — blocks full reload of files that already have a symbol map; redirects to targeted offset/limit reads.
- **Stale-file guard** (`file_hash_guard.py`) — warns before Edit/Write if the file changed on disk since last read.
- **Resume/batch-work gate** (`detect_resume.py`) — forces Task dispatch when a prompt references 3+ pending work items.
- **Dispatch-context-budget gate** (`dispatch_context_gate.py`, `estimate_dispatch_context.py`) — estimates subagent prompt token usage before dispatch; nudges/blocks when over 70%/100% of the subagent's context window.
- **Agent-call tracker** (`agent_call_tracker.py`) — enforces haiku model requirement for Explore agents.
- **Privacy-by-design** — logs only hashes, byte counts, and coarse tiers locally. Never raw prompts, responses, file paths, or code.
- **Zero external dependencies** — pure stdlib Python throughout.
- **Test suite** — 686 tests, 38 files, ~83% script coverage. Adversarial robustness suite (`test_prod8_hardening.py`). All tests run against a temp `DATA_DIR`.
- **Fail-open** — every gate fails open on exception; malformed input produces a valid allow response, never a crash.
- `install.sh` — one-command install that detects Claude Code and Cursor, creates plugin dirs, and delegates to `resync/resync.sh`.

### Known Limitations

- **Token estimation is heuristic** (~4 chars/token + per-model correction factors). Labeled `(est.)` throughout. Not billing-accurate. Ignores prompt-cache discounts; real billing is likely lower.
- **Claude Code — model tier invisible after SessionStart.** The `model` field is only present in `SessionStart` hook payloads. All tool calls after that are tagged `tier: unknown`. Escalation detection and tier cost breakdown are Cursor-sourced only on Claude Code sessions. Token totals and gate enforcement are unaffected. `/cost-report` shows a notice when >50% of events are `tier: unknown`.
- **Encrypted/redacted thinking blocks.** A `Routing:` verdict placed only inside an encrypted thinking block is invisible to the gate. The `model-router` skill requires the verdict in visible assistant text for this reason.
- **Rare false-deny on first mutation.** If the host hasn't flushed the assistant's message to the transcript before `PreToolUse` fires, the gate may deny the first tool call. Self-heals on retry.
- **Adaptive routing is Claude-Code-heavy today.** Cursor sessions accumulate evidence and can have proposals rolled back, but the full-session `compact_log.py` auto-apply call is not yet wired into `hooks-cursor.json`'s `stop` list.

---

[0.1.0]: https://github.com/Ami05/cost-router/releases/tag/v0.1.0
