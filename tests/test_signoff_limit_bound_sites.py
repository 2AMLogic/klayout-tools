"""Issue #7329: the two truthy-``limits`` bound tests left outside the
row-credit paths PR #7292 tightened. Both the ``model-run`` coverage refusal
(issue #4859) and the timing-margin row check (issue #4314) must require a
recognised, non-null ``min``/``max`` bound -- ``signoff_derived_rows._limit_keys``
-- not merely a truthy ``limits`` dict."""

from __future__ import annotations

import json

import pytest

from klayout_tools.signoff import (
    _REASON_NO_LIMITS_CHECKED,
    _coverage_refusal,
    _row_margin_lacking_corners,
    build_tier_report,
)

LIM = {"min": 1.0}
#: ``sim._RECOGNISED_LIMIT_KEYS`` is ``("min", "max")`` -- anything else (or a
#: null bound) applied no bound `klt sim` recognises.
UNRECOGNISED = {"maximum": 1.0}
NULL_BOUNDS = {"min": None, "max": None}

# -- Site 1: the `model-run` arm of `_coverage_refusal` ----------------------


def _meas(name, row=None, *, limits=LIM, status="pass"):
    m = {"name": name, "status": status, "limits": limits}
    if row:
        m["spec_row"] = row
    return m


def _env(measurements):
    return {
        "schema_version": 1,
        "kind": "model-run",
        "status": "pass",
        "method": "linear small-signal model, ideal supplies",
        "measurements": measurements,
    }


def _item(tmp_path, measurements, *, rows=("gain",)):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(_env(measurements)))
    manifest = {
        "block": "b",
        "kind": "analog",
        "evidence": {"5": [{"file": str(path), "rows": list(rows)}]},
        "spec_rows": {"5": [{"id": r} for r in rows]},
    }
    return next(i for i in build_tier_report(manifest)["items"] if i["id"] == 5)


@pytest.mark.parametrize("limits", [UNRECOGNISED, NULL_BOUNDS, {"min": None}])
def test_model_run_unrecognised_only_limits_is_refused(limits):
    env = _env([_meas("gain", "gain", limits=limits)])
    assert _coverage_refusal("model-run", env) == _REASON_NO_LIMITS_CHECKED


@pytest.mark.parametrize("limits", [None, {}])
def test_model_run_vacuous_limits_still_refused(limits):
    env = _env([_meas("gain", "gain", limits=limits)])
    assert _coverage_refusal("model-run", env) == _REASON_NO_LIMITS_CHECKED


@pytest.mark.parametrize(
    "limits",
    [
        {"min": 0.0},
        {"max": 0.0},
        {"min": 0},
        {"min": 1, "maximum": 2},
    ],
)
def test_model_run_recognised_bound_is_not_refused(limits):
    env = _env([_meas("gain", "gain", limits=limits)])
    assert _coverage_refusal("model-run", env) is None


def test_model_run_unrecognised_only_limits_is_no_limits_checked(tmp_path):
    item = _item(tmp_path, [_meas("gain", "gain", limits=UNRECOGNISED)])
    assert item["status"] == "unmet"
    assert item["reason"] == "no_limits_checked"


def test_model_run_null_bounds_only_is_no_limits_checked(tmp_path):
    item = _item(tmp_path, [_meas("gain", "gain", limits=NULL_BOUNDS)])
    assert item["status"] == "unmet"
    assert item["reason"] == "no_limits_checked"


def test_model_run_recognised_zero_bound_still_met(tmp_path):
    item = _item(tmp_path, [_meas("gain", "gain", limits={"min": 0})])
    assert item["status"] == "met"


def test_model_run_mixed_limits_with_recognised_bound_is_met(tmp_path):
    item = _item(tmp_path, [_meas("gain", "gain", limits={"min": 1, "maximum": 2})])
    assert item["status"] == "met"


