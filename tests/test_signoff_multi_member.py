"""Issue #5031: items 3 (DRC) and 4 (LVS) accept per-cell report lists and
state how many members were covered."""

from __future__ import annotations

from klayout_tools.signoff import build_tier_report
from test_signoff import (
    DRC_CLEAN_ENVELOPE,
    DRC_VIOLATIONS_ENVELOPE,
    LVS_MATCH_POWER_MATCH_ENVELOPE,
    LVS_MISMATCH_ENVELOPE,
    SIM_PASS_ENVELOPE,
    _write,
)


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
