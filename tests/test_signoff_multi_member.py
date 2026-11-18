"""Issue #5031: items 3 (DRC) and 4 (LVS) accept per-cell report lists and
state how many members were covered."""

from __future__ import annotations

from klayout_tools.signoff import build_tier_report
from test_signoff import (
    DRC_CLEAN_ENVELOPE,
    DRC_VIOLATIONS_ENVELOPE,
    FUNCTIONAL_VERIFICATION_PASS_ENVELOPE,
    LVS_MATCH_POWER_MATCH_ENVELOPE,
    LVS_MISMATCH_ENVELOPE,
    SIM_PASS_ENVELOPE,
    _write,
)
from test_signoff_derived_rows import _LIM, TWO_ROW_FAIL_SIM, _meas, _tagged_sim


def _item(item_id, entry):
    manifest = {
        "block": "lib",
        "kind": "analog",
        "evidence": {} if entry is None else {str(item_id): entry},
    }
    report = build_tier_report(manifest)
    return next(i for i in report["items"] if i["id"] == item_id)


def test_drc_all_pass_is_met_with_full_coverage(tmp_path):
    paths = [_write(tmp_path, f"{n}.json", DRC_CLEAN_ENVELOPE) for n in "abc"]
    item = _item(3, paths)
    assert item["status"] == "met"
    assert (item["members_covered"], item["members_total"]) == (3, 3)
    assert len(item["citations"]) == 3


def test_drc_one_failing_member_is_unmet_named_and_counted(tmp_path):
    a = _write(tmp_path, "a.json", DRC_CLEAN_ENVELOPE)
    b = _write(tmp_path, "b.json", DRC_VIOLATIONS_ENVELOPE)
    c = _write(tmp_path, "c.json", DRC_CLEAN_ENVELOPE)
    item = _item(3, [a, b, c])
    assert item["status"] == "unmet"
    assert item["detail"]["failed_citations"] == [b]
    assert (item["members_covered"], item["members_total"]) == (2, 3)


def test_drc_malformed_member_and_empty_list_are_invalid(tmp_path):
    a = _write(tmp_path, "a.json", DRC_CLEAN_ENVELOPE)
    bad = _item(3, [a, 42])
    assert bad["reason"] == "invalid_evidence"
    assert (bad["members_covered"], bad["members_total"]) == (0, 2)
    assert _item(3, [])["reason"] == "invalid_evidence"


def test_drc_wrong_kind_member_is_unmet(tmp_path):
    a = _write(tmp_path, "a.json", DRC_CLEAN_ENVELOPE)
    sim = _write(tmp_path, "sim.json", SIM_PASS_ENVELOPE)
    item = _item(3, [a, sim])
    assert item["status"] == "unmet"
    assert item["reason"] == "wrong_kind"
    assert item["members_covered"] == 1


def test_drc_mixed_path_and_object_members(tmp_path):
    a = _write(tmp_path, "a.json", DRC_CLEAN_ENVELOPE)
    b = _write(tmp_path, "b.json", DRC_CLEAN_ENVELOPE)
    item = _item(3, [a, {"file": b}])
    assert item["status"] == "met"
    assert item["members_total"] == 2


def test_drc_duplicate_members_are_each_counted(tmp_path):
    a = _write(tmp_path, "a.json", DRC_CLEAN_ENVELOPE)
    item = _item(3, [a, a])
    assert item["status"] == "met"
    assert (item["members_covered"], item["members_total"]) == (2, 2)


def test_lvs_list_reports_members(tmp_path):
    a = _write(tmp_path, "a.json", LVS_MATCH_POWER_MATCH_ENVELOPE)
    b = _write(tmp_path, "b.json", LVS_MISMATCH_ENVELOPE)
    ok = _item(4, [a, a])
    assert (ok["members_covered"], ok["members_total"]) == (2, 2)
    bad = _item(4, [a, b])
    assert bad["status"] == "unmet"
    assert (bad["members_covered"], bad["members_total"]) == (1, 2)


