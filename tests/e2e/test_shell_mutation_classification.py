"""E2E tests: SHELL-CLASS-1 — shell mutation classification.

Verifies that deploy/resync scripts are exempted from GATE-3 (shell_calls_since_dispatch)
and that working-tree mutations and test suites still count as before.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import check_routing  # noqa: E402


class TestIsDeployCommand(unittest.TestCase):
    """Unit tests for _is_deploy_command helper (SHELL-CLASS-1)."""

    def test_resync_script_detected(self) -> None:
        self.assertTrue(check_routing._is_deploy_command("bash resync/resync.sh"))

    def test_deploy_script_detected(self) -> None:
        self.assertTrue(check_routing._is_deploy_command("bash scripts/deploy.sh"))

    def test_sh_deploy_detected(self) -> None:
        self.assertTrue(check_routing._is_deploy_command("sh deploy.sh"))

    def test_install_not_deploy(self) -> None:
        """install is excluded — ambiguous (pip/npm install modifies working tree)."""
        self.assertFalse(check_routing._is_deploy_command("bash install.sh"))

    def test_sync_not_deploy(self) -> None:
        """sync is excluded — ambiguous (could sync local state)."""
        self.assertFalse(check_routing._is_deploy_command("sh sync.sh"))

    def test_pytest_not_deploy(self) -> None:
        self.assertFalse(check_routing._is_deploy_command("pytest tests/"))

    def test_npm_test_not_deploy(self) -> None:
        self.assertFalse(check_routing._is_deploy_command("npm test"))

    def test_empty_command_not_deploy(self) -> None:
        self.assertFalse(check_routing._is_deploy_command(""))

    def test_plain_bash_not_deploy(self) -> None:
        self.assertFalse(check_routing._is_deploy_command("bash run_tests.sh"))


class TestBulkVerificationExcludesDeployScripts(unittest.TestCase):
    """Deploy scripts must NOT be classified as bulk verification commands."""

    def test_resync_not_bulk_verification(self) -> None:
        """bash resync/resync.sh must not trigger the bulk-verify ask flow."""
        self.assertFalse(
            check_routing._is_bulk_verification_command("bash resync/resync.sh"),
            "SHELL-CLASS-1: resync script must not be classified as a bulk verification command.",
        )

    def test_install_not_bulk_verification(self) -> None:
        """install.sh is not a test suite — must not trigger the bulk-verify ask flow."""
        self.assertFalse(check_routing._is_bulk_verification_command("bash install.sh"))

    def test_deploy_not_bulk_verification(self) -> None:
        self.assertFalse(check_routing._is_bulk_verification_command("bash scripts/deploy.sh"))

    def test_pytest_still_bulk_verification(self) -> None:
        """Full pytest suite must still be detected as bulk verification."""
        self.assertTrue(check_routing._is_bulk_verification_command("pytest tests/"))

    def test_npm_test_still_bulk_verification(self) -> None:
        self.assertTrue(check_routing._is_bulk_verification_command("npm test"))


if __name__ == "__main__":
    unittest.main()
