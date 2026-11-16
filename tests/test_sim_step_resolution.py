"""Tests for ``measurements[].step_resolution`` (issue #4773). No ngspice."""

from __future__ import annotations

import json

import pytest

from klayout_tools import sim, sim_batch, sim_step_resolution, sim_validate
from klayout_tools.sim_errors import SimError

# A ramp 0 -> 1 over [10, 11]; coarse steps (2.0) away from the edge, fine
# (0.1) around it.
SCALE = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6]
SCALE += [10.7, 10.8, 10.9, 11.0, 13.0, 15.0]
VALUES = [0.0] * 6 + [i / 10 for i in range(1, 11)] + [1.0, 1.0]
assert len(SCALE) == len(VALUES)


def test_window_max_gap_overlap_semantics():
    assert sim_step_resolution.window_max_gap(SCALE, 10.5, 0.3) == pytest.approx(0.1)
    # Window reaching back to 8.0..10.0 interval picks up the coarse step.
    assert sim_step_resolution.window_max_gap(SCALE, 10.5, 1.0) == pytest.approx(2.0)
    # Interval straddling the window boundary counts.
    assert sim_step_resolution.window_max_gap(SCALE, 11.5, 0.2) == pytest.approx(2.0)


def test_compute_per_crossing_and_overall():
    out = sim_step_resolution.compute_step_resolution(
        SCALE, VALUES, 0.55, 0.3, edge="rise"
    )
    assert out.error is None
    assert out.count == 1
    assert out.max_gap == pytest.approx(0.1)
    assert out.crossings[0]["time"] == pytest.approx(10.55, abs=1e-6)
    coarse = sim_step_resolution.compute_step_resolution(
        SCALE, VALUES, 0.55, 3.0, edge="rise"
    )
    assert coarse.max_gap == pytest.approx(2.0)


def test_two_crossings_overall_is_max_and_truncation():
    scale = [0, 1, 2, 2.1, 2.2, 5, 6, 6.5, 7]
    values = [0, 0, 0, 1, 1, 1, 1, 0, 0]
    out = sim_step_resolution.compute_step_resolution(scale, values, 0.5, 0.5)
    assert out.count == 2 and len(out.crossings) == 2
    assert out.max_gap == pytest.approx(max(c["max_gap"] for c in out.crossings))
    capped = sim_step_resolution.compute_step_resolution(
        scale, values, 0.5, 0.5, max_count=1
    )
    assert capped.truncated and len(capped.crossings) == 1
    assert capped.max_gap == out.max_gap


def test_no_crossings_is_error():
    out = sim_step_resolution.compute_step_resolution(SCALE, VALUES, 5.0, 1.0)
    assert out.code == "step_resolution_no_crossings" and out.value is None


def test_crossing_near_run_edges():
    out = sim_step_resolution.compute_step_resolution(
        [0.0, 1.0, 2.0], [0.0, 1.0, 1.0], 0.5, 100.0
    )
    assert out.max_gap == pytest.approx(1.0)


def test_validation_errors():
    ok = {"signal": "v(sw)", "level": 2.5, "window": "10n"}
    sim_step_resolution.validate_step_resolution_spec("m", ok)
    for bad in (
        {**ok, "window": 0},
        {**ok, "window": -1e-9},
        {**ok, "window": "abc"},
        {k: v for k, v in ok.items() if k != "window"},
        {**ok, "level": "x"},
        {**ok, "level": float("nan")},
        {k: v for k, v in ok.items() if k != "level"},
        {**ok, "edge": "up"},
        {**ok, "bogus": 1},
        {**ok, "max": 0},
        {**ok, "from": "2n", "to": "1n"},
        {"level": 1, "window": 1e-9},
    ):
        with pytest.raises(SimError):
            sim_step_resolution.validate_step_resolution_spec("m", bad)


def test_exclusive_with_other_forms():
    body = {"signal": "v(x)", "level": 1, "window": 1e-9}
    with pytest.raises(SimError, match="alternative forms"):
        sim_validate._validate_measurement_form(
            {"name": "m", "expr": "v(x)", "step_resolution": body}
        )
    with pytest.raises(SimError, match="optional"):
        sim_validate._validate_measurement_form(
            {"name": "m", "step_resolution": body, "optional": True}
        )
    sim_validate._validate_measurement_form({"name": "m", "step_resolution": body})