def test_single_entry_output_has_no_member_fields(tmp_path):
    drc = _item(3, _write(tmp_path, "a.json", DRC_CLEAN_ENVELOPE))
    lvs = _item(4, _write(tmp_path, "b.json", LVS_MATCH_POWER_MATCH_ENVELOPE))
    for item in (drc, lvs):
        assert item["status"] == "met"
        assert "members_total" not in item
        assert "members_covered" not in item
        assert "citations_graded" not in item


def test_layout_item2_list_all_pass_is_met_with_coverage(tmp_path):
    """Issue #5044: item 2 accepts a list of native envelopes."""
    a = _write(tmp_path, "a.json", DRC_CLEAN_ENVELOPE)
    b = _write(tmp_path, "b.json", LVS_MATCH_POWER_MATCH_ENVELOPE)
    item = _item(2, [a, b])
    assert item["status"] == "met"
    assert (item["members_covered"], item["members_total"]) == (2, 2)
    assert len(item["citations"]) == 2


def test_layout_item2_failing_member_is_unmet_and_named(tmp_path):
    a = _write(tmp_path, "a.json", DRC_CLEAN_ENVELOPE)
    b = _write(tmp_path, "b.json", DRC_VIOLATIONS_ENVELOPE)
    item = _item(2, [a, b])
    assert item["status"] == "unmet"
    assert item["detail"]["failed_citations"] == [b]
    assert (item["members_covered"], item["members_total"]) == (1, 2)


def test_layout_item2_edge_cases(tmp_path):
    a = _write(tmp_path, "a.json", DRC_CLEAN_ENVELOPE)
    assert _item(2, [])["reason"] == "invalid_evidence"
    bad = _item(2, [a, 42])
    assert bad["reason"] == "invalid_evidence"
    assert (bad["members_covered"], bad["members_total"]) == (0, 2)
    dup = _item(2, [a, a])
    assert dup["status"] == "met"
    assert dup["members_total"] == 2
    single = _item(2, [a])
    assert single["status"] == "met"
    assert single["members_total"] == 1


def test_layout_item2_single_entry_unchanged(tmp_path):
    item = _item(2, _write(tmp_path, "a.json", DRC_CLEAN_ENVELOPE))
    assert item["status"] == "met"
    assert "members_total" not in item
    assert "citations_graded" not in item


# Issue #6027: a `check_failed` `sim` citation's envelope is captured for the
# row credit only when it survives the trust gates (`_maybe_record_failed_sim`
# in `signoff_derived_rows`): a stale pin, a vacuous verdict or an
# unverifiable pin still grades `check_failed`, but credits nothing. These
# live beside the member-grading tests for size-ratchet room, not scope.


def _graded_item(item_id, entry, spec_rows):
    manifest = {
        "block": "lib",
        "kind": "analog",
        "evidence": {str(item_id): entry},
        "spec_rows": {str(item_id): spec_rows},
    }
    report = build_tier_report(manifest)
    return next(i for i in report["items"] if i["id"] == item_id)


def _failing_item5(entry, spec_rows):
    return _graded_item(5, entry, spec_rows)


def test_failing_stale_pinned_sim_credits_nothing(tmp_path):
    bad = _write(tmp_path, "bad.json", TWO_ROW_FAIL_SIM)
    item = _failing_item5(
        [{"file": bad, "rows": ["tdec"], "content_hash": "sha256:stale-revision"}],
        [{"id": "tdec"}],
    )

    # The failure outranks the pin (issue #5598: the pin is disclosed beside
    # the verdict, never as the verdict) -- but the pin refusal keeps the
    # envelope out of the credit, so the passing row stays in `failed_rows`.
    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"
    assert item["detail"]["failed_rows"] == ["tdec"]
    assert item["detail"]["row_sources"] == {}


