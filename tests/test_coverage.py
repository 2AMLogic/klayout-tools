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


# --------------------------------------------------------------------------- #
# The common partial-success rollup rule (issue #2109, Phase 2 of #1988).
# --------------------------------------------------------------------------- #


def _envelope(state):
    """One envelope per :func:`coverage.coverage_state` value, built through
    the producer helper wherever a producer could actually reach that state.

    ``malformed`` and ``legacy`` are consumer-only rows -- `build_check_coverage`
    validates what it emits and always emits the common fields -- so they are
    spelled directly, as an external or pre-contract artifact would arrive.
    """
    if state == "legacy":
        return {"status": "clean"}
    if state == "malformed":
        return {"coverage": {"schema_version": 1, "known": "yes"}}
    fields = {
        "full": {"checked": ["rule:width"]},
        "partial": {
            "checked": ["rule:width"],
            "skipped": [{"id": "rule:space", "reason": "absent_input_layer"}],
        },
        "zero": {"checked": []},
        "unknown": {
            "checked": [],
            "unknown": [{"id": "deck", "reason": "execution_not_instrumented"}],
        },
    }[state]
    return {"coverage": coverage.build_check_coverage(**fields)}


#: Every row of the decision table, spelled out independently of the module's
#: own :data:`coverage.ROLLUP_TABLE` so a change to either has to be made
#: deliberately in both: (row, reason, exit code, unconditional, complete).
_DECISION_TABLE = [
    ("errored", "check_errored", 4, False, False),
    ("failed", "check_failed", 3, False, False),
    ("malformed", "malformed_coverage", 4, False, False),
    ("unknown", "coverage_unknown", 4, False, False),
    ("zero", "nothing_checked", 4, False, False),
    ("partial", "partial_coverage", 0, False, False),
    ("legacy", "coverage_not_reported", 0, True, False),
    ("full", None, 0, True, True),
]

_COVERAGE_STATES = ["full", "partial", "zero", "unknown", "malformed", "legacy"]


@pytest.mark.parametrize(
    ("row", "reason", "exit_code", "unconditional", "complete"), _DECISION_TABLE
)
def test_rollup_decision_table(row, reason, exit_code, unconditional, complete):
    rollup = coverage.ROLLUP_TABLE[row]
    assert (rollup.result, rollup.reason, rollup.exit_code) == (row, reason, exit_code)
    assert rollup.unconditional is unconditional
    assert rollup.complete is complete
    assert rollup.successful is (exit_code == 0)
    assert rollup.reached_verdict is (exit_code != 4)


@pytest.mark.parametrize("state", _COVERAGE_STATES)
def test_coverage_derived_rows_are_reached_from_a_real_envelope(state):
    """Every coverage state classifies to its own identically-named row."""
    assert coverage.coverage_rollup(_envelope(state)).result == state


@pytest.mark.parametrize("state", _COVERAGE_STATES)
def test_real_failures_and_errors_take_precedence_over_every_state(state):
    envelope = _envelope(state)
    assert coverage.coverage_rollup(envelope, failed=True).result == "failed"
    assert coverage.coverage_rollup(envelope, errored=True).result == "errored"
    # A run that could not complete cannot vouch for what it did emit.
    both = coverage.coverage_rollup(envelope, failed=True, errored=True)
    assert both.result == "errored"


def test_decision_table_covers_every_state_and_has_no_unreachable_row():
    reachable = {
        coverage.coverage_rollup(_envelope(s)).result for s in _COVERAGE_STATES
    }
    assert reachable | {"failed", "errored"} == set(coverage.ROLLUP_TABLE)
    assert {row[0] for row in _DECISION_TABLE} == set(coverage.ROLLUP_TABLE)


@pytest.mark.parametrize("state", ["partial", "zero", "unknown", "malformed"])
def test_no_state_short_of_full_is_ever_an_unconditional_or_complete_success(state):
    rollup = coverage.coverage_rollup(_envelope(state))
    assert not rollup.unconditional
    assert not rollup.complete
    assert coverage.coverage_qualification_reason(_envelope(state)) == rollup.reason


def test_partial_is_a_real_result_that_is_not_an_unconditional_success():
    """The distinguishing row: successful checks *plus* skipped requested
    work. It reached a verdict (unlike zero/unknown/malformed) and exits 0,
    but it is neither unconditional nor complete."""
    rollup = coverage.coverage_rollup(_envelope("partial"))
    assert (rollup.successful, rollup.reached_verdict) == (True, True)
    assert (rollup.unconditional, rollup.complete) == (False, False)
    # The hard gate lets it through; the qualification gate does not.
    assert coverage.coverage_refusal_reason(_envelope("partial")) is None
    assert coverage.coverage_qualification_reason(_envelope("partial")) == (
        "partial_coverage"
    )


