"""Issue #4914: `klt place-and-route` fmax nulling when unconstrained, and
`constraints.power_activity` / `power_basis` (split from test_place_and_route.py
to keep that file's size ceiling from growing)."""

from __future__ import annotations

import os

import pytest

from klayout_tools.place_and_route import PlaceAndRouteError, run_place_and_route
from test_place_and_route import (
    _STAGE_METRICS,
    _UNCONSTRAINED,
    _script_lines,
    _setup_success_env,
    _stub_merge_def_to_gds,
    _stub_openroad_success,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def test_fmax_mhz_is_null_when_timing_is_unconstrained(tmp_path, monkeypatch):
    """Issue #4914: `timing__fmax` is not backed by any timed path when every
    slack field is the sentinel, so `fmax_mhz` is `null` at top level and in
    every stage entry; the slack sentinels themselves stay verbatim."""
    sentinel_stage_metrics = {
        stage: {**metrics, "timing__setup__ws": _UNCONSTRAINED, "timing__setup__tns": 0}
        for stage, metrics in _STAGE_METRICS.items()
    }
    assert any("timing__fmax" in m for m in sentinel_stage_metrics.values())
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(
        monkeypatch,
        stage_metrics=sentinel_stage_metrics,
        corner_sweep_metrics={
            "timing__setup__ws": _UNCONSTRAINED,
            "timing__hold__ws": _UNCONSTRAINED,
        },
    )
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["timing_status"] == "unconstrained"
    assert report["worst_slack_ns"] == _UNCONSTRAINED
    assert report["fmax_mhz"] is None
    assert all(stage.get("fmax_mhz") is None for stage in report["stages"])


def test_fmax_mhz_unchanged_when_timing_is_constrained(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["timing_status"] == "constrained"
    assert report["fmax_mhz"] == round(_STAGE_METRICS["route"]["timing__fmax"] / 1e6, 4)
    assert all(
        isinstance(stage["fmax_mhz"], float)
        for stage in report["stages"]
        if stage["name"] != "floorplan"
    )


def _all_stage_script_lines(request_path):
    script_dir = os.path.join(os.path.dirname(request_path), ".klt", "place-and-route")
    out = []
    for name in sorted(os.listdir(script_dir)):
        if name.endswith(".tcl"):
            out.append((name, _script_lines(os.path.join(script_dir, name))))
    return out


def test_power_activity_emitted_before_power_report_and_power_basis(
    tmp_path, monkeypatch
):
    """Issue #4914: `constraints.power_activity` reaches every stage that
    reports power, ahead of `report_power_metric`, and `power_basis` echoes it."""
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clock_port": "clk",
            "clock_period_ns": 1.1,
            "power_activity": {"activity": 0.1, "duty": 0.5},
        },
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["power_basis"]["activity_source"] == "global"
    assert report["power_basis"]["activity"] == 0.1
    assert report["power_basis"]["duty"] == 0.5
    emitted = 0
    for _name, lines in _all_stage_script_lines(request_path):
        if "report_power_metric" in lines:
            emitted += 1
            assert lines.index(
                "set_power_activity -global -activity 0.1 -duty 0.5"
            ) < lines.index("report_power_metric")
    assert emitted


def test_power_activity_omitted_leaves_scripts_unchanged(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["power_basis"]["activity_source"] == "engine_default"
    assert report["power_basis"]["activity"] is None
    for _name, lines in _all_stage_script_lines(request_path):
        assert not any("set_power_activity" in line for line in lines)


@pytest.mark.parametrize(
    "value, match",
    [
        ({}, "at least one"),
        ("0.1", "must be an object"),
        ({"activity": -0.1}, "activity must be >= 0"),
        ({"duty": 1.5}, r"duty must be in \[0, 1\]"),
        ({"density": 1}, "unknown key"),
    ],
)
def test_power_activity_rejected(tmp_path, monkeypatch, value, match):
    request_path = _setup_success_env(
        tmp_path,
        monkeypatch,
        constraints={
            "clock_port": "clk",
            "clock_period_ns": 1.1,
            "power_activity": value,
        },
    )
    with pytest.raises(PlaceAndRouteError, match=match):
        run_place_and_route(request_path)
