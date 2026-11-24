"""Tests for ``measurements[].eye`` -- the built-in eye / phase-fold
measurement and transient step integration."""

from __future__ import annotations

import json

import pytest

from helpers.tool_probes import SKIP_NO_NGSPICE
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


@pytest.mark.parametrize(("phase", "height"), [(None, 1.0), (0, 1.0), (0.25, 7 / 12)])
def test_sampling_phase_uses_interpolation_relative_to_window(phase, height):
    start = 2.5
    scale = [
        start + t for t in [0.0, 1.0, 1.1, 2.0, 2.6, 3.0, 3.1, 4.0, 4.6, 5.0, 5.1, 6.0]
    ]
    values = [0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0]
    out = sim_eye.compute_eye(scale, values, ui=1, uis=6, start=start, phase=phase)
    assert out.height == pytest.approx(height)
    assert out.width == pytest.approx(0.75)
    assert out.best_phase == pytest.approx(0.675)
    assert out.report().get("sampling_phase") == phase


def _step(name, level):
    return {
        "name": name,
        "analysis": {"kind": "tran", "args": "10p 8n"},
        "alter": {"Vrail": level},
        "measurements": [
            {
                "name": "opening",
                "unit": "V",
                "eye": {
                    "signal": "v(rx)",
                    "ui": "1n",
                    "uis": 6,
                    "from": "1n",
                    "phase": "500p",
                },
            }
        ],
    }


@pytest.mark.parametrize("phase", [-0.1, "1n", "2n", True])
def test_out_of_range_or_non_numeric_phase(phase):
    body = _step("lo", 1)["measurements"][0]["eye"]
    body["phase"] = phase
    with pytest.raises(SimError, match="phase"):
        sim_eye.validate_eye_spec("opening", body)


def test_step_eye_validation_and_derived_references():
    from klayout_tools.sim_steps import resolve_analysis_steps

    lo, hi = _step("lo", 1), _step("hi", 2)
    with pytest.raises(SimError, match="derived expression"):
        resolve_analysis_steps(
            {"analysis_steps": [lo], "measurements": lo["measurements"]}
        )
    with pytest.raises(SimError, match="postprocessed eye 'lo.opening'"):
        resolve_analysis_steps(
            {
                "analysis_steps": [lo],
                "measurements": [{"name": "out", "expr": "lo.opening"}],
            }
        )
    hi["analysis"]["args"] = "10p ${lo.opening}"
    with pytest.raises(SimError, match="postprocessed eye 'lo.opening'"):
        resolve_analysis_steps({"analysis_steps": [lo, hi]})
    lo["analysis"] = {"kind": "op", "args": ""}
    with pytest.raises(SimError, match="tran"):
        resolve_analysis_steps({"analysis_steps": [lo]})
    lo["analysis"] = {"kind": "tran", "args": "10p 4n"}
    with pytest.raises(SimError, match="window ends"):
        resolve_analysis_steps({"analysis_steps": [lo]})


def test_step_failure_does_not_grade_an_eye_from_another_plot():
    from klayout_tools import sim_steps

    analysis, specs = sim_steps.resolve_analysis_steps(
        {"analysis_steps": [_step("lo", 1), _step("hi", 2)]}
    )
    scale, values = _nrz(_BITS)
    eye = sim_eye.compute_eye(scale, values, ui=1, uis=10, start=1)
    log = (
        "klt_step_begin 0 unknown1\nklt_step_end 0 tran1\n"
        "klt_step_begin 1 unknown2\nklt_step_end 1 unknown2"
    )
    rows, diags, _ = sim_steps.grade_step_measurements(
        {},
        specs,
        analysis=analysis,
        log_text=log,
        save_mode="all",
        eyes={0: eye, 1: eye},
    )
    assert [(r["step"], r["status"], r["value"]) for r in rows] == [
        ("lo", "pass", 1),
        ("hi", "error", None),
    ]
    assert [d["code"] for d in diags] == ["step_failed"]


def test_extended_feature_detection_distinguishes_legacy_eyes():
    check = sim_batch._RUNNER_REQUEST_FEATURES["eye_step_or_phase"][1]
    lo = _step("lo", 1)
    assert check({"analysis_steps": [lo]})
    assert check({"measurements": lo["measurements"]})
    del lo["measurements"][0]["eye"]["phase"]
    assert not check({"measurements": lo["measurements"]})
    assert "eye_step_or_phase" in sim_batch._MANDATORY_RUNNER_FEATURES


@SKIP_NO_NGSPICE
@pytest.mark.parametrize("fail_high", [False, True])
def test_public_step_eyes_are_bound_to_their_own_waveforms(tmp_path, fail_high):
    netlist = tmp_path / "eye.sp"
    netlist.write_text(
        "Vpattern data 0 PULSE(0 1 0 100p 100p 900p 2n)\n"
        "Vrail rail 0 1\nBreceive rx 0 V=V(data)*V(rail)\nRload rx 0 1k\n"
    )
    lo, hi = _step("low", 1), _step("high", 2)
    # Intermediate lets change a step's measurement emission path. The
    # corner's two rawfile indices must still agree with result harvesting.
    lo["measurements"].insert(
        0, {"name": "railvalue", "expr": "v(rail)", "intermediate": True}
    )
    lo["measurements"].append({"name": "saved", "expr": "railvalue[0]"})
    if fail_high:
        hi["analysis"]["args"] = "10p 8n 9n"
    spec = {
        "netlist": str(netlist),
        "analysis_steps": [lo, hi],
        "options": {"ngspice_init": []},
    }
    path = tmp_path / "steps.json"
    path.write_text(json.dumps(spec))
    corner = sim.run_sim(str(path))["corners"][0]
    rows = {r["name"]: r for r in corner["measurements"]}
    low = rows["low.opening"]
    assert low["step"] == "low"
    assert low["eye"]["height"] == low["value"] == 1
    assert low["eye"]["width"] == pytest.approx(1e-9, abs=1e-18)
    assert low["eye"]["sampling_phase"] == pytest.approx(5e-10, abs=1e-18)
    assert rows["low.saved"]["value"] == 1
    high = rows["high.opening"]
    assert high["step"] == "high"
    if fail_high:
        assert high["status"] == "error" and high["value"] is None
        assert "step_failed" in [d["code"] for d in corner["diagnostics"]]
    else:
        assert high["eye"]["height"] == high["value"] == 2
        assert high["eye"]["width"] == pytest.approx(low["eye"]["width"], abs=1e-18)


@pytest.mark.parametrize("code", sim_eye.ERROR_CODES)
@pytest.mark.parametrize("save_mode", ["netlist", "all"])
def test_eye_diagnostic_save_hint_only_for_missing_netlist_vector(code, save_mode):
    outcome = sim_eye.EyeOutcome(error="eye detail", code=code)
    result, diagnostics = sim._eye_measurement_result(
        {"name": "opening"}, outcome, save_mode=save_mode
    )
    expected = f"[{code}] eye detail"
    if code == "eye_vector_missing" and save_mode == "netlist":
        expected += sim.SAVE_MODE_NETLIST_NO_VALUE_HINT
    assert result["status"] == "error"
    assert diagnostics == [
        {"severity": "error", "code": "measurement", "message": expected}
    ]
