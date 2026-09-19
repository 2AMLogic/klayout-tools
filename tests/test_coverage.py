"""The common checked-work contract, independent of any producer's verdict."""

import copy
import json
from pathlib import Path

import jsonschema
import pytest

from klayout_tools import coverage


def _full():
    return {
        "schema_version": 1,
        "known": True,
        "checked": ["rule:width"],
        "skipped": [],
        "inapplicable": [],
        "unknown": [],
        "nothing_checked": False,
        "nothing_checked_reasons": [],
    }


@pytest.mark.parametrize(
    ("fields", "state"),
    [
        ({"checked": ["rule:width"]}, "full"),
        (
            {
                "checked": ["rule:width"],
                "skipped": [{"id": "rule:space", "reason": "absent_input_layer"}],
            },
            "partial",
        ),
        (
            {
                "checked": ["rule:width"],
                "inapplicable": [{"id": "gate_role", "reason": "not_applicable"}],
            },
            "full",
        ),
        ({"checked": []}, "zero"),
        (
            {
                "checked": [],
                "unknown": [{"id": "deck", "reason": "execution_not_instrumented"}],
            },
            "unknown",
        ),
    ],
)
def test_common_coverage_states_and_schema(fields, state):
    block = coverage.build_check_coverage(**fields)
    assert coverage.coverage_state({"coverage": block}) == state
    assert block["nothing_checked"] is (state == "zero")
    assert bool(block["nothing_checked_reasons"]) is (state == "zero")
    schema_path = Path(__file__).parents[1] / "docs/schemas/coverage.schema.json"
    schema = json.loads(schema_path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(block, schema)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("known", "true"),
        ("checked", "width"),
        ("checked", [True]),
        ("checked", [""]),
        ("checked", ["rule:width", "rule:width"]),
        ("skipped", [{"id": "rule:space"}]),
        ("skipped", [{"id": "rule:width", "reason": "missing_limit"}]),
        ("skipped", [{"id": "rule:space", "reason": None}]),
        ("inapplicable", None),
        ("unknown", [{"id": "deck", "reason": "unmeasured"}]),
        ("nothing_checked", True),
        ("nothing_checked_reasons", ["no_relevant_checks"]),
    ],
)
def test_malformed_or_contradictory_coverage_cannot_qualify(key, value):
    block = _full()
    block[key] = value
    assert coverage.coverage_state({"coverage": block}) == "malformed"


@pytest.mark.parametrize("key", list(_full()))
def test_versioned_coverage_requires_each_field(key):
    block = _full()
    del block[key]
    assert coverage.coverage_state({"coverage": block}) == "malformed"


def test_unknown_cannot_claim_known_zero_checks():
    block = _full()
    block.update(
        known=False,
        checked=[],
        unknown=[{"id": "deck", "reason": "execution_not_instrumented"}],
    )
    assert coverage.coverage_state({"coverage": block}) == "unknown"
    assert not coverage.coverage_nothing_checked({"coverage": block})
    block["nothing_checked"] = True
    block["nothing_checked_reasons"] = ["no_relevant_checks"]
    assert coverage.coverage_state({"coverage": block}) == "malformed"


@pytest.mark.parametrize(
    "envelope",
    [{}, {"coverage": None}, {"coverage": {"line_pct": 0.0}}],
)
def test_legacy_and_optional_verilator_coverage_are_not_zero_checked(envelope):
    assert coverage.coverage_state(envelope) == "legacy"
    assert not coverage.coverage_nothing_checked(envelope)


def test_legacy_explicit_zero_claim_still_refuses():
    envelope = {"coverage": coverage.build_nothing_checked(["no_delta_rows"])}
    assert coverage.coverage_state(envelope) == "zero"
    assert coverage.coverage_nothing_checked_reasons(envelope) == ["no_delta_rows"]


def test_builder_copies_inputs_and_retains_stable_reasons():
    skipped = [{"id": "limit:gain", "reason": "missing_limit"}]
    original = copy.deepcopy(skipped)
    block = coverage.build_check_coverage(checked=[], skipped=skipped)
    skipped[0]["reason"] = "changed"
    assert block["skipped"] == original
    assert block["nothing_checked_reasons"] == ["missing_limit"]


def test_builder_rejects_ambiguous_work_identity():
    with pytest.raises(ValueError, match="coverage"):
        coverage.build_check_coverage(
            checked=["same"], skipped=[{"id": "same", "reason": "missing_limit"}]
        )


def test_legacy_klayout_lvs_is_not_external_drc_unknown_coverage():
    envelope = {"engine": "klayout", "status": "match", "mismatch_count": 0}
    assert coverage.coverage_state(envelope) == "legacy"
    assert coverage.coverage_refusal_reason(envelope) is None


def test_legacy_klayout_drc_is_unknown_even_without_violation_count():
    # A legacy external DRC envelope is identified by its recognized shape
    # (a "violations" list), not by the optional "violation_count" field --
    # omitting that field must not downgrade "unknown" coverage to "legacy"
    # and thereby bypass refusal.
    envelope = {
        "schema_version": 1,
        "engine": "klayout",
        "status": "clean",
        "violations": [],
        "coverage": {"rules_checked": ["DECLARED"], "nothing_checked": False},
    }
    assert coverage.coverage_state(envelope) == "unknown"
    assert coverage.coverage_refusal_reason(envelope) == "coverage_unknown"


def test_zero_inapplicable_work_names_why_no_assessment_was_requested():
    block = coverage.build_check_coverage(
        checked=[], inapplicable=[{"id": "em", "reason": "no_current_solve_requested"}]
    )
    assert block["nothing_checked_reasons"] == ["no_current_solve_requested"]


def test_zero_reasons_require_stable_codes():
    block = coverage.build_check_coverage(checked=[])
    block["nothing_checked_reasons"] = ["free form prose"]
    assert coverage.coverage_validation_error(block)


def test_work_identity_escapes_user_names_without_collisions():
    assert coverage.work_id("limit", "a/b", "c") != coverage.work_id(
        "limit", "a", "b/c"
    )
