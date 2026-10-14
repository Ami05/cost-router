"""DT-08: offline tests for the Tier0 table (model-router.md) and Tier1 catalogue.

Covers catalogue schema, Tier0 token budget, gate safety, dispatch-schema
compatibility and resolver match/fallback/no-match. All inputs are the real
repo files; no network and no ANTHROPIC_API_KEY.
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import enforce_role_gates as erg  # noqa: E402
import policy_gc  # noqa: E402
import routing_resolver as rr  # noqa: E402
from _model_utils import estimate_tokens  # noqa: E402
from schema_validators import SCHEMA_VALIDATORS  # noqa: E402

CATALOGUE = ROOT / "config" / "routing_situations.json"
RULE_FILE = ROOT / ".claude" / "rules" / "model-router.md"

ROLES = {"DIRECT", "FO", "FW", "RB", "FV"}
SPECIALIST_TAGS = {"script-apply", "babysit", "review"}
PRE_DT_TIER0_TOKENS = 435  # Decision Table section tokens (len//4) in production rules/model-router.md before DT, no Tier1 ref
TIER0_ADDED_BUDGET_TOKENS = 500  # decision-table-common.json token_budget: tokens ADDED over the pre-DT section
REQUIRED_ROW_FIELDS = {"id", "q", "f", "r", "i"}
EDIT_AFTER_FO_RB = re.compile(r"\b(FO|RB)\b[^.;|]*\bthen\b[^.;|]*\b(edit|write|shell)\b", re.IGNORECASE)


@pytest.fixture(scope="module")
def catalogue():
    return json.loads(CATALOGUE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rows(catalogue):
    return catalogue["situations"]


@pytest.fixture(scope="module")
def tier0_section():
    text = RULE_FILE.read_text(encoding="utf-8")
    start = text.index("## Decision Table")
    return text[start:]


@pytest.fixture(scope="module")
def tier0_rows(tier0_section):
    return [ln for ln in tier0_section.splitlines() if ln.startswith("|") and " -> " in ln and not ln.startswith("| Situation")]


def test_resolver_and_gc_never_read_api_key():
    for mod in (rr, policy_gc):
        assert "ANTHROPIC_API_KEY" not in Path(mod.__file__).read_text(encoding="utf-8")


# ── catalogue schema ──────────────────────────────────────────────────────────

def test_catalogue_top_level_keys(catalogue):
    assert set(catalogue) == {"families", "policy", "escalation", "situations"}


def test_rows_have_required_fields_and_unique_ids(rows):
    assert rows, "catalogue is empty"
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))
    for r in rows:
        assert REQUIRED_ROW_FIELDS <= set(r), r["id"]


def test_row_roles_and_tags_are_valid(rows):
    for r in rows:
        assert r["r"] in ROLES, r["id"]
        if "v" in r:
            assert r["v"] in ROLES, r["id"]
        if "t" in r:
            assert r["t"] in SPECIALIST_TAGS, r["id"]


def test_row_family_is_declared(catalogue, rows):
    families = set(catalogue["families"])
    for r in rows:
        assert r["f"] in families, r["id"]


def test_row_intent_exists_in_routing_matrix(rows):
    matrix = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["routing_matrix"]
    for r in rows:
        assert r["i"] in matrix, f"{r['id']}: intent {r['i']!r} not in routing_matrix"


# ── Tier0 token budget ────────────────────────────────────────────────────────

def test_tier0_static_baseline_delta(tier0_section):
    """Test: static part (before first cr-auto) minus PRE_DT baseline <= TIER0_ADDED_BUDGET."""
    lines = tier0_section.splitlines()
    # Find first line with cr-auto marker
    auto_idx = next((i for i, ln in enumerate(lines) if policy_gc.AUTO_TAG_PREFIX in ln), len(lines))
    static_part = "\n".join(lines[:auto_idx])

    tokens_used = estimate_tokens(static_part)
    tokens_added = tokens_used - PRE_DT_TIER0_TOKENS
    assert tokens_added <= TIER0_ADDED_BUDGET_TOKENS, (
        f"Static section (with added rows) used {tokens_used} tokens, "
        f"baseline {PRE_DT_TIER0_TOKENS}, delta {tokens_added}, budget {TIER0_ADDED_BUDGET_TOKENS}"
    )


def test_tier0_auto_rows_count(tier0_rows):
    """Test: cr-auto marker rows <= 15."""
    auto_rows = [ln for ln in tier0_rows if policy_gc.AUTO_TAG_PREFIX in ln]
    assert len(auto_rows) <= 15, f"Found {len(auto_rows)} cr-auto rows, max 15 allowed"


def test_tier0_is_last_block(tier0_rows):
    text = RULE_FILE.read_text(encoding="utf-8").rstrip().splitlines()
    assert text[-1] == tier0_rows[-1], "cr-auto appends need the table to be the final block"


# ── gate safety ───────────────────────────────────────────────────────────────

def test_role_regex_accepts_every_role_and_tag(rows):
    for role in ROLES:
        assert erg._ROLE_RE.search(f"Routing: {role} — reason"), role
    for r in rows:
        line = f"Routing: {r['r']}" + (f" +{r['t']}" if "t" in r else "") + " — reason"
        m = erg._ROLE_RE.search(line)
        assert m and m.group(1).upper() == r["r"], r["id"]


def test_no_catalogue_row_edits_after_fo_or_rb(rows):
    for r in rows:
        text = f"{r['r']} {r.get('n', '')}"
        assert not EDIT_AFTER_FO_RB.search(text), r["id"]


def test_no_tier0_row_edits_after_fo_or_rb(tier0_rows):
    for ln in tier0_rows:
        route = ln.split(" -> ", 1)[1]
        assert not EDIT_AFTER_FO_RB.search(route), ln


def test_tier0_routes_start_with_valid_role(tier0_rows):
    for ln in tier0_rows:
        route = ln.split(" -> ", 1)[1].strip()
        m = re.match(r"`?(?:Routing:\s*)?(DIRECT|FO|FW|RB|FV)\b", route)
        assert m, ln


def test_gc_leaves_untagged_rows_alone(tier0_rows):
    tagged = [ln for ln in tier0_rows if policy_gc.AUTO_TAG_PREFIX in ln]
    assert policy_gc.filter_tagged(tier0_rows) == tagged
    auto = "| some tuned situation | -> FW <!-- cr-auto: test -->"
    assert policy_gc.filter_tagged(tier0_rows + [auto]) == tagged + [auto]


# ── dispatches built from rows pass the schema validators ─────────────────────

def _read_dispatch(row):
    return {"worker": "read", "goal": row["q"], "scope": ["scripts/"], "return": ["file:line references"]}


def _shell_dispatch(row):
    return {"worker": "shell", "intent": row["q"], "command": "pytest tests/e2e/ -x -q --timeout=30 -k decision_table", "expect": "exit_code"}


@pytest.mark.parametrize("builder,worker", [(_read_dispatch, "read"), (_shell_dispatch, "shell")])
def test_dispatches_from_rows_pass_schema_validator(rows, builder, worker):
    validator = SCHEMA_VALIDATORS[worker]
    for r in rows:
        payload = builder(r)
        json.loads(json.dumps(payload))
        result = validator.validate(payload)
        assert result.ok, (r["id"], result.violations)


# ── resolver: match / fallback / no-match ─────────────────────────────────────


def test_intent_fallback_returns_routing_dict(rows):
    keys = {"orchestrator", "worker", "explorer", "test_runner"}
    default = rr.get_routing_for_intent("definitely_not_an_intent_zz")
    assert keys <= set(default)
    for intent in {r["i"] for r in rows}:
        assert keys <= set(rr.get_routing_for_intent(intent)), intent
