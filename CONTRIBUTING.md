# Contributing to cost-router

Thanks for considering a contribution. A few things are specific to how this repo works — read this before opening a PR.

## Dev source vs. runtime — edit the right copy

`cost-router-v5/` (this folder, wherever you've cloned it) is the **canonical dev source**. Runtime hooks never read from here directly.

The actual runtime is an **installed copy** at `~/.cursor/plugins/local/cost-router` (Cursor) and `~/.claude/plugins/local/cost-router` (Claude Code). Never edit those installed copies directly — any changes there are silently overwritten the next time someone runs the resync step below.

**Dev workflow:**

```
edit cost-router-v5/   →   bash resync/resync.sh   →   reload Cursor / Claude Code
```

If you're testing a change, always go through `resync/resync.sh` rather than hand-copying files.

## One codebase, two platforms — don't fork shared files

`skills/`, `scripts/`, `commands/`, `rules/`, and `data/` are shared **verbatim** between Cursor and Claude Code. Do not create a Cursor-only or Claude-Code-only version of anything in those folders — if a change genuinely needs to branch by platform, branch inside the shared file (e.g. detect the host from the hook payload shape), don't duplicate the file. Only `.cursor-plugin/`, `.claude-plugin/`, `hooks/hooks.json`, and `hooks/hooks-cursor.json` are allowed to differ per platform.

## Proposing a routing Decision Table change

`rules/model-router.md`'s Decision Table is the routing gate's source of truth. There are two sanctioned paths to change it:

1. **Preferred — `tune-routing` skill.** Ask the agent to "tune routing," review the data-driven suggestion it proposes from `scripts/analyze_routing.py`, and only apply it after you've reviewed the exact row being added.
2. **Direct PR edits to `rules/model-router.md`.** These go through the same review as any other PR — explain in the PR description what evidence or reasoning motivates the new row, since a bad row here changes how every agent turn gets routed.

The source of truth is `rules/model-router.md`.

## Local setup

> **Plugin name is locked.** The `name` field in both `.claude-plugin/plugin.json` and `.cursor-plugin/plugin.json` (`cost-router`) is an immutable slug on both marketplaces once published — Anthropic's and Cursor's docs both confirm renaming it after publish breaks every existing install with a plugin-not-found error. Don't change it in a PR without raising that explicitly.

```bash
pip install pytest pre-commit
pre-commit install
```

`pre-commit install` wires up `.pre-commit-config.yaml`, which runs Python formatting/linting on `scripts/*.py`, JSON validation on `hooks/*.json` and both plugin manifests, and a check that blocks accidentally staging any `data/*.jsonl` or `data/*.json` file (these hold real local usage data and must never be committed — see SECURITY.md). Run `pre-commit run --all-files` once after installing to check your existing tree.

## Tests

```bash
pytest tests/ -q
```

One test file per script/feature — see `tests/readme.md` for naming conventions. Every test isolates its own temp `DATA_DIR`; none should touch a real `~/.cursor` or `~/.claude` install. Add a test alongside any behavior change.

## Opening a PR

- Fill in the [pull request template](.github/PULL_REQUEST_TEMPLATE.md) — PRs without it filled in will be asked to update before review.
- Make sure `pytest tests/ -q` and `pre-commit run --all-files` both pass locally first.
- For bug reports, use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md) — it asks for platform, hook event, and the `Routing:` verdict from the failing turn, since that triple is usually what's needed to actually diagnose a gate misfire.

## Code of Conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
