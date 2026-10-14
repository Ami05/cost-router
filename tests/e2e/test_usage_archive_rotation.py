"""E2E test for OPEN-8: usage_archive rotation must not drop old rows on a
second rotation.

Bug: compact_usage_log()'s archive-rotation step always did
``USAGE_ARCHIVE.rename("usage_archive.1.jsonl")``. On a second rotation this
silently overwrote (and permanently lost) whatever was already in
usage_archive.1.jsonl.

Ruling: retain 3 numbered generations (usage_archive.{1,2,3}.jsonl) via an
oldest-first shift (.2 -> .3, .1 -> .2, current -> .1) instead of a single
blind rename.

This test builds a usage_archive.jsonl with distinguishable content, forces
two consecutive rotations by driving compact_usage_log() with tiny budget/
threshold constants, and asserts the union of the live archive plus its
numbered generations still contains every original distinguishable line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _event(sid: str, ts: str, marker: str) -> str:
    return json.dumps({"session_id": sid, "ts": ts, "type": "tool_call", "marker": marker})


class TestUsageArchiveRotationKeepsGenerations:
    """Verifies usage_archive rotation shifts generations instead of clobbering them."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        import log_compactor

        self.tmp_path = tmp_path
        self.usage_log = tmp_path / "usage.jsonl"
        self.archive = tmp_path / "usage_archive.jsonl"
        self.gen1 = tmp_path / "usage_archive.1.jsonl"
        self.gen2 = tmp_path / "usage_archive.2.jsonl"
        self.gen3 = tmp_path / "usage_archive.3.jsonl"

        monkeypatch.setattr(log_compactor, "USAGE_LOG", self.usage_log)
        monkeypatch.setattr(log_compactor, "USAGE_ARCHIVE", self.archive)
        # Tiny budgets so a couple of distinguishable lines are enough to
        # force both an eviction from usage.jsonl and an archive rotation.
        monkeypatch.setattr(log_compactor, "WORKING_SET_BYTES", 1)
        monkeypatch.setattr(log_compactor, "ARCHIVE_ROTATE_BYTES", 1)
        self.log_compactor = log_compactor
        yield

    def _all_archive_text(self) -> str:
        parts = []
        for p in (self.archive, self.gen1, self.gen2, self.gen3):
            if p.exists():
                parts.append(p.read_text(encoding="utf-8"))
        return "\n".join(parts)

    def test_two_rotations_preserve_all_generations(self):
        # Pre-existing archive content from "before this test started" —
        # stands in for data evicted in some earlier run.
        self.archive.write_text(_event("s0", "2026-01-01T00:00:00+00:00", "GEN0") + "\n")

        # Round 1: usage.jsonl has an old session (s1) and a newer session
        # (s2). With WORKING_SET_BYTES=1, only the newest session is kept;
        # s1 gets evicted to the archive, which then exceeds
        # ARCHIVE_ROTATE_BYTES=1 and rotates GEN0 -> usage_archive.1.jsonl.
        l1 = _event("s1", "2026-01-02T00:00:00+00:00", "L1")
        l2 = _event("s2", "2026-01-03T00:00:00+00:00", "L2")
        self.usage_log.write_text(l1 + "\n" + l2 + "\n", encoding="utf-8")

        self.log_compactor.compact_usage_log()

        assert self.gen1.exists(), "first rotation should have produced usage_archive.1.jsonl"
        assert "GEN0" in self.gen1.read_text(encoding="utf-8")
        assert "L1" in self._all_archive_text()
        assert "L2" in self.usage_log.read_text(encoding="utf-8")

        # Round 2: usage.jsonl now holds s2 (older) plus a brand-new s3.
        # s2 gets evicted, which rotates the archive a second time: the old
        # usage_archive.1.jsonl (GEN0) must shift to .2, not be overwritten.
        l3 = _event("s3", "2026-01-04T00:00:00+00:00", "L3")
        self.usage_log.write_text(l2 + "\n" + l3 + "\n", encoding="utf-8")

        self.log_compactor.compact_usage_log()

        union_text = self._all_archive_text() + "\n" + self.usage_log.read_text(encoding="utf-8")

        for marker in ("GEN0", "L1", "L2", "L3"):
            assert marker in union_text, f"{marker} was lost across rotations"

        # The critical regression check: GEN0 must have survived the SECOND
        # rotation by shifting to generation 2, not been clobbered.
        assert self.gen2.exists(), "second rotation should have shifted gen1 -> gen2"
        assert "GEN0" in self.gen2.read_text(encoding="utf-8"), (
            "GEN0 was overwritten on the second rotation instead of shifted to usage_archive.2.jsonl"
        )
        assert "L1" in self.gen2.read_text(encoding="utf-8")
        assert "L2" in self.gen1.read_text(encoding="utf-8"), (
            "usage_archive.1.jsonl should hold the most recently evicted generation (L2)"
        )
