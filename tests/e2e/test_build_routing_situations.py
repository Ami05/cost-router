"""Tier1 catalogue is reproducible from decision-table source + overlay (offline, real files)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_routing_situations as brs  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (brs.SOURCE.exists() and brs.OVERLAY.exists()),
    reason="knowledge/ inputs are dev-only and not synced to the plugin",
)


@pytest.fixture(scope="module")
def inputs():
    return brs.load_source_rows(), json.loads(brs.OVERLAY.read_text(encoding="utf-8"))


def test_catalogue_matches_build_output(inputs):
    assert brs.CATALOGUE.read_text(encoding="utf-8") == brs.build(*inputs)


def test_build_is_valid_json_with_source_order(inputs):
    cat = json.loads(brs.build(*inputs))
    src_ids = [r["id"] for r in inputs[0] if r["id"] not in set(inputs[1]["dropped"])]
    assert [r["id"] for r in cat["situations"]] == src_ids


def test_uncovered_source_row_is_rejected(inputs):
    rows, overlay = inputs
    broken = {**overlay, "rows": {k: v for k, v in overlay["rows"].items() if k != "S001"}}
    with pytest.raises(ValueError, match="S001"):
        brs.build(rows, broken)


def test_unknown_overlay_row_is_rejected(inputs):
    rows, overlay = inputs
    broken = {**overlay, "rows": {**overlay["rows"], "S999": {"r": "DIRECT", "i": "qa_simple"}}}
    with pytest.raises(ValueError, match="S999"):
        brs.build(rows, broken)