def test_inapplicable_work_is_not_a_skipped_request():
    """Work outside this invocation's assessment does not make an otherwise
    complete run partial -- the rule is strict about skips precisely because
    it does not punish a verb for work nobody asked it to do."""
    envelope = {
        "coverage": coverage.build_check_coverage(
            checked=["rule:width"],
            inapplicable=[{"id": "gate_role", "reason": "not_applicable"}],
        )
    }
    rollup = coverage.coverage_rollup(envelope)
    assert rollup.result == "full"
    assert rollup.complete
    assert coverage.coverage_skipped_work(envelope) == []


@pytest.mark.parametrize(
    ("state", "refusal"),
    [
        ("full", None),
        ("legacy", None),
        ("partial", None),
        ("zero", "nothing_checked"),
        ("unknown", "coverage_unknown"),
        ("malformed", "malformed_coverage"),
    ],
)
def test_hard_refusal_gate_fires_only_where_no_verdict_was_reached(state, refusal):
    assert coverage.coverage_refusal_reason(_envelope(state)) == refusal


def test_legacy_policy_is_recorded_not_silent():
    """Missing common coverage keeps grading by the verb-specific rules that
    always governed it -- and is never counted as proof of completeness."""
    envelope = _envelope("legacy")
    rollup = coverage.coverage_rollup(envelope)
    assert rollup.result == "legacy"
    assert rollup.unconditional and not rollup.complete
    assert rollup.reason == "coverage_not_reported"
    assert coverage.coverage_refusal_reason(envelope) is None
    assert coverage.coverage_qualification_reason(envelope) is None


@pytest.mark.parametrize(
    "envelope",
    [
        {"coverage": {"line_pct": 91.2, "branch_pct": 74.0}},
        {"coverage": {"line_pct": 100.0}},
    ],
)
def test_optional_code_coverage_percentages_are_not_requested_check_coverage(envelope):
    """Verilator's percentages share the key name and nothing else: they are
    never read as a complete, partial or zero *checked-work* claim."""
    rollup = coverage.coverage_rollup(envelope)
    assert rollup.result == "legacy"
    assert not rollup.complete
    assert coverage.coverage_skipped_work(envelope) == []


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, ["pass", "pass_partial", "not_checked"]),
        ({"success": "clean"}, ["clean", "clean_partial", "not_checked"]),
        (
            {"success": "clean", "partial": "clean_but_incomplete"},
            ["clean", "clean_but_incomplete", "not_checked"],
        ),
    ],
)
def test_rollup_status_maps_rows_onto_a_verbs_own_vocabulary(kwargs, expected):
    statuses = [
        coverage.rollup_status(coverage.ROLLUP_TABLE[row], **kwargs)
        for row in ("full", "partial", "zero")
    ]
    assert statuses == expected


def test_rollup_status_fixes_the_shared_tokens_every_adapter_must_reuse():
    table = coverage.ROLLUP_TABLE
    assert coverage.rollup_status(table["unknown"]) == "coverage_unknown"
    assert coverage.rollup_status(table["malformed"]) == "malformed_coverage"
    assert coverage.rollup_status(table["failed"], failure="violations") == "violations"
    assert coverage.rollup_status(table["errored"]) == "error"
    # A legacy envelope read by a consumer still renders the verb's own
    # success word -- the row is not a new status, it is "no claim made".
    assert coverage.rollup_status(table["legacy"], success="clean") == "clean"


def test_skipped_work_is_quoted_verbatim_and_cannot_alias_the_source():
    envelope = _envelope("partial")
    skipped = coverage.coverage_skipped_work(envelope)
    assert skipped == [{"id": "rule:space", "reason": "absent_input_layer"}]
    skipped[0]["reason"] = "mutated"
    assert coverage.coverage_skipped_work(envelope)[0]["reason"] == (
        "absent_input_layer"
    )


def test_skipped_list_and_checked_count_stay_consistent_with_the_row():
    """Count/list consistency: the partial row exists exactly when there is
    at least one checked item and at least one skipped request."""
    for checked, skipped, expected in (
        (["a"], [{"id": "b", "reason": "missing_limit"}], "partial"),
        (["a"], [], "full"),
        ([], [{"id": "b", "reason": "missing_limit"}], "zero"),
    ):
        block = coverage.build_check_coverage(checked=checked, skipped=skipped)
        envelope = {"coverage": block}
        assert coverage.coverage_rollup(envelope).result == expected
        assert len(coverage.coverage_skipped_work(envelope)) == (
            len(skipped) if expected == "partial" else 0
        )


@pytest.mark.parametrize(
    "block",
    [
        "partial",
        ["rule:width"],
        {"schema_version": 1, "known": True, "checked": ["a"], "skipped": "none"},
        {**_full(), "checked": [], "nothing_checked": False},
    ],
)
def test_malformed_or_contradictory_evidence_cannot_reach_a_verdict(block):
    envelope = {"coverage": block}
    rollup = coverage.coverage_rollup(envelope)
    assert rollup.result == "malformed"
    assert not rollup.reached_verdict
    assert rollup.exit_code == 4
    assert coverage.coverage_skipped_work(envelope) == []
