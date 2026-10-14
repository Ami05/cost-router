# Release Notes

## 1.0.0

- **Gate: deny mechanical work on reasoning-tier models.** `enforce_role_gates.py` now hard-blocks `Edit`/`Write`/`Shell` when the routing verdict is `Routing: FW` (mechanical) but the active model is a reasoning-tier model (e.g. `claude-opus-*`, `o1`, `o3`). Mechanical work must be dispatched to a fast-tier worker — emitting `FW` on a reasoning model and calling inline is now a gate violation.
- **ENRICH-001**: Enriched `routing_declared` events with `model`, `intent`, and `rule_id` fields (`check_routing.py`). Shadow pipeline (`shadow_log.py`, `shadow_evaluator.py`, `proposal_writer.py`) now produces per-intent evaluations and proposals instead of per-role aggregates. **Breaking:** `rule_id` values in `policy_proposals.json` changed from bare role strings (e.g. `"FO"`) to composite keys (e.g. `"FO:agentic_action"`). External consumers filtering on bare-role `rule_id` must update their filters.
- Added `rule_id` field to `confidence_scores.json` entries (`confidence_score.py`) to support composite key lookups in `proposal_writer.py`.
- Added one-time HWM reset in `shadow_log.py` to skip pre-enrichment rows in `shadow_routes.jsonl`.
- Added warning log when `classify_intent` returns `"unknown"` to surface silent `ROLE:unknown` bucket inflation.

## 0.1.8

- Added impact reporting pipeline: `scripts/impact_calculator.py` computes routing savings (Bucket 1) and waste prevented (Bucket 2) from `session_log.jsonl`, with correction factors, policy trend (week-over-week routing rate delta), and top savings moments.
- Added `scripts/session_impact.py` for per-session impact computation.
- Added `scripts/generate_reports.py` with `--card` and `--digest` modes: `--card` renders a shareable HTML impact card saved to `data/impact-card.html`; `--digest` renders a full markdown weekly digest saved to `data/weekly-digest-YYYY-MM-DD.md`.
- Added `/impact-card` skill (`skills/impact-card/SKILL.md`): generates the HTML savings card; requires ≥5 completed sessions.
- Added `/weekly-digest` skill (`skills/weekly-digest/SKILL.md`): generates a detailed weekly savings breakdown including routing rate trend, gate-type breakdown, and top 3 savings sessions; supports `--since YYYY-MM-DD`.
- Added unit tests for all three new scripts (`tests/test_impact_calculator.py`, `tests/test_session_impact.py`, `tests/test_generate_reports.py`).
- Version bump coordinated across `VERSION`, both plugin manifests, and `marketplace.json` via `scripts/bump-version.sh`.

## 0.1.7

- Consolidated 800+ unit tests into 56 focused E2E tests across 10 capability areas (`tests/e2e/`), cutting CI runtime from ~20s to <1s.

## 0.1.6

- README restructured for a first-time visitor: leads with a one-line thesis, a before/after example of the routing gate, badges, and a quick-start block; all prior detailed sections preserved under a new "How it works" fold.
- Documented both distribution paths and the decisions behind them — Path A (self-hosted marketplace, live now) and Path B (official `claude-plugins-official` directory, decided: pursue) for Claude Code; Cursor's marketplace distribution.
- Fixed `analyze_routing.py`'s over-route "estimated savings" figure: `fast_output` in config.json was `0.0003`, roughly 13x below haiku's real output price (`0.004` in `scripts/pricing.json`), overstating potential savings from re-routing to the fast tier. Corrected to match, and documented the intentional mirror relationship between the two files (MVP-1 / PROOF-3).
- Replaced the hardcoded `$0.56` `explore_sonnet_waste_usd` figure with a live derivation from `scripts/pricing.json` (`_model_utils.explore_sonnet_waste_usd()`), so it no longer silently drifts from real pricing (MVP-1).
- `/cost-report`'s session table now labels any row priced off a heuristic estimate (rather than a real, transcript-verified token count) with `†`, so real and estimated numbers are never silently blended in a stakeholder-facing report (PROOF-3).
- Added `.gitignore` with comprehensive exclusions: runtime data (`data/`), local deployment files (`install.sh`, `resync/`), machine-specific config (`cursor_claude/`), test artifacts, and internal content that must never enter public history.
- Added `.editorconfig` for consistent code style across editors (LF line endings, 4-space Python, 2-space JSON/shell).
- Added `.gitattributes` for cross-OS line ending normalization; marks binary assets (`.png`, `.zip`, `.gz`) to prevent diff corruption.
- Added `.pre-commit-config.yaml`: black + flake8 on all `scripts/*.py`, JSON validation for hook/plugin manifests, and a hard block on committing `data/*.jsonl` files.
- Added `.version-bump.json` + `scripts/bump-version.sh` for coordinated single-command version bumping across `VERSION`, both plugin manifests, and marketplace.json.

## 0.1.0

Initial public release.

- Model-router rule: routes agent work to the cheapest capable model (`Routing: DIRECT/FW/FO/RB/FV`), gate-enforced on Cursor and Claude Code.
- Local, privacy-conscious cost estimator (`/cost-report`) — heuristic token counts, no raw prompt/response text ever logged.
- Safety/cost gates: stale-file guard, repeat-read guard, dispatch-context-budget guard, resume/batch-work guard.
- Adaptive routing feedback loop (`tune-routing` skill): evidence-based Decision Table proposals, confidence-tiered auto-apply, automatic rollback on regression.
- Shared codebase across Cursor and Claude Code — `skills/`, `scripts/`, `commands/`, `rules/`, `data/` are identical on both platforms.