def test_model_run_second_measurement_with_recognised_bound_is_not_refused(
    tmp_path,
):
    measurements = [
        _meas("gain_other", "gain", limits=UNRECOGNISED),
        _meas("gain", "gain", limits=LIM),
    ]
    item = _item(tmp_path, measurements)
    assert item["status"] == "met"


# -- Site 2: `_row_margin_lacking_corners` (timing margin, issue #4314) ------

CORNERS = ["tt/1.8V/27C", "ss/1.6V/125C"]


def _rollup(name, *, limits=LIM, status="pass", row="slack"):
    return {"name": name, "status": status, "limits": limits, "spec_row": row}


def _corners(*names_by_corner):
    return [
        {
            "corner_id": cid,
            "status": "pass",
            "measurements": [{"name": n, "status": "pass"} for n in names],
        }
        for cid, names in zip(CORNERS, names_by_corner, strict=True)
    ]


@pytest.mark.parametrize("limits", [UNRECOGNISED, NULL_BOUNDS, {"min": None}])
def test_row_margin_unrecognised_bound_only_is_star(limits):
    env = {
        "measurements": [_rollup("slack_ns", limits=limits)],
        "corners": _corners(("slack_ns",), ("slack_ns",)),
    }
    assert _row_margin_lacking_corners(env, "slack") == ["*"]


def test_row_margin_recognised_zero_bound_covers_corners():
    env = {
        "measurements": [_rollup("slack_ns", limits={"min": 0})],
        "corners": _corners(("slack_ns",), ("slack_ns",)),
    }
    assert _row_margin_lacking_corners(env, "slack") == []


def _sim_envelope(rollup, corners):
    return {
        "schema_version": 1,
        "netlist": "design.spice",
        "status": "pass",
        "corner_count": 2,
        "passed": 2,
        "failed": 0,
        "errored": 0,
        "environment": {},
        "provenance": {
            "klt_version": "0.2.0",
            "klayout_version": "0.30.10",
            "pdk": {"name": "sky130A", "source": "volare", "version": "20240101"},
            "deck": {"name": "sky130.lib.spice", "content_hash": "sha256:models"},
            "input": {"content_hash": "sha256:netlistA", "role": "netlist"},
        },
        "measurements": rollup,
        "corners": corners,
    }


def _grade(tmp_path, envelope):
    path = tmp_path / "sim.json"
    path.write_text(json.dumps(envelope))
    manifest = {
        "block": "b",
        "kind": "digital",
        "evidence": {"5": {"file": str(path), "rows": ["slack"]}},
        "spec_rows": {"5": [{"id": "slack", "timing_margin": True}]},
    }
    return next(i for i in build_tier_report(manifest)["items"] if i["id"] == 5)


def test_unrecognised_bound_passing_everywhere_does_not_satisfy_row(tmp_path):
    """The two-measurement shape from the issue: a valid-bound measurement
    that credits the row but passes at only some corners, plus an
    unrecognised-bound one passing at all of them. The row is reported as
    lacking the corner, not satisfied by the bound `klt sim` never applied."""
    rollup = [
        _rollup("slack_ns", limits={"min": 0.0}),
        _rollup("slack_alt", limits=UNRECOGNISED),
    ]
    corners = _corners(("slack_ns", "slack_alt"), ("slack_alt",))
    item = _grade(tmp_path, _sim_envelope(rollup, corners))
    assert item["status"] == "unmet"
    assert item["reason"] == "timing_margin_missing"
    assert item["detail"]["missing_timing_margin"] == [
        {"row": "slack", "corners": [CORNERS[1]]}
    ]


def test_null_bound_passing_everywhere_does_not_satisfy_row(tmp_path):
    rollup = [
        _rollup("slack_ns", limits={"min": 0.0}),
        _rollup("slack_alt", limits=NULL_BOUNDS),
    ]
    corners = _corners(("slack_ns", "slack_alt"), ("slack_alt",))
    item = _grade(tmp_path, _sim_envelope(rollup, corners))
    assert item["reason"] == "timing_margin_missing"