def test_context_requires_tran():
    spec = [
        {"name": "m", "step_resolution": {"signal": "v(x)", "level": 1, "window": 1}}
    ]
    with pytest.raises(SimError, match="tran"):
        sim_step_resolution.validate_step_resolution_context(
            spec, {"kind": "ac", "args": "dec 10 1 1e9"}
        )
    sim_step_resolution.validate_step_resolution_context(
        spec, {"kind": "tran", "args": "1n 1u"}
    )


def test_control_lines(tmp_path):
    spec = [
        {"name": "m", "step_resolution": {"signal": "v(a)", "level": 1, "window": 1}}
    ]
    lines = sim_step_resolution.control_lines(spec, str(tmp_path))
    assert lines == [
        "let __klt_stepres_0 = v(a)",
        f"write {tmp_path}/step_resolution_0.raw __klt_stepres_0",
    ]
    assert sim_step_resolution.control_lines([{"name": "x", "expr": "v(a)"}], "d") == []


def test_extract_from_rawfile_matches_direct_read(tmp_path):
    """The rawfile round trip (what a remote/batch worker does) reproduces the
    in-memory reduction."""
    lines = [
        "Title: t",
        "Plotname: Transient Analysis",
        "Flags: real",
        "No. Variables: 2",
        f"No. Points: {len(SCALE)}",
        "Variables:",
        "\t0\ttime\ttime",
        "\t1\tv(sw)\tvoltage",
        "Values:",
    ]
    for i, (t, v) in enumerate(zip(SCALE, VALUES, strict=True)):
        lines += [f" {i}\t{t!r}", f"\t{v!r}"]
    (tmp_path / "step_resolution_0.raw").write_text("\n".join(lines) + "\n")
    spec = [
        {
            "name": "m",
            "step_resolution": {
                "signal": "v(sw)",
                "level": 0.55,
                "edge": "rise",
                "window": 0.3,
            },
        }
    ]
    out = sim_step_resolution.extract_corner_step_resolutions(spec, str(tmp_path))[0]
    assert out.error is None
    assert out.max_gap == pytest.approx(0.1)
    # Missing rawfile -> its own error, not a raise.
    missing = sim_step_resolution.extract_corner_step_resolutions(
        spec, str(tmp_path / "nope")
    )[0]
    assert missing.code == "step_resolution_vector_missing"


def test_measurement_result_shape_and_error():
    spec = {"name": "m", "unit": "s", "limits": {"max": 1.0}}
    out = sim_step_resolution.compute_step_resolution(SCALE, VALUES, 0.55, 0.3)
    result, _ = sim._step_resolution_measurement_result(spec, out, save_mode="all")
    assert result["value"] == pytest.approx(0.1)
    assert result["status"] == "pass"
    assert set(result["step_resolution"]) >= {"max_gap", "crossings", "count"}
    result, diags = sim._step_resolution_measurement_result(spec, None, save_mode="all")
    assert result["status"] == "error" and result["value"] is None
    assert diags[0]["message"].startswith("[step_resolution_vector_missing]")


def test_runner_feature_gate_registered():
    assert "step_resolution" in sim_batch._RUNNER_REQUEST_FEATURES
    assert "step_resolution" in sim_batch._MANDATORY_RUNNER_FEATURES
    detect = sim_batch._RUNNER_REQUEST_FEATURES["step_resolution"][1]
    assert detect({"measurements": [{"name": "e", "step_resolution": {}}]})
    assert not detect({"measurements": [{"name": "e", "expr": "v(a)"}]})


def test_refused_with_analysis_steps(tmp_path):
    (tmp_path / "body.spice").write_text("R1 a 0 1k\n")
    spec = {
        "name": "m",
        "step_resolution": {"signal": "v(a)", "level": 1, "window": "1n"},
    }
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis_steps": [
                    {
                        "name": "s1",
                        "analysis": {"kind": "tran", "args": "1n 1u"},
                        "measurements": [spec],
                    }
                ],
            }
        )
    )
    with pytest.raises(SimError, match="not supported with analysis_steps"):
        sim.run_sim(str(request))
