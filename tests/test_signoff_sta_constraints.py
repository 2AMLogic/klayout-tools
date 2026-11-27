"""Opt-in ``require_complete_constraints`` on `sta` evidence (issue #5234).

Kept out of ``test_signoff.py`` (file-size ratchet); reuses its fixtures.
"""

from __future__ import annotations

import pytest

from klayout_tools.signoff import build_signoff
from test_signoff import (
    STA_MULTI_CORNER_CLEAN_ENVELOPE,
    STA_SINGLE_CORNER_CLEAN_ENVELOPE,
    _grade_sta_item_5,
    _write,
)


def _check(count):
    if count is None:
        return {"status": "unavailable", "count": None, "reason": "no query"}
    return {"status": "ok", "count": count, "sample": [], "truncated": False}


def _completeness(status, endpoints=0, registers=0):
    return {
        "status": status,
        "unconstrained_endpoints": _check(endpoints),
        "unclocked_registers": _check(registers),
        "sample_limit": 20,
    }


_CC_COMPLETE = _completeness("complete")
_CC_INCOMPLETE = _completeness("incomplete", endpoints=3, registers=2)


def _sta_with_constraints(blocks: list) -> dict:
    corners = [
        {**corner, "constraint_completeness": block}
        for corner, block in zip(
            STA_MULTI_CORNER_CLEAN_ENVELOPE["corners"], blocks, strict=True
        )
    ]
    return {**STA_MULTI_CORNER_CLEAN_ENVELOPE, "corners": corners}


def test_require_complete_constraints_passes_when_every_corner_complete(tmp_path):
    envelope = _sta_with_constraints([_CC_COMPLETE, _CC_COMPLETE])
    item = _grade_sta_item_5(tmp_path, envelope, require_complete_constraints=True)
    assert item["status"] == "met"


def test_require_complete_constraints_fails_one_incomplete_among_complete(tmp_path):
    envelope = _sta_with_constraints([_CC_COMPLETE, _CC_INCOMPLETE])
    item = _grade_sta_item_5(tmp_path, envelope, require_complete_constraints=True)
    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"
    assert item["detail"]["sta_constraints_incomplete"] == [
        {
            "corner": STA_MULTI_CORNER_CLEAN_ENVELOPE["corners"][1]["corner"],
            "status": "incomplete",
            "unconstrained_endpoints": 3,
            "unclocked_registers": 2,
        }
    ]


def test_require_complete_constraints_unavailable_check_counts_are_null(tmp_path):
    block = _completeness("unknown", endpoints=None, registers=None)
    envelope = _sta_with_constraints([_CC_COMPLETE, block])
    item = _grade_sta_item_5(tmp_path, envelope, require_complete_constraints=True)
    (shortfall,) = item["detail"]["sta_constraints_incomplete"]
    assert shortfall["status"] == "unknown"
    assert shortfall["unconstrained_endpoints"] is None
    assert shortfall["unclocked_registers"] is None


@pytest.mark.parametrize("block", [None, "absent", "junk", {}, {"status": "ok"}])
def test_require_complete_constraints_fails_closed_on_missing_or_malformed(
    tmp_path, block
):
    envelope = _sta_with_constraints([_CC_COMPLETE, block])
    if block == "absent":
        del envelope["corners"][1]["constraint_completeness"]
    item = _grade_sta_item_5(tmp_path, envelope, require_complete_constraints=True)
    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"


def test_require_complete_constraints_flat_single_corner_shape(tmp_path):
    envelope = {
        **STA_SINGLE_CORNER_CLEAN_ENVELOPE,
        "constraint_completeness": _CC_INCOMPLETE,
    }
    item = _grade_sta_item_5(tmp_path, envelope, require_complete_constraints=True)
    assert item["status"] == "unmet"
    assert item["detail"]["sta_constraints_incomplete"][0]["unclocked_registers"] == 2
    ok = {**envelope, "constraint_completeness": _CC_COMPLETE}
    ok_item = _grade_sta_item_5(tmp_path, ok, require_complete_constraints=True)
    assert ok_item["status"] == "met"


def test_incomplete_constraints_unchanged_when_option_not_set(tmp_path):
    envelope = _sta_with_constraints([_CC_INCOMPLETE, _CC_INCOMPLETE])
    assert _grade_sta_item_5(tmp_path, envelope)["status"] == "met"
    item = _grade_sta_item_5(tmp_path, envelope, require_complete_constraints=False)
    assert item["status"] == "met"


def test_sta_check_detail_surfaces_constraint_completeness_status(tmp_path):
    envelope = _sta_with_constraints([_CC_COMPLETE, _CC_INCOMPLETE])
    sta_path = _write(tmp_path, "sta.json", envelope)
    detail = build_signoff([sta_path])["checks"][0]["detail"]
    # Surfaced for the worst-slack corner even with the opt-in off.
    worst = max(envelope["corners"], key=lambda c: -c["worst_slack_ns"])
    expected = worst["constraint_completeness"]["status"]
    assert detail["constraint_completeness_status"] == expected
    flat = {**STA_SINGLE_CORNER_CLEAN_ENVELOPE}
    flat_path = _write(tmp_path, "flat.json", flat)
    flat_detail = build_signoff([flat_path])["checks"][0]["detail"]
    assert flat_detail["constraint_completeness_status"] is None


def test_require_complete_constraints_non_boolean_is_invalid_evidence(tmp_path):
    envelope = _sta_with_constraints([_CC_COMPLETE, _CC_COMPLETE])
    item = _grade_sta_item_5(tmp_path, envelope, require_complete_constraints="yes")
    assert item["status"] == "unmet"
    assert item["reason"] == "invalid_evidence"
