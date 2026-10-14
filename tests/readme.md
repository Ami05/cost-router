# cost-router — Testing Backlog (Automation Test Suite Spec)
---

## Conventions
- **Framework:** `pytest` for Python (`scripts/*.py`), `bats` + `shellcheck` + `bash -n` for
  shell (`hooks/run.sh`).
- **Isolation:** every test runs with a temp `DATA_DIR` (monkeypatch `_common.DATA_DIR`/`USAGE_LOG`
  or `HOME=$(mktemp -d)` for installers). No test touches the real `~/.cursor` or `~/.claude`.
- **Fixtures:** keep `tests/fixtures/` with real captured hook payloads per platform (Cursor,
  Claude Code) and per event, plus synthetic `usage.jsonl` logs of known shape.
- **Determinism:** freeze time (`freezegun` or inject a clock) wherever `ts`/cooldown matters.
- **File naming (2026-07-31):** one file per script/feature under test, named for what it
  tests rather than an arbitrary suite letter — see `tests/*.py` (e.g.
  `test_routing_gate.py`, `test_reward_scoring.py`, `test_analyze_routing_feedback.py`).
  The old "Suite A–L" lettering below is historical context for where tests originated,
  not the current file layout: A tokenizer/helpers · B logging/dedup · C estimate_tokens
  hook · D inject_rule · E report · F feedback loop · G telemetry · H install/export ·
  I config integrity · J e2e cross-platform · K adversarial/break · L improvement/feedback
  paths (L's tests were merged into whichever file already covered the same script,
  de-duplicating overlapping coverage).

---