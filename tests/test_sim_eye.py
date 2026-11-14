"""Tests for ``measurements[].eye`` -- the built-in eye / phase-fold
measurement (issue #4292). Everything runs without ngspice."""

from __future__ import annotations

import pytest

from klayout_tools import sim, sim_batch, sim_eye, sim_validate
from klayout_tools.sim_errors import SimError

UI = 1.0
_BITS = [0, 1, 1, 0, 1, 0, 0, 1, 1, 1, 0, 0]


def _nrz(bits, *, rise=0.2, lo=0.0, hi=1.0, delay=0.0):
    """Piecewise-linear NRZ: bit k occupies [k + delay, k + 1 + delay), the
    transition into it takes ``rise`` UI starting at the boundary."""
    scale, values = [0.0], [lo + (hi - lo) * bits[0]]
    for k in range(1, len(bits)):
        t = k + delay
        scale += [t, t + rise]
        values += [lo + (hi - lo) * bits[k - 1], lo + (hi - lo) * bits[k]]
    scale.append(len(bits) + delay + 1.0)
    values.append(values[-1])
    return scale, values


def test_open_eye_known_height_width_phase():
    scale, values = _nrz(_BITS, rise=0.2)
    out = sim_eye.compute_eye(scale, values, ui=UI, uis=10, start=1.0)
    assert out.error is None
    assert out.height == pytest.approx(1.0)
    # Crossings of a clean NRZ all sit 0.1 UI after the boundary, so the
    # opening is the whole UI and its centre is half a UI later.
    assert out.width == pytest.approx(1.0, abs=1e-6)
    assert out.best_phase == pytest.approx(0.6, abs=1e-6)
    assert out.level == pytest.approx(0.5)


def test_crossing_spread_narrows_the_eye():
    # Alternate a fast and a slow edge so crossings land at two phases.
    scale = [0.0, 1.0, 1.1, 2.0, 2.6, 3.0, 3.1, 4.0, 4.6, 5.0, 5.1, 6.0]
    values = [0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0]
    out = sim_eye.compute_eye(scale, values, ui=UI, uis=6)
    assert out.error is None
    # Crossings at phases 0.05 (rising) and 0.3 (falling): widest gap 0.75.
    assert out.width == pytest.approx(0.75, abs=1e-6)
    assert out.best_phase == pytest.approx(0.675, abs=1e-6)


def test_metric_selects_value():
    scale, values = _nrz(_BITS)
    for metric, attr in (
        ("height", "height"),
        ("width", "width"),
        ("best_phase", "best_phase"),
    ):
        out = sim_eye.compute_eye(
            scale, values, ui=UI, uis=10, start=1.0, metric=metric
        )
        assert out.value == getattr(out, attr)


def test_window_outside_transient():
    scale, values = _nrz(_BITS)
    out = sim_eye.compute_eye(scale, values, ui=UI, uis=50, start=1.0)
    assert out.code == "eye_window_outside_transient"
    assert out.value is None


def test_no_crossing_of_level_is_no_levels():
    out = sim_eye.compute_eye([0.0, 10.0], [1.0, 1.0], ui=1.0, uis=4, level=0.5)
    assert out.code == "eye_no_levels"


def test_validation_errors():
    ok = {"signal": "v(x)", "ui": "100p", "uis": 8}
    sim_eye.validate_eye_spec("e", ok)
    for bad in (
        {**ok, "uis": 2.5},
        {**ok, "uis": True},
        {**ok, "uis": 1},
        {**ok, "ui": 0},
        {**ok, "ui": "abc"},
        {**ok, "metric": "jitter"},
        {**ok, "bogus": 1},
        {"ui": "1n", "uis": 4},
    ):
        with pytest.raises(SimError):
            sim_eye.validate_eye_spec("e", bad)


def test_exclusive_with_other_forms():
    spec = {
        "name": "e",
        "expr": "v(x)",
        "eye": {"signal": "v(x)", "ui": 1e-9, "uis": 4},
    }
    with pytest.raises(SimError, match="alternative forms"):
        sim_validate._validate_measurement_form(spec)
    sim_validate._validate_measurement_form(
        {"name": "e", "eye": {"signal": "v(x)", "ui": 1e-9, "uis": 4}}
    )


def test_context_requires_tran_and_window_inside_stop():
    spec = [
        {"name": "e", "eye": {"signal": "v(x)", "ui": "1n", "uis": 8, "from": "2n"}}
    ]
    with pytest.raises(SimError, match="tran"):
        sim_eye.validate_eye_context(spec, {"kind": "ac", "args": "dec 10 1 1e9"})
    with pytest.raises(SimError, match="transient stops"):
        sim_eye.validate_eye_context(spec, {"kind": "tran", "args": "10p 5n"})
    sim_eye.validate_eye_context(spec, {"kind": "tran", "args": "10p 20n"})


def test_control_lines(tmp_path):
    spec = [{"name": "e", "eye": {"signal": "v(a)-v(b)", "ui": 1e-9, "uis": 4}}]
    lines = sim_eye.control_lines(spec, str(tmp_path))
    assert lines[0] == "let __klt_eye_0 = v(a)-v(b)"
    assert lines[1] == f"write {tmp_path}/eye_0.raw __klt_eye_0"
    assert sim_eye.control_lines([{"name": "x", "expr": "v(a)"}], str(tmp_path)) == []


def test_measurement_result_shape_and_error():
    spec = {"name": "e", "unit": "V", "limits": {"min": 0.5}}
    scale, values = _nrz(_BITS)
    outcome = sim_eye.compute_eye(scale, values, ui=UI, uis=10, start=1.0)
    result, _ = sim._eye_measurement_result(spec, outcome, save_mode="all")
    assert result["value"] == pytest.approx(1.0)
    assert result["status"] == "pass"
    assert set(result["eye"]) >= {"height", "width", "best_phase"}
    result, diags = sim._eye_measurement_result(spec, None, save_mode="all")
    assert result["status"] == "error" and result["value"] is None
    assert diags[0]["message"].startswith("[eye_vector_missing]")


def test_runner_feature_gate_registered():
    assert "eye" in sim_batch._RUNNER_REQUEST_FEATURES
    assert "eye" in sim_batch._MANDATORY_RUNNER_FEATURES
    detect = sim_batch._RUNNER_REQUEST_FEATURES["eye"][1]
    assert detect({"measurements": [{"name": "e", "eye": {}}]})
    assert not detect({"measurements": [{"name": "e", "expr": "v(a)"}]})


def test_refused_with_analysis_steps(tmp_path):
    import json

    (tmp_path / "body.spice").write_text("R1 a 0 1k\n")
    spec = {"name": "e", "eye": {"signal": "v(a)", "ui": "1n", "uis": 4}}
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
