"""E2E tests: TICKET-040 (fix-7) — tiered verification policy.

Verifies (1) the classification rule in scripts/verification_tier.py, and
(2) that check_routing.py's bulk-verify gate redirects a LIGHT-tier fix's
broad verification command to a scoped one instead of forcing the mandatory
full independent re-run flow.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import verification_tier  # noqa: E402
import check_routing  # noqa: E402
import session_state  # noqa: E402


class TestClassifyFixTier(unittest.TestCase):
    def test_single_file_decided_pattern_is_light(self) -> None:
        tier = verification_tier.classify_fix_tier(
            ["scripts/foo.py:10"],
            pattern="Rename local var x to y.",
            worker="mutation",
        )
        self.assertEqual(tier, verification_tier.LIGHT)

    def test_multi_file_is_full(self) -> None:
        tier = verification_tier.classify_fix_tier(
            ["scripts/foo.py:10", "scripts/bar.py:20"],
            pattern="Rename local var x to y.",
            worker="mutation",
        )
        self.assertEqual(tier, verification_tier.FULL)

    def test_no_files_is_full(self) -> None:
        self.assertEqual(
            verification_tier.classify_fix_tier(None, pattern="x", worker="mutation"),
            verification_tier.FULL,
        )

    def test_schema_keyword_forces_full(self) -> None:
        tier = verification_tier.classify_fix_tier(
            ["scripts/foo.py:10"],
            pattern="Change the API contract for this schema.",
            worker="mutation",
        )
        self.assertEqual(tier, verification_tier.FULL)

    def test_constraints_keyword_forces_full(self) -> None:
        tier = verification_tier.classify_fix_tier(
            ["scripts/foo.py:10"],
            pattern="Fix off-by-one.",
            constraints=["no breaking migration"],
            worker="mutation",
        )
        self.assertEqual(tier, verification_tier.FULL)

    def test_no_decided_pattern_is_full(self) -> None:
        """A single file with no fully-decided pattern still leaves ambiguity
        to the worker — must not qualify for LIGHT."""
        self.assertEqual(
            verification_tier.classify_fix_tier(["scripts/foo.py:10"], pattern=None, worker="mutation"),
            verification_tier.FULL,
        )

    def test_orchestrator_worker_is_full(self) -> None:
        """Non-mutation dispatches (orchestrator/design) carry ambiguity by
        definition and must never classify LIGHT even with one file."""
        tier = verification_tier.classify_fix_tier(
            ["scripts/foo.py:10"],
            pattern="Implement the redesign.",
            worker="orchestrator",
        )
        self.assertEqual(tier, verification_tier.FULL)

    def test_requires_full_rerun(self) -> None:
        self.assertFalse(verification_tier.requires_full_rerun(verification_tier.LIGHT))
        self.assertTrue(verification_tier.requires_full_rerun(verification_tier.FULL))


class TestClassifyDispatchJson(unittest.TestCase):
    def test_valid_mutation_json_light(self) -> None:
        task_text = (
            '{"worker":"mutation","intent":"fix typo","files":["scripts/foo.py:5"],'
            '"pattern":"Fix the typo in the log message.","output":"targeted_edits"}'
        )
        tier, files = verification_tier.classify_dispatch_json(task_text)
        self.assertEqual(tier, verification_tier.LIGHT)
        self.assertEqual(files, ["scripts/foo.py:5"])

    def test_unparseable_json_is_full(self) -> None:
        tier, files = verification_tier.classify_dispatch_json("not json")
        self.assertEqual(tier, verification_tier.FULL)
        self.assertEqual(files, [])

    def test_empty_text_is_full(self) -> None:
        tier, files = verification_tier.classify_dispatch_json(None)
        self.assertEqual(tier, verification_tier.FULL)
        self.assertEqual(files, [])


class TestTouchedFileAndCommandScoping(unittest.TestCase):
    def test_touched_file_from_single_entry(self) -> None:
        self.assertEqual(
            verification_tier.touched_file_from_files(["scripts/foo.py:5,10"]),
            "scripts/foo.py",
        )

    def test_touched_file_from_multi_entry_is_none(self) -> None:
        self.assertIsNone(
            verification_tier.touched_file_from_files(["scripts/foo.py:5", "scripts/bar.py:6"])
        )

    def test_scoped_command_is_ok(self) -> None:
        self.assertTrue(
            verification_tier.light_tier_command_ok(
                "pytest tests/e2e/test_foo.py", "scripts/foo.py"
            )
        )

    def test_broad_command_is_not_ok(self) -> None:
        self.assertFalse(
            verification_tier.light_tier_command_ok("pytest tests/", "scripts/foo.py")
        )

    def test_missing_inputs_fail_closed(self) -> None:
        self.assertFalse(verification_tier.light_tier_command_ok("", "scripts/foo.py"))
        self.assertFalse(verification_tier.light_tier_command_ok("pytest tests/foo.py", None))


class TestGateRedirect(unittest.TestCase):
    """Verifies the bulk-verify gate branch in check_routing.py redirects a
    LIGHT-tier fix's broad command instead of falling into the standard
    ask/deny/dispatch flow."""

    def setUp(self) -> None:
        # Isolate routing state per test. The shared tests/data state files are
        # rewritten by conftest's autouse reset in every xdist worker, which can
        # wipe the session entry saved below before event_gate reads it.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for mod in (check_routing, session_state):
            for attr, fname in (
                ("ROUTING_STATE_FILE", "routing_state.json"),
                ("GATE_COUNTERS_FILE", "gate_counters.json"),
                ("SUBAGENT_STATE_FILE", "subagent_state.json"),
            ):
                p = mock.patch.object(mod, attr, root / fname)
                p.start()
                self.addCleanup(p.stop)

    def _payload(self, command: str, session_id: str) -> dict:
        return {
            "session_id": session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }

    def test_light_tier_broad_command_redirected(self) -> None:
        session_id = "test-ticket-040-light-redirect"
        state = check_routing._load_state()
        entry = check_routing._get_session(state["sessions"], session_id)
        entry["last_dispatch_tier"] = "light"
        entry["last_dispatch_tier_file"] = "scripts/foo.py"
        check_routing._save_state(state)

        payload = self._payload("pytest tests/", session_id)
        result = check_routing.event_gate(payload, True)
        hso = result.get("hookSpecificOutput", {})
        self.assertEqual(hso.get("permissionDecision"), "deny")
        message = hso.get("additionalContext", "")
        self.assertIn("light-tier", message)
        self.assertIn("TICKET-040", message)

    def test_light_tier_scoped_command_not_redirected(self) -> None:
        """A command already scoped to the touched file must not be denied
        by the light-tier redirect (it isn't classified as bulk at all)."""
        session_id = "test-ticket-040-light-scoped"
        state = check_routing._load_state()
        entry = check_routing._get_session(state["sessions"], session_id)
        entry["last_dispatch_tier"] = "light"
        entry["last_dispatch_tier_file"] = "scripts/foo.py"
        check_routing._save_state(state)

        payload = self._payload("pytest scripts/foo.py", session_id)
        result = check_routing.event_gate(payload, True)
        hso = result.get("hookSpecificOutput", {})
        message = hso.get("additionalContext") or result.get("systemMessage") or ""
        self.assertNotIn("TICKET-040", message)

    def test_full_tier_broad_command_still_goes_through_ask_flow(self) -> None:
        """No tier recorded (or FULL tier) must fall through to the existing
        bulk-verify ask/deny flow, unchanged."""
        session_id = "test-ticket-040-full-tier"
        state = check_routing._load_state()
        entry = check_routing._get_session(state["sessions"], session_id)
        entry["last_dispatch_tier"] = "full"
        entry["last_dispatch_tier_file"] = None
        entry["bulk_shell_choice"] = None
        entry["bulk_shell_pending_hash"] = None
        check_routing._save_state(state)

        payload = self._payload("pytest tests/", session_id)
        result = check_routing.event_gate(payload, True)
        hso = result.get("hookSpecificOutput", {})
        self.assertEqual(hso.get("permissionDecision"), "deny")
        message = hso.get("additionalContext") or result.get("systemMessage") or ""
        self.assertNotIn("TICKET-040", message)


if __name__ == "__main__":
    unittest.main()
