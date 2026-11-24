"""`sta` envelope ``status`` guard and opt-in ``require_routed_geometry`` (issue #7711).

Kept out of ``test_signoff.py`` (file-size ratchet); reuses its fixtures.
"""

from __future__ import annotations

import pytest

from klayout_tools.signoff import build_signoff
from klayout_tools.signoff_sta_geometry import (
    sta_geometry_refusal,
    sta_status_detail,
    sta_status_ok,
)
from test_signoff import (
    STA_MULTI_CORNER_CLEAN_ENVELOPE,
    STA_SINGLE_CORNER_CLEAN_ENVELOPE,
    _grade_sta_item_5,
    _write,
)

_MULTI = STA_MULTI_CORNER_CLEAN_ENVELOPE
_FLAT = STA_SINGLE_CORNER_CLEAN_ENVELOPE
_ENVELOPES = pytest.mark.parametrize("base", [_MULTI, _FLAT], ids=["multi", "flat"])
_ESTIMATES = ("placement_estimate", "netlist_estimate", "synthesized")


def _sta_citation(item: dict) -> dict:
    return next(c for c in item["citations"] if c["kind"] == "sta")


# --- status guard (default behaviour, no opt-in) ---------------------------


@_ENVELOPES
def test_routed_ok_baseline_stays_met(tmp_path, base):
    assert base["status"] == "ok" and base["geometry_source"] == "routed"
    item = _grade_sta_item_5(tmp_path, base)
    assert item["status"] == "met"
    assert item["reason"] is None


@_ENVELOPES
@pytest.mark.parametrize("status", ["error", "failed", "pass", "", "OK"])
def test_non_ok_status_grades_unmet_without_any_opt_in(tmp_path, base, status):
    item = _grade_sta_item_5(tmp_path, {**base, "status": status})
    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"
    assert item["detail"]["sta_status"] == status


@_ENVELOPES
def test_absent_or_non_string_status_is_refused_as_before(tmp_path, base):
    # `status: str` is a required key of the `sta` envelope shape, so these
    # never reach the status guard: they were `unrecognized_envelope` before
    # #7711 and still are (the guard changes no verdict here).
    absent = {k: v for k, v in base.items() if k != "status"}
    for envelope in (absent, {**base, "status": None}, {**base, "status": 0}):
        item = _grade_sta_item_5(tmp_path, envelope)
        assert item["status"] == "unmet"
        assert item["reason"] == "unrecognized_envelope"


@pytest.mark.parametrize(
    ("envelope", "ok"),
    [
        ({}, True),  # absent: not failed by the guard itself
        ({"status": "ok"}, True),
        ({"status": "error"}, False),
        ({"status": None}, False),
        ({"status": 0}, False),
        ({"status": "OK"}, False),
    ],
)
def test_status_guard_predicate(envelope, ok):
    assert sta_status_ok(envelope) is ok
    assert bool(sta_status_detail("sta", envelope)) is (not ok)
    assert sta_status_detail("drc", envelope) == {}


def test_non_ok_status_fails_aggregation_mode_too(tmp_path):
    ok = build_signoff([_write(tmp_path, "ok.json", _MULTI)])["checks"][0]
    bad = build_signoff([_write(tmp_path, "bad.json", {**_MULTI, "status": "error"})])
    assert ok["passed"] is True
    assert bad["checks"][0]["passed"] is False


# --- geometry_source: not graded by default, but always disclosed ----------


@_ENVELOPES
@pytest.mark.parametrize("source", _ESTIMATES)
def test_estimate_geometry_stays_met_without_the_opt_in(tmp_path, base, source):
    envelope = {**base, "geometry_source": source}
    assert _grade_sta_item_5(tmp_path, envelope)["status"] == "met"
    off = _grade_sta_item_5(tmp_path, envelope, require_routed_geometry=False)
    assert off["status"] == "met"


@_ENVELOPES
@pytest.mark.parametrize("source", ["routed", *_ESTIMATES])
def test_met_citation_carries_geometry_source_by_default(tmp_path, base, source):
    item = _grade_sta_item_5(tmp_path, {**base, "geometry_source": source})
    assert item["status"] == "met"
    assert _sta_citation(item)["detail"]["geometry_source"] == source
    assert item["citation"]["detail"]["geometry_source"] == source


def test_geometry_source_disclosure_does_not_leak_into_item_detail(tmp_path):
    item = _grade_sta_item_5(
        tmp_path, {**_MULTI, "geometry_source": "netlist_estimate"}
    )
    assert "geometry_source" not in (item.get("detail") or {})


# --- require_routed_geometry (opt-in) ---------------------------------------


@_ENVELOPES
def test_require_routed_geometry_passes_a_routed_envelope(tmp_path, base):
    item = _grade_sta_item_5(tmp_path, base, require_routed_geometry=True)
    assert item["status"] == "met"
    assert _sta_citation(item)["detail"]["geometry_source"] == "routed"


@_ENVELOPES
@pytest.mark.parametrize("source", _ESTIMATES)
def test_require_routed_geometry_refuses_anything_not_routed(tmp_path, base, source):
    envelope = {**base, "geometry_source": source}
    item = _grade_sta_item_5(tmp_path, envelope, require_routed_geometry=True)
    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"
    assert item["detail"]["sta_geometry_not_routed"] == {"geometry_source": source}


