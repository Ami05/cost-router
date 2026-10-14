"""E2E tests: plugin manifest and config file integrity.

Reads the real repository files — no mocking.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO / "scripts"
HOOKS_DIR = REPO / "hooks"
CONFIG_JSON = REPO / "config.json"
PRICING_JSON = SCRIPTS_DIR / "pricing.json"
CURSOR_MANIFEST = REPO / ".cursor-plugin" / "plugin.json"
CLAUDE_MANIFEST = REPO / ".claude-plugin" / "plugin.json"
CURSOR_HOOKS = HOOKS_DIR / "hooks-cursor.json"
CLAUDE_HOOKS = HOOKS_DIR / "hooks.json"
VERSION_FILE = REPO / "VERSION"


# 1 ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("json_file", [
    pytest.param(CURSOR_MANIFEST, id="cursor-manifest"),
    pytest.param(CLAUDE_MANIFEST, id="claude-manifest"),
    pytest.param(CURSOR_HOOKS, id="cursor-hooks"),
    pytest.param(CLAUDE_HOOKS, id="claude-hooks"),
    pytest.param(CONFIG_JSON, id="config"),
    pytest.param(PRICING_JSON, id="pricing"),
])
def test_json_files_parse_cleanly(json_file):
    """Every JSON config file must exist and parse without error."""
    assert json_file.exists(), f"Missing file: {json_file}"
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{json_file.name} top-level must be an object"


# 2 ──────────────────────────────────────────────────────────────────────────

def test_hook_scripts_referenced_in_hooks_exist():
    """Every Python script referenced in hooks.json and hooks-cursor.json must exist in scripts/."""

    def _script_names(hooks_data: dict) -> list[str]:
        raw = json.dumps(hooks_data)
        return [m.group(1) for m in re.finditer(r'run\.sh[^a-z]*([a-z_]+\.py)', raw)]

    for hooks_file in (CLAUDE_HOOKS, CURSOR_HOOKS):
        if not hooks_file.exists():
            continue
        hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
        for name in _script_names(hooks):
            assert (SCRIPTS_DIR / name).exists(), (
                f"{name} referenced in {hooks_file.name} not found in scripts/"
            )


# 3 ──────────────────────────────────────────────────────────────────────────

def test_manifest_version_matches_version_file():
    """VERSION file must match the version field in both plugin manifests."""
    if not VERSION_FILE.exists():
        pytest.skip("VERSION file not present")

    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    assert version, "VERSION file must not be empty"

    for manifest in (CLAUDE_MANIFEST, CURSOR_MANIFEST):
        if not manifest.exists():
            continue
        data = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_ver = data.get("version", "")
        assert manifest_ver == version, (
            f"{manifest.name} version '{manifest_ver}' != VERSION '{version}'"
        )
