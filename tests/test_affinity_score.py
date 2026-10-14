"""Tests for DGC-201: context-affinity scoring for task clustering.

Tests that:
- affinity_score combines Jaccard similarity and shared directory prefix
- Scores are deterministic on fixtures
- Threshold is measurable (default 0.3)
- Existing grouping tests remain unchanged
- High-affinity units are not separated
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Add scripts directory to path for imports
scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from affinity import (
    affinity_score,
    is_high_affinity,
    get_affinity_threshold,
    _jaccard_similarity,
    _shared_dir_prefix,
)


class TestJaccardSimilarity:
    """Test Jaccard similarity calculation."""

    def test_identical_sets(self) -> None:
        """Identical sets have Jaccard = 1.0."""
        set_a = {"scripts/foo.py", "scripts/bar.py"}
        set_b = {"scripts/foo.py", "scripts/bar.py"}
        assert _jaccard_similarity(set_a, set_b) == 1.0

    def test_disjoint_sets(self) -> None:
        """Disjoint sets have Jaccard = 0.0."""
        set_a = {"scripts/foo.py"}
        set_b = {"tests/bar.py"}
        assert _jaccard_similarity(set_a, set_b) == 0.0

    def test_partial_overlap(self) -> None:
        """Partial overlap: |A ∩ B| / |A ∪ B|."""
        set_a = {"a", "b", "c"}
        set_b = {"b", "c", "d"}
        # A ∩ B = {b, c}, |intersection| = 2
        # A ∪ B = {a, b, c, d}, |union| = 4
        # Jaccard = 2/4 = 0.5
        assert _jaccard_similarity(set_a, set_b) == 0.5

    def test_empty_sets(self) -> None:
        """Empty sets are considered identical (vacuous truth)."""
        assert _jaccard_similarity(set(), set()) == 1.0

    def test_one_empty_set(self) -> None:
        """One empty set and one non-empty: Jaccard = 0."""
        set_a = {"a", "b"}
        set_b = set()
        assert _jaccard_similarity(set_a, set_b) == 0.0


class TestSharedDirPrefix:
    """Test shared directory prefix calculation."""

    def test_identical_files_same_dir(self) -> None:
        """Files in same directory have shared prefix."""
        files_a = ["scripts/foo.py"]
        files_b = ["scripts/bar.py"]
        # Both in "scripts" (1 shared part), both have 1 part before filename
        # normalized: 1/1 = 1.0
        result = _shared_dir_prefix(files_a, files_b)
        assert result == 1.0

    def test_different_top_level_dirs(self) -> None:
        """Files in different top-level directories."""
        files_a = ["scripts/foo.py"]
        files_b = ["tests/bar.py"]
        # No shared directory prefix
        assert _shared_dir_prefix(files_a, files_b) == 0.0

    def test_nested_dirs_partial_overlap(self) -> None:
        """Nested directories with partial prefix match."""
        files_a = ["scripts/dispatch/foo.py"]  # parts: (scripts, dispatch)
        files_b = ["scripts/bar.py"]           # parts: (scripts,)
        # Shared prefix: "scripts" (1 part)
        # Deeper file has 2 parts, so normalized = 1/2 = 0.5
        result = _shared_dir_prefix(files_a, files_b)
        assert result == 0.5

    def test_multiple_file_pairs(self) -> None:
        """Multiple files: take maximum shared prefix."""
        files_a = ["scripts/foo.py", "tests/unit/baz.py"]
        files_b = ["scripts/bar.py", "tests/unit/qux.py"]
        # (scripts/foo, scripts/bar) -> 1/1 = 1.0 ✓
        # (tests/unit/baz, tests/unit/qux) -> 2/2 = 1.0 ✓
        # Maximum = 1.0
        result = _shared_dir_prefix(files_a, files_b)
        assert result == 1.0

    def test_empty_file_list(self) -> None:
        """Empty file lists return 0."""
        assert _shared_dir_prefix([], ["scripts/foo.py"]) == 0.0
        assert _shared_dir_prefix(["scripts/foo.py"], []) == 0.0
        assert _shared_dir_prefix([], []) == 0.0


class TestAffinityScore:
    """Test overall affinity scoring."""

    def test_identical_file_sets_same_dir(self) -> None:
        """Units with identical files in same dir: high affinity."""
        unit_a = {"files": ["scripts/foo.py", "scripts/bar.py"]}
        unit_b = {"files": ["scripts/foo.py", "scripts/bar.py"]}
        # Jaccard = 1.0, shared_prefix = 1.0
        # Score = 0.7*1.0 + 0.3*1.0 = 1.0
        score = affinity_score(unit_a, unit_b)
        assert score == 1.0

    def test_disjoint_files(self) -> None:
        """Units with disjoint files: low affinity."""
        unit_a = {"files": ["scripts/foo.py"]}
        unit_b = {"files": ["tests/bar.py"]}
        # Jaccard = 0.0, shared_prefix = 0.0
        # Score = 0.7*0.0 + 0.3*0.0 = 0.0
        score = affinity_score(unit_a, unit_b)
        assert score == 0.0

    def test_partial_overlap_in_same_dir(self) -> None:
        """Partial file overlap in same directory."""
        unit_a = {"files": ["scripts/foo.py", "scripts/bar.py"]}
        unit_b = {"files": ["scripts/bar.py", "scripts/baz.py"]}
        # Jaccard({foo, bar}, {bar, baz}) = 1/3 ≈ 0.333
        # shared_prefix = 1.0 (all in scripts)
        # Score = 0.7*0.333 + 0.3*1.0 ≈ 0.533
        score = affinity_score(unit_a, unit_b)
        assert 0.52 < score < 0.54

    def test_file_paths_key(self) -> None:
        """Units with file_paths instead of files key."""
        unit_a = {"file_paths": ["scripts/foo.py"]}
        unit_b = {"file_paths": ["scripts/foo.py"]}
        score = affinity_score(unit_a, unit_b)
        assert score == 1.0

    def test_none_units(self) -> None:
        """None units have zero affinity."""
        assert affinity_score(None, {"files": ["scripts/foo.py"]}) == 0.0
        assert affinity_score({"files": ["scripts/foo.py"]}, None) == 0.0
        assert affinity_score(None, None) == 0.0

    def test_invalid_files_key(self) -> None:
        """Units with non-list files default to empty."""
        unit_a = {"files": "not a list"}
        unit_b = {"files": ["scripts/foo.py"]}
        score = affinity_score(unit_a, unit_b)
        assert score == 0.0

    def test_empty_files_list(self) -> None:
        """Units with empty file lists."""
        unit_a = {"files": []}
        unit_b = {"files": []}
        # Empty sets: Jaccard = 1.0, shared_prefix = 0.0
        # Score = 0.7*1.0 + 0.3*0.0 = 0.7
        score = affinity_score(unit_a, unit_b)
        assert score == 0.7

    def test_score_bounds(self) -> None:
        """Affinity scores always in [0, 1]."""
        test_cases = [
            ({"files": ["a", "b", "c"]}, {"files": ["x", "y", "z"]}),
            ({"files": ["a"]}, {"files": ["a"]}),
            (None, {"files": ["a"]}),
            ({}, {}),
        ]
        for unit_a, unit_b in test_cases:
            score = affinity_score(unit_a, unit_b)
            assert 0.0 <= score <= 1.0, f"Score out of bounds: {score}"


class TestAffinityThreshold:
    """Test threshold configuration."""

    def test_default_threshold(self) -> None:
        """Default threshold is 0.3."""
        with patch("affinity._get_affinity_config", return_value={}):
            assert get_affinity_threshold() == 0.3

    def test_config_threshold(self) -> None:
        """Threshold from config."""
        with patch("affinity._get_affinity_config", return_value={"threshold": 0.5}):
            assert get_affinity_threshold() == 0.5

    def test_invalid_threshold_type(self) -> None:
        """Invalid threshold type defaults to 0.3."""
        with patch("affinity._get_affinity_config", return_value={"threshold": "invalid"}):
            assert get_affinity_threshold() == 0.3

    def test_threshold_out_of_bounds(self) -> None:
        """Threshold outside [0, 1] defaults to 0.3."""
        with patch("affinity._get_affinity_config", return_value={"threshold": 1.5}):
            assert get_affinity_threshold() == 0.3
        with patch("affinity._get_affinity_config", return_value={"threshold": -0.1}):
            assert get_affinity_threshold() == 0.3


class TestHighAffinityCheck:
    """Test is_high_affinity predicate."""

    def test_above_threshold(self) -> None:
        """Units above threshold are high affinity."""
        unit_a = {"files": ["scripts/foo.py"]}
        unit_b = {"files": ["scripts/foo.py"]}
        # Score = 1.0, threshold = 0.3 (default)
        with patch("affinity.get_affinity_threshold", return_value=0.3):
            assert is_high_affinity(unit_a, unit_b) is True

    def test_below_threshold(self) -> None:
        """Units below threshold are not high affinity."""
        unit_a = {"files": ["scripts/foo.py"]}
        unit_b = {"files": ["tests/bar.py"]}
        # Score = 0.0, threshold = 0.3 (default)
        with patch("affinity.get_affinity_threshold", return_value=0.3):
            assert is_high_affinity(unit_a, unit_b) is False

    def test_at_threshold_boundary(self) -> None:
        """At exactly threshold: considered high affinity (>=)."""
        unit_a = {"files": ["scripts/a", "scripts/b"]}
        unit_b = {"files": ["scripts/b", "scripts/c"]}
        # Jaccard = 1/3, shared_prefix = 1.0
        # Score ≈ 0.7*0.333 + 0.3*1.0 ≈ 0.533
        score = affinity_score(unit_a, unit_b)
        with patch("affinity.get_affinity_threshold", return_value=score):
            assert is_high_affinity(unit_a, unit_b) is True


class TestDeterminism:
    """Test that affinity scores are deterministic."""

    def test_repeated_calls_same_result(self) -> None:
        """Same units produce identical scores across calls."""
        unit_a = {"files": ["scripts/dispatch/foo.py", "scripts/context_estimator.py"]}
        unit_b = {"files": ["scripts/dispatch/bar.py", "scripts/bws_enforcer.py"]}

        scores = [affinity_score(unit_a, unit_b) for _ in range(5)]
        assert all(s == scores[0] for s in scores), "Scores not deterministic"

    def test_order_independent(self) -> None:
        """File order within units doesn't affect score."""
        unit_a1 = {"files": ["a", "b", "c"]}
        unit_a2 = {"files": ["c", "b", "a"]}
        unit_b = {"files": ["b", "c", "d"]}

        score1 = affinity_score(unit_a1, unit_b)
        score2 = affinity_score(unit_a2, unit_b)
        assert score1 == score2, "File order should not affect score"

    def test_symmetry(self) -> None:
        """affinity_score(a, b) == affinity_score(b, a)."""
        unit_a = {"files": ["scripts/foo.py", "tests/bar.py"]}
        unit_b = {"files": ["scripts/baz.py"]}

        score_ab = affinity_score(unit_a, unit_b)
        score_ba = affinity_score(unit_b, unit_a)
        assert score_ab == score_ba, "Affinity should be symmetric"