@pytest.mark.parametrize("value", ["Routed", "routed ", "ROUTED", "", None, 5, True])
def test_geometry_refusal_fails_closed_on_every_non_literal_routed(value):
    spec = {"require_routed_geometry": True}
    refusal = sta_geometry_refusal("sta", spec, {"geometry_source": value})
    assert refusal == (
        "unmet",
        "check_failed",
        None,
        {"sta_geometry_not_routed": {"geometry_source": value}},
    )


def test_geometry_refusal_fails_closed_when_the_field_is_missing():
    refusal = sta_geometry_refusal("sta", {"require_routed_geometry": True}, {})
    assert refusal is not None
    assert refusal[3] == {"sta_geometry_not_routed": {"geometry_source": None}}


def test_geometry_refusal_is_off_unless_literally_true():
    estimate = {"geometry_source": "placement_estimate"}
    assert sta_geometry_refusal("sta", {}, estimate) is None
    assert (
        sta_geometry_refusal("sta", {"require_routed_geometry": False}, estimate)
        is None
    )
    # Only an `sta` citation is subject to the requirement.
    spec = {"require_routed_geometry": True}
    assert sta_geometry_refusal("drc", spec, estimate) is None
    assert sta_geometry_refusal("sta", spec, {"geometry_source": "routed"}) is None


@pytest.mark.parametrize("value", [None, 5])
def test_unrecognisable_geometry_source_is_unmet_even_before_the_opt_in(
    tmp_path, value
):
    # A missing / null / non-string `geometry_source` is not a `klt sta`
    # envelope at all (classification needs the string), so it never reaches
    # the opt-in: it fails closed one step earlier, with or without the key.
    envelope = {**_MULTI, "geometry_source": value}
    for extra in ({}, {"require_routed_geometry": True}):
        item = _grade_sta_item_5(tmp_path, envelope, **extra)
        assert item["status"] == "unmet"
        assert item["reason"] == "unrecognized_envelope"


@pytest.mark.parametrize("value", ["yes", "true", 1, 0, None, []])
def test_non_boolean_require_routed_geometry_is_invalid_evidence(tmp_path, value):
    item = _grade_sta_item_5(tmp_path, _MULTI, require_routed_geometry=value)
    assert item["status"] == "unmet"
    assert item["reason"] == "invalid_evidence"


# --- composition with the sibling opt-ins -----------------------------------

_COMPLETE_BLOCK = {
    "status": "complete",
    "unconstrained_endpoints": {"status": "ok", "count": 0},
    "unclocked_registers": {"status": "ok", "count": 0},
}
_ANNOTATED = {"annotation_complete": True, "annotation_warning": None}
_UNANNOTATED = {"annotation_complete": False, "annotation_warning": "not applied"}


def _with_corner_blocks(base: dict, **blocks) -> dict:
    corners = [{**corner, **blocks} for corner in base["corners"]]
    return {**base, "corners": corners}


def test_all_three_opt_ins_compose_on_one_entry(tmp_path):
    envelope = _with_corner_blocks(
        _MULTI, constraint_completeness=_COMPLETE_BLOCK, spef_annotation=_ANNOTATED
    )
    opts = {
        "require_annotation_complete": True,
        "require_complete_constraints": True,
        "require_routed_geometry": True,
    }
    assert _grade_sta_item_5(tmp_path, envelope, **opts)["status"] == "met"

    estimate = {**envelope, "geometry_source": "placement_estimate"}
    item = _grade_sta_item_5(tmp_path, estimate, **opts)
    assert item["status"] == "unmet"
    assert "sta_geometry_not_routed" in item["detail"]
    assert "sta_annotation_incomplete" not in item["detail"]


def test_sibling_opt_in_failures_keep_their_own_detail(tmp_path):
    opts = {"require_annotation_complete": True, "require_routed_geometry": True}
    bad_annotation = _with_corner_blocks(_MULTI, spef_annotation=_UNANNOTATED)
    item = _grade_sta_item_5(tmp_path, bad_annotation, **opts)
    assert item["status"] == "unmet"
    assert "sta_annotation_incomplete" in item["detail"]

    both_bad = {**bad_annotation, "geometry_source": "netlist_estimate"}
    first = _grade_sta_item_5(tmp_path, both_bad, **opts)
    # Annotation is evaluated first; fixing it exposes the geometry shortfall.
    assert "sta_annotation_incomplete" in first["detail"]
    fixed = _with_corner_blocks(both_bad, spef_annotation=_ANNOTATED)
    second = _grade_sta_item_5(tmp_path, fixed, **opts)
    assert second["detail"]["sta_geometry_not_routed"] == {
        "geometry_source": "netlist_estimate"
    }


def test_require_routed_geometry_does_not_hide_a_failed_status(tmp_path):
    envelope = {**_MULTI, "status": "error"}
    item = _grade_sta_item_5(tmp_path, envelope, require_routed_geometry=True)
    assert item["status"] == "unmet"
    assert item["detail"]["sta_status"] == "error"