def test_failing_nothing_checked_sim_credits_nothing(tmp_path):
    bad = _write(
        tmp_path,
        "bad.json",
        {**TWO_ROW_FAIL_SIM, "coverage": {"nothing_checked": True}},
    )
    item = _failing_item5([{"file": bad, "rows": ["tdec"]}], [{"id": "tdec"}])

    # Same verdict as a trustworthy failure (issue #5479 orders the trust
    # gates after the pass/fail check) -- but a vacuous-verdict envelope is
    # not evidence for any row, so nothing is credited.
    assert item["reason"] == "check_failed"
    assert item["detail"]["failed_rows"] == ["tdec"]
    assert item["detail"]["row_sources"] == {}


def test_failing_unverifiable_pin_sim_credits_nothing(tmp_path):
    bare = {k: v for k, v in TWO_ROW_FAIL_SIM.items() if k != "provenance"}
    bad = _write(tmp_path, "bad.json", bare)
    item = _failing_item5(
        [{"file": bad, "rows": ["tdec"], "content_hash": "sha256:pinned"}],
        [{"id": "tdec"}],
    )

    # No `provenance.input.content_hash` to compare the pin against -- the
    # provenance refusal records nothing, like the stale pin above.
    assert item["reason"] == "check_failed"
    assert item["detail"]["failed_rows"] == ["tdec"]
    assert item["detail"]["row_sources"] == {}


# Issue #6027: the credit is for a citation the item *accepts*. A passing
# tagged envelope of a kind the item rejects as `wrong_kind` grades
# `unmet`/`wrong_kind` -- and must not leave its rows reading `derived`
# (machine-checked) in `detail`, absent from both `failed_rows` and
# `uncovered_rows`.


def test_wrong_kind_sim_cited_for_item6_credits_nothing(tmp_path):
    # Item 6 accepts only `yield`; `klt sim` is the one verb whose envelope
    # carries `measurements[].spec_row`, so citing it here is a reachable
    # manifest mistake.
    sim = _write(tmp_path, "sim.json", _tagged_sim([_meas("t", "A", limits=_LIM)]))
    item = _graded_item(6, [{"file": sim, "rows": ["A"]}], [{"id": "A"}])

    assert item["status"] == "unmet"
    assert item["reason"] == "wrong_kind"
    assert item["detail"]["failed_rows"] == ["A"]
    assert item["detail"]["row_sources"] == {}
    assert item["detail"]["uncovered_rows"] == []


def test_wrong_kind_functional_verification_cited_for_item5_credits_nothing(tmp_path):
    # Item 5 analog accepts `{sim, model-run}`, not `functional-verification`.
    fv = _write(
        tmp_path,
        "fv.json",
        {
            **FUNCTIONAL_VERIFICATION_PASS_ENVELOPE,
            "measurements": [_meas("t", "A", limits=_LIM)],
        },
    )
    item = _graded_item(5, [{"file": fv, "rows": ["A"]}], [{"id": "A"}])

    assert item["status"] == "unmet"
    assert item["reason"] == "wrong_kind"
    assert item["detail"]["failed_rows"] == ["A"]
    assert item["detail"]["row_sources"] == {}
    assert item["detail"]["uncovered_rows"] == []


def test_opt_in_model_run_cited_for_item6_credits_nothing(tmp_path):
    # The opt-in downgrade (`_OPT_IN_KIND_ITEMS`) is a second route to
    # `wrong_kind`: `model-run` is item 5 only, so item 6 refuses it.
    model_run = _write(
        tmp_path,
        "model.json",
        {
            "schema_version": 1,
            "kind": "model-run",
            "status": "pass",
            "method": "linear small-signal model, ideal supplies",
            "measurements": [_meas("t", "A", limits=_LIM)],
        },
    )
    item = _graded_item(6, [{"file": model_run, "rows": ["A"]}], [{"id": "A"}])

    assert item["status"] == "unmet"
    assert item["reason"] == "wrong_kind"
    assert item["detail"]["failed_rows"] == ["A"]
    assert item["detail"]["row_sources"] == {}
    assert item["detail"]["uncovered_rows"] == []
