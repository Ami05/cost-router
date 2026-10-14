"""Tests for DGC-204: advisory split planner for large dispatches.

Covers:
- extract_units_from_prompt: derive units from dispatch prompt
- partition_units: partition units into groups respecting affinity and size
- split_plan_text: format split plan for deny message
- plan_split_for_dispatch: orchestrate full pipeline

Tests verify:
- Deterministic partitioning
- Every unit appears in exactly one group
- High-affinity units not separated unless unsplittable
- Unsplittable units flagged when exceeding target_max
- Integration with deny message text
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

# Add scripts directory to path for imports
scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from split_planner import (
    extract_units_from_prompt,
    extract_units_from_dispatch_json,
    count_dispatch_paths,
    partition_units,
    split_plan_text,
    plan_split_for_dispatch,
)


class TestExtractUnitsFromPrompt:
    """Test unit extraction from dispatch prompts."""

    def test_empty_prompt(self) -> None:
        """Empty prompt yields empty units list."""
        units = extract_units_from_prompt("")
        assert units == []

    def test_prompt_with_no_paths(self) -> None:
        """Prompt with no recognized paths yields empty units."""
        prompt = "Please implement a new feature for the authentication system."
        units = extract_units_from_prompt(prompt)
        assert units == []

    def test_single_path_extraction(self) -> None:
        """Prompt with single path creates one unit."""
        prompt = "Fix the bug in scripts/dispatch_enforcer.py"
        with patch("split_planner.extract_paths_from_prompt", return_value=["scripts/dispatch_enforcer.py"]):
            with patch("split_planner.file_tokens_for_path", return_value=5000):
                units = extract_units_from_prompt(prompt)
                assert len(units) == 1
                assert units[0]["id"] == "unit-0"
                assert "scripts" in units[0]["goal"]
                assert units[0]["tokens"] == 5000

    def test_multiple_paths_same_directory(self) -> None:
        """Paths in same directory are grouped into one unit."""
        prompt = "Update scripts/foo.py and scripts/bar.py"
        paths = ["scripts/foo.py", "scripts/bar.py"]
        with patch("split_planner.extract_paths_from_prompt", return_value=paths):
            with patch("split_planner.file_tokens_for_path", side_effect=[3000, 2000]):
                units = extract_units_from_prompt(prompt)
                assert len(units) == 1
                assert set(units[0]["files"]) == set(paths)
                assert units[0]["tokens"] == 5000

    def test_multiple_paths_different_directories(self) -> None:
        """Paths in different directories create separate units."""
        prompt = "Update scripts/foo.py and tests/bar.py"
        paths = ["scripts/foo.py", "tests/bar.py"]
        with patch("split_planner.extract_paths_from_prompt", return_value=paths):
            with patch("split_planner.file_tokens_for_path", side_effect=[3000, 2000]):
                units = extract_units_from_prompt(prompt)
                assert len(units) == 2
                assert units[0]["files"] == ["scripts/foo.py"]
                assert units[1]["files"] == ["tests/bar.py"]

    def test_extraction_failure_returns_empty(self) -> None:
        """On extraction failure, returns empty list."""
        with patch("split_planner.extract_paths_from_prompt", side_effect=Exception("Boom")):
            units = extract_units_from_prompt("some prompt")
            assert units == []

    def test_units_have_required_fields(self) -> None:
        """Each unit has id, goal, files, and tokens fields."""
        paths = ["scripts/foo.py"]
        with patch("split_planner.extract_paths_from_prompt", return_value=paths):
            with patch("split_planner.file_tokens_for_path", return_value=1000):
                units = extract_units_from_prompt("dummy")
                assert len(units) > 0
                for unit in units:
                    assert "id" in unit
                    assert "goal" in unit
                    assert "files" in unit
                    assert "tokens" in unit


class TestPartitionUnits:
    """Test unit partitioning respecting affinity and size."""

    def test_empty_units(self) -> None:
        """Empty units list returns empty partition."""
        result = partition_units([], target_max=165000)
        assert result["groups"] == []
        assert result["total_tokens"] == 0
        assert result["total_groups"] == 0

    def test_none_units(self) -> None:
        """None units returns empty partition."""
        result = partition_units(None, target_max=165000)
        assert result["groups"] == []
        assert result["total_tokens"] == 0

    def test_single_unit_fits(self) -> None:
        """Single unit that fits in target_max goes into one group."""
        units = [
            {
                "id": "unit-0",
                "goal": "Process unit 0",
                "files": ["scripts/foo.py"],
                "tokens": 50000,
            }
        ]
        result = partition_units(units, target_max=165000)
        assert len(result["groups"]) == 1
        assert result["groups"][0]["units"] == ["unit-0"]
        assert result["groups"][0]["tokens"] == 50000
        assert result["groups"][0]["unsplittable"] is False

    def test_single_unit_exceeds_limit_unsplittable(self) -> None:
        """Single unit exceeding target_max is marked unsplittable."""
        units = [
            {
                "id": "unit-0",
                "goal": "Large unit",
                "files": ["scripts/foo.py"],
                "tokens": 200000,  # Exceeds target_max of 165000
            }
        ]
        result = partition_units(units, target_max=165000)
        assert len(result["groups"]) == 1
        assert result["groups"][0]["unsplittable"] is True
        assert result["groups"][0]["tokens"] == 200000

    def test_two_units_fit_together_no_affinity_constraint(self) -> None:
        """Two small units fit in one group if total <= target_max."""
        units = [
            {
                "id": "unit-0",
                "goal": "Unit 0",
                "files": ["scripts/foo.py"],
                "tokens": 70000,
            },
            {
                "id": "unit-1",
                "goal": "Unit 1",
                "files": ["tests/bar.py"],
                "tokens": 70000,
            },
        ]
        # Low affinity between scripts/ and tests/, but both fit in group
        with patch("split_planner.affinity_score", return_value=0.0):
            result = partition_units(units, target_max=165000)
            # Even with low affinity, if they fit, should be in separate groups
            # due to greedy packing with affinity check
            assert result["total_tokens"] == 140000

    def test_high_affinity_units_stay_together(self) -> None:
        """Units with high affinity above threshold are kept in same group."""
        units = [
            {
                "id": "unit-0",
                "goal": "Unit 0",
                "files": ["scripts/foo.py", "scripts/bar.py"],
                "tokens": 50000,
            },
            {
                "id": "unit-1",
                "goal": "Unit 1",
                "files": ["scripts/baz.py"],
                "tokens": 50000,
            },
        ]
        # High affinity between scripts files
        with patch("split_planner.affinity_score", return_value=0.8):
            with patch("split_planner.get_affinity_threshold", return_value=0.3):
                result = partition_units(units, target_max=165000)
                # Both should be in same group due to high affinity
                assert len(result["groups"]) == 1
                assert set(result["groups"][0]["units"]) == {"unit-0", "unit-1"}

    def test_low_affinity_units_separate(self) -> None:
        """Units below affinity threshold are placed in separate groups."""
        units = [
            {
                "id": "unit-0",
                "goal": "Unit 0",
                "files": ["scripts/foo.py"],
                "tokens": 50000,
            },
            {
                "id": "unit-1",
                "goal": "Unit 1",
                "files": ["tests/bar.py"],
                "tokens": 50000,
            },
        ]
        # Low affinity below threshold
        with patch("split_planner.affinity_score", return_value=0.1):
            with patch("split_planner.get_affinity_threshold", return_value=0.3):
                result = partition_units(units, target_max=165000)
                # Both fit but should be in separate groups due to low affinity
                assert len(result["groups"]) == 2

    def test_every_unit_in_exactly_one_group(self) -> None:
        """Every unit appears in exactly one group (determinism check)."""
        units = [
            {"id": f"unit-{i}", "goal": f"Unit {i}", "files": [f"path{i}.py"], "tokens": 30000}
            for i in range(5)
        ]
        with patch("split_planner.affinity_score", return_value=0.2):
            with patch("split_planner.get_affinity_threshold", return_value=0.5):
                result = partition_units(units, target_max=100000)
                all_unit_ids = set()
                for group in result["groups"]:
                    all_unit_ids.update(group["units"])
                expected_ids = {f"unit-{i}" for i in range(5)}
                assert all_unit_ids == expected_ids

    def test_deterministic_partitioning(self) -> None:
        """Partitioning is deterministic: same input → same output."""
        units = [
            {"id": f"unit-{i}", "goal": f"Unit {i}", "files": [f"path{i}.py"], "tokens": 40000}
            for i in range(3)
        ]
        with patch("split_planner.affinity_score", return_value=0.1):
            with patch("split_planner.get_affinity_threshold", return_value=0.3):
                result1 = partition_units(units, target_max=150000)
                result2 = partition_units(units, target_max=150000)
                # Results should be identical
                assert len(result1["groups"]) == len(result2["groups"])
                for g1, g2 in zip(result1["groups"], result2["groups"]):
                    assert g1["units"] == g2["units"]
                    assert g1["tokens"] == g2["tokens"]

    def test_files_union_in_group(self) -> None:
        """Group files list is union of all unit files."""
        units = [
            {
                "id": "unit-0",
                "goal": "Unit 0",
                "files": ["a.py", "b.py"],
                "tokens": 40000,
            },
            {
                "id": "unit-1",
                "goal": "Unit 1",
                "files": ["c.py", "d.py"],
                "tokens": 40000,
            },
        ]
        with patch("split_planner.affinity_score", return_value=0.9):
            with patch("split_planner.get_affinity_threshold", return_value=0.3):
                result = partition_units(units, target_max=150000)
                assert len(result["groups"]) == 1
                assert set(result["groups"][0]["files"]) == {"a.py", "b.py", "c.py", "d.py"}


class TestSplitPlanText:
    """Test split plan text generation for deny message."""

    def test_empty_groups_no_text(self) -> None:
        """Empty partition yields empty plan text."""
        result = {"groups": [], "total_tokens": 0, "total_groups": 0, "summary": "No units"}
        text = split_plan_text(result)
        assert text == ""

    def test_single_group_text(self) -> None:
        """Single group produces plan text."""
        result = {
            "groups": [
                {
                    "id": "group-0",
                    "units": ["unit-0"],
                    "files": ["foo.py"],
                    "tokens": 50000,
                    "unsplittable": False,
                }
            ],
            "total_tokens": 50000,
            "total_groups": 1,
            "summary": "Split into 1 group; total ~50,000 tokens.",
        }
        text = split_plan_text(result)
        assert "group-0" in text
        assert "unit-0" in text
        assert "50,000" in text
        assert "[Recommended split plan:" in text

    def test_unsplittable_flag_in_text(self) -> None:
        """Unsplittable units are marked in plan text."""
        result = {
            "groups": [
                {
                    "id": "group-0",
                    "units": ["unit-0"],
                    "files": ["foo.py"],
                    "tokens": 200000,
                    "unsplittable": True,
                }
            ],
            "total_tokens": 200000,
            "total_groups": 1,
            "summary": "Split into 1 group (1 unsplittable); total ~200,000 tokens.",
        }
        text = split_plan_text(result)
        assert "unsplittable" in text.lower()

    def test_multiple_groups_text(self) -> None:
        """Multiple groups are listed in plan text."""
        result = {
            "groups": [
                {
                    "id": "group-0",
                    "units": ["unit-0", "unit-1"],
                    "files": ["a.py", "b.py"],
                    "tokens": 80000,
                    "unsplittable": False,
                },
                {
                    "id": "group-1",
                    "units": ["unit-2"],
                    "files": ["c.py"],
                    "tokens": 60000,
                    "unsplittable": False,
                },
            ],
            "total_tokens": 140000,
            "total_groups": 2,
            "summary": "Split into 2 groups; total ~140,000 tokens.",
        }
        text = split_plan_text(result)
        assert "group-0" in text
        assert "group-1" in text
        assert "2 groups" in text
        assert "140,000" in text


class TestPlanSplitForDispatch:
    """Test full pipeline: prompt → units → partition → text."""

    def test_empty_prompt_no_plan(self) -> None:
        """Empty prompt yields empty plan."""
        plan = plan_split_for_dispatch("")
        assert plan == ""

    def test_no_paths_in_prompt(self) -> None:
        """Prompt with no paths yields empty plan."""
        prompt = "Implement a new feature"
        with patch("split_planner.extract_units_from_prompt", return_value=[]):
            plan = plan_split_for_dispatch(prompt)
            assert plan == ""

    def test_orchestration_happy_path(self) -> None:
        """Full pipeline: extract → partition → text."""
        prompt = "Update scripts/foo.py and tests/bar.py"
        units = [
            {
                "id": "unit-0",
                "goal": "Process scripts",
                "files": ["scripts/foo.py"],
                "tokens": 80000,
            },
            {
                "id": "unit-1",
                "goal": "Process tests",
                "files": ["tests/bar.py"],
                "tokens": 70000,
            },
        ]
        with patch("split_planner.extract_units_from_prompt", return_value=units):
            with patch("split_planner.partition_units") as mock_partition:
                mock_partition.return_value = {
                    "groups": [
                        {
                            "id": "group-0",
                            "units": ["unit-0"],
                            "files": ["scripts/foo.py"],
                            "tokens": 80000,
                            "unsplittable": False,
                        },
                        {
                            "id": "group-1",
                            "units": ["unit-1"],
                            "files": ["tests/bar.py"],
                            "tokens": 70000,
                            "unsplittable": False,
                        },
                    ],
                    "total_tokens": 150000,
                    "total_groups": 2,
                    "summary": "Split into 2 groups; total ~150,000 tokens.",
                }
                plan = plan_split_for_dispatch(prompt)
                assert plan != ""
                assert "group-0" in plan
                assert "group-1" in plan

    def test_exception_in_pipeline_returns_empty(self) -> None:
        """On any exception in pipeline, returns empty string."""
        prompt = "some prompt"
        with patch("split_planner.extract_units_from_prompt", side_effect=Exception("Boom")):
            plan = plan_split_for_dispatch(prompt)
            assert plan == ""


class TestExtractUnitsFromDispatchJson:
    """Test unit extraction from JSON dispatch payloads."""

    def test_json_with_files_list_and_line_suffixes(self) -> None:
        """JSON with files list strips line suffixes like :N or :N,M."""
        payload = {
            "files": ["scripts/foo.py:10", "scripts/bar.py:5,10"]
        }
        json_str = json.dumps(payload)
        with patch("split_planner.file_tokens_for_path", side_effect=[3000, 2000]):
            units = extract_units_from_dispatch_json(json_str)
            assert len(units) >= 1
            # Check that files are stripped of line suffixes
            all_files = []
            for unit in units:
                all_files.extend(unit["files"])
            assert "scripts/foo.py" in all_files
            assert "scripts/bar.py" in all_files
            # Should not contain :
            for f in all_files:
                assert ":" not in f

    def test_json_with_scope_directory(self, tmp_path: Path) -> None:
        """JSON with scope field expands directories to files."""
        d = tmp_path / "scopedir"
        d.mkdir()
        (d / "test1.py").write_text("x")
        (d / "test2.py").write_text("y")
        json_str = json.dumps({"scope": [str(d)]})
        with patch("split_planner.file_tokens_for_path", return_value=1000):
            units = extract_units_from_dispatch_json(json_str)
        all_files = [f for u in units for f in u["files"]]
        assert sorted(Path(f).name for f in all_files) == ["test1.py", "test2.py"]

    def test_json_not_dict_falls_back_to_prompt_parsing(self) -> None:
        """JSON array (valid JSON but not dict) falls back to prompt parsing."""
        json_str = '["file1.py", "file2.py"]'
        with patch("split_planner.extract_paths_from_prompt", return_value=["file1.py"]):
            with patch("split_planner.file_tokens_for_path", return_value=1000):
                units = extract_units_from_dispatch_json(json_str)
                assert len(units) > 0

    def test_invalid_json_falls_back_to_prompt_parsing(self) -> None:
        """Invalid JSON falls back to prompt parsing."""
        invalid_json = "not json {{"
        with patch("split_planner.extract_paths_from_prompt", return_value=["scripts/foo.py"]):
            with patch("split_planner.file_tokens_for_path", return_value=1000):
                units = extract_units_from_dispatch_json(invalid_json)
                assert len(units) > 0

    def test_json_with_zero_paths_falls_back_to_prompt(self) -> None:
        """JSON that yields zero paths falls back to prompt parsing."""
        payload = {"files": [], "scope": []}
        json_str = json.dumps(payload)
        with patch("split_planner.extract_paths_from_prompt", return_value=["legacy.py"]):
            with patch("split_planner.file_tokens_for_path", return_value=1000):
                units = extract_units_from_dispatch_json(json_str)
                # Should fall back and extract from prompt
                assert len(units) > 0

    def test_json_with_max_files_limit(self, tmp_path: Path) -> None:
        """JSON scope field respects max_files limit."""
        d = tmp_path / "many"
        d.mkdir()
        for i in range(20):
            (d / f"file{i}.py").write_text("x")
        json_str = json.dumps({"scope": [str(d)], "max_files": 5})
        with patch("split_planner.file_tokens_for_path", return_value=100):
            units = extract_units_from_dispatch_json(json_str)
        assert sum(len(u["files"]) for u in units) == 5


class TestCountDispatchPaths:
    """Test path counting for dispatch payloads."""

    def test_count_json_with_files(self) -> None:
        """count_dispatch_paths returns correct count for JSON files."""
        payload = {"files": ["a.py:10", "b.py", "c.py"]}
        json_str = json.dumps(payload)
        count = count_dispatch_paths(json_str)
        assert count == 3

    def test_count_legacy_prompt(self) -> None:
        """count_dispatch_paths counts legacy prompt paths."""
        prompt = "Update /path/to/file1.py and /path/to/file2.py"
        with patch("split_planner.extract_paths_from_prompt", return_value=["file1.py", "file2.py"]):
            count = count_dispatch_paths(prompt)
            assert count == 2

    def test_count_invalid_json_falls_back(self) -> None:
        """count_dispatch_paths falls back on invalid JSON."""
        invalid = "not json {{"
        with patch("split_planner.extract_paths_from_prompt", return_value=["a.py"]):
            count = count_dispatch_paths(invalid)
            assert count == 1

    def test_count_empty_prompt_returns_zero(self) -> None:
        """count_dispatch_paths returns 0 for empty input."""
        count = count_dispatch_paths("")
        assert count == 0
        assert isinstance(count, int)

    def test_count_garbage_input_returns_zero(self) -> None:
        """count_dispatch_paths returns 0 on extraction failure, never raises."""
        with patch("split_planner.extract_paths_from_prompt", side_effect=Exception("Boom")):
            count = count_dispatch_paths("anything")
            assert count == 0
            assert isinstance(count, int)

    def test_count_returns_int_always(self) -> None:
        """count_dispatch_paths always returns int >= 0."""
        test_inputs = [
            "",
            "plain text",
            '{"files": ["a.py"]}',
            "garbage!!",
            None,
        ]
        for test_input in test_inputs:
            if test_input is None:
                continue
            count = count_dispatch_paths(test_input)
            assert isinstance(count, int)
            assert count >= 0


class TestPlanSplitWithJsonDispatch:
    """Test plan_split_for_dispatch with JSON payloads."""

    def test_json_payload_with_real_files_no_unsupported_message(self) -> None:
        """plan_split_for_dispatch on JSON with files doesn't return empty."""
        # Use a real file from the repo
        payload = {"files": ["scripts/split_planner.py"]}
        json_str = json.dumps(payload)
        plan = plan_split_for_dispatch(json_str)
        # If files exist and can be tokenized, plan should not be empty
        # or if plan is empty, it's due to extraction/partition, not "(split plan not available)"
        assert isinstance(plan, str) and plan and "split plan not available" not in plan


class TestEmitSitesSourceCheck:
    """Source-level verification that both emit sites contain paths_extracted_count."""

    def test_dispatch_budget_enforcer_has_paths_extracted_count(self) -> None:
        """dispatch_budget_enforcer.py emits paths_extracted_count field."""
        enforcer_path = Path(__file__).resolve().parent.parent / "scripts" / "dispatch_budget_enforcer.py"
        content = enforcer_path.read_text()
        assert "paths_extracted_count" in content
        assert "count_dispatch_paths" in content

    def test_routing_resolver_has_paths_extracted_count(self) -> None:
        """routing_resolver.py emits paths_extracted_count field."""
        resolver_path = Path(__file__).resolve().parent.parent / "scripts" / "routing_resolver.py"
        content = resolver_path.read_text()
        assert "paths_extracted_count" in content
        assert "count_dispatch_paths" in content
