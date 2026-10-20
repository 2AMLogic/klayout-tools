"""Tests for ``measurements[].cycle_mean`` -- a mean over whole cycles of a
reference signal (issue #2947).

Everything here runs without ngspice: the reduction is pure Python, and the
``run_sim`` tests stub ``ngspice -b`` (cf. tests/test_sim_crossings.py).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import sim, sim_cycle_mean

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_BODY = {
    "signal": "v(out)",
    "align": "v(clk)",
    "level": 0.0,
    "edge": "rise",
    "cycles": 2,
}
_DECL = {"name": "vavg", "cycle_mean": _BODY}


def _write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


def _write_body(tmp_path: Path) -> Path:
    path = tmp_path / "body.spice"
    path.write_text("V1 out 0 SIN(0.5 0.5 1e6)\nR1 out 0 1k\n")
    return path


# --------------------------------------------------------------------------- #
# Reduction: analytic mean on a non-uniform timestep
# --------------------------------------------------------------------------- #


def _triangle_grid():
    """Period-1 triangle between 0 and 2 (mean exactly 1), piecewise linear
    with knots every 0.5 -- so trapezoidal integration is exact -- but sampled
    densely near the peak and sparsely elsewhere, which biases a plain sample
    mean. The reference is the same wave minus 1, so it
    rises through 0 at t = 0.25 + k."""
    scale = []
    for cycle in range(4):
        scale.append(float(cycle))
        scale += [cycle + 0.3 + i * 0.02 for i in range(10)]  # dense near the peak
        scale.append(cycle + 0.5)  # then one sample across the whole falling half
    scale.append(4.0)

    def tri(t):
        phase = t % 1.0
        return 4 * phase if phase <= 0.5 else 4 * (1 - phase)

    signal = [tri(t) for t in scale]  # 0..2, peak at 0.5
    reference = [value - 1.0 for value in signal]
    return scale, signal, reference


def test_mean_over_whole_cycles_is_exact_on_a_nonuniform_timestep():
    scale, signal, reference = _triangle_grid()
    outcome = sim_cycle_mean.compute_cycle_mean(
        scale, signal, reference, level=0.0, edge="rise", start=0.0, cycles=2
    )
    assert outcome.error is None
    assert outcome.t_start == pytest.approx(0.25)
    assert outcome.t_end == pytest.approx(2.25)
    assert outcome.cycles_used == 2
    assert outcome.mean == pytest.approx(1.0, abs=1e-12)


def test_plain_sample_mean_and_fixed_window_routes_fail_the_same_signal():
    """The two routes the issue rules out are measurably biased on the very
    signal the aligned mean gets exactly right."""
    scale, signal, reference = _triangle_grid()
    inside = [v for t, v in zip(scale, signal, strict=True) if 0.25 <= t <= 2.25]
    plain = sum(inside) / len(inside)
    assert abs(plain - 1.0) > 0.1
    # Fixed literal window that is not a whole number of periods.
    fixed = sim_cycle_mean.time_weighted_mean(scale, signal, 0.0, 1.9)
    assert abs(fixed - 1.0) > 0.01


def test_falling_edge_alignment_and_single_cycle():
    scale, signal, reference = _triangle_grid()
    outcome = sim_cycle_mean.compute_cycle_mean(
        scale, signal, reference, level=0.0, edge="fall", start=0.0, cycles=1
    )
    assert outcome.t_start == pytest.approx(0.75)
    assert outcome.t_end == pytest.approx(1.75)
    assert outcome.mean == pytest.approx(1.0, abs=1e-12)


def test_window_start_skips_earlier_edges():
    scale, signal, reference = _triangle_grid()
    outcome = sim_cycle_mean.compute_cycle_mean(
        scale, signal, reference, level=0.0, start=1.0, cycles=2
    )
    assert outcome.t_start == pytest.approx(1.25)
    assert outcome.t_end == pytest.approx(3.25)


def test_window_straddling_samples_interpolates_both_endpoints():
    # Ramp 0..10 over t in [0, 10]; neither endpoint of [2.5, 7.5] is a sample.
    scale = [0.0, 5.0, 10.0]
    signal = [0.0, 5.0, 10.0]
    mean = sim_cycle_mean.time_weighted_mean(scale, signal, 2.5, 7.5)
    assert mean == pytest.approx(5.0)


def test_too_few_edges_is_an_error_with_a_code():
    scale, signal, reference = _triangle_grid()
    outcome = sim_cycle_mean.compute_cycle_mean(
        scale, signal, reference, level=0.0, start=0.0, cycles=4
    )  # only 4 rising edges in the run: 5 are needed
    assert outcome.mean is None
    assert outcome.code == "cycle_mean_insufficient_edges"
    assert "need 5" in outcome.error


def test_after_s_past_the_end_of_the_run_has_no_reference_crossing():
    scale, signal, reference = _triangle_grid()
    outcome = sim_cycle_mean.compute_cycle_mean(
        scale, signal, reference, level=0.0, start=99.0, cycles=1
    )
    assert outcome.mean is None
    assert outcome.code == "cycle_mean_no_reference_crossing"


def test_reference_that_never_reaches_the_level():
    outcome = sim_cycle_mean.compute_cycle_mean(
        [0.0, 1.0, 2.0], [1.0, 1.0, 1.0], [0.1, 0.2, 0.1], level=5.0, cycles=1
    )
    assert outcome.code == "cycle_mean_no_reference_crossing"


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body,match",
    [
        ({**_BODY, "cycles": 0}, "cycles is required"),
        ({**_BODY, "cycles": 1.5}, "cycles is required"),
        ({k: v for k, v in _BODY.items() if k != "cycles"}, "cycles is required"),
        ({k: v for k, v in _BODY.items() if k != "align"}, "signal is required"),
        ({k: v for k, v in _BODY.items() if k != "signal"}, "signal is required"),
        ({k: v for k, v in _BODY.items() if k != "level"}, "level is required"),
        ({**_BODY, "edge": "either"}, "edge must be one of"),
        ({**_BODY, "from": "soon"}, "must be a time in seconds"),
        ({**_BODY, "tmax": 1}, "unknown cycle_mean key"),
        ("v(a)", "must be an object"),
    ],
)
def test_malformed_declarations_are_refused(body, match):
    with pytest.raises(sim.SimError, match=match):
        sim._validate_measurement_forms([{"name": "t", "cycle_mean": body}])


@pytest.mark.parametrize(
    "other",
    [
        {"spice": ".meas tran t MAX v(a)"},
        {"expr": "v(a)"},
        {"crossings": {"signal": "v(a)", "level": 0.5}},
    ],
)
def test_combined_with_another_form_is_refused(other):
    with pytest.raises(sim.SimError, match="declare exactly one"):
        sim._validate_measurement_forms([{**_DECL, **other}])


def test_valid_declaration_with_spice_time_literal_is_accepted():
    sim._validate_measurement_forms(
        [{"name": "t", "cycle_mean": {**_BODY, "from": "10u"}}]
    )


def test_requires_a_transient(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "ac", "args": "dec 10 1 1e6"},
            "measurements": [_DECL],
        },
    )
    with pytest.raises(sim.SimError, match='requires analysis.kind "tran"'):
        sim.run_sim(str(request))


def test_refused_for_xyce(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [_DECL],
        },
    )
    with pytest.raises(
        sim.SimError, match="cycle_mean is not supported for engine 'xyce'"
    ):
        sim.run_sim(str(request))


def test_refused_with_analysis_steps(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis_steps": [
                {
                    "name": "s1",
                    "analysis": {"kind": "tran", "args": "1n 1u"},
                    "measurements": [_DECL],
                }
            ],
        },
    )
    with pytest.raises(sim.SimError, match="not supported with analysis_steps"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def _deck(tmp_path: Path, measurements_spec) -> list[str]:
    deck_path = tmp_path / "corner.cir"
    sim._write_corner_deck(
        deck_path=str(deck_path),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        point=sim.CornerPoint(None, {}, 27),
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=measurements_spec,
        raw_path=None,
    )
    return deck_path.read_text().splitlines()


def test_deck_dumps_both_vectors_after_the_analysis(tmp_path):
    lines = _deck(tmp_path, [_DECL])
    tran = lines.index("tran 1n 1u")
    assert lines.index("set filetype=ascii") < tran
    sig = lines.index("let __klt_cmean_0_sig = v(out)")
    ref = lines.index("let __klt_cmean_0_ref = v(clk)")
    write = lines.index(
        f"write {tmp_path / 'cycle_mean_0.raw'} __klt_cmean_0_sig __klt_cmean_0_ref"
    )
    assert tran < sig < ref < write < lines.index("quit")


def test_deck_without_cycle_mean_is_unchanged(tmp_path):
    lines = _deck(tmp_path, [{"name": "v", "spice": ".meas tran v MAX v(out)"}])
    assert not any("cmean" in line or "filetype" in line for line in lines)


# --------------------------------------------------------------------------- #
# run_sim with ngspice stubbed: grading and the response shape
# --------------------------------------------------------------------------- #


def _rawfile(scale, signal, reference) -> str:
    lines = [
        "Title: stub",
        "Plotname: Transient Analysis",
        "Flags: real",
        "No. Variables: 3",
        f"No. Points: {len(scale)}",
        "Variables:",
        "\t0\ttime\ttime",
        "\t1\tv(__klt_cmean_0_sig)\tvoltage",
        "\t2\tv(__klt_cmean_0_ref)\tvoltage",
        "Values:",
    ]
    for index, (t, s, r) in enumerate(zip(scale, signal, reference, strict=True)):
        lines.append(f" {index}\t{t!r}")
        lines.append(f"\t{s!r}")
        lines.append(f"\t{r!r}")
    return "\n".join(lines) + "\n"


def _stub_ngspice(monkeypatch, *, raw: str | None):
    real_which = shutil.which
    monkeypatch.setattr(
        paths_module.shutil,
        "which",
        lambda cmd, *a, **k: cmd if cmd == "ngspice" else real_which(cmd, *a, **k),
    )

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        log_path = cmd[cmd.index("-o") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("No. of Data Rows : 5\n")
        if raw is not None:
            path = os.path.join(cwd, "cycle_mean_0.raw")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(raw)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def _request(tmp_path: Path, measurement: dict, **extra) -> Path:
    _write_body(tmp_path)
    return _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 4"},
            "measurements": [measurement],
            "options": {"keep_artifacts": False},
            **extra,
        },
    )


def test_cycle_mean_value_window_and_grading(tmp_path, monkeypatch):
    """Carries the mean as a plain scalar (graded by limits) plus the window,
    with keep_artifacts false and no waveform artifact."""
    request = _request(tmp_path, {**_DECL, "limits": {"min": 0.9, "max": 1.1}})
    _stub_ngspice(monkeypatch, raw=_rawfile(*_triangle_grid()))

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    assert report["coverage"]["nothing_checked"] is False
    (corner,) = report["corners"]
    (measurement,) = corner["measurements"]
    assert measurement["value"] == pytest.approx(1.0, abs=1e-12)
    assert measurement["status"] == "pass"
    assert measurement["cycle_mean"]["cycles_used"] == 2
    assert measurement["cycle_mean"]["t_start"] == pytest.approx(0.25)
    assert measurement["cycle_mean"]["t_end"] == pytest.approx(2.25)
    assert corner["diagnostics"] == []
    assert corner["artifacts"]["raw"] is None


def test_cycle_mean_is_graded_against_limits(tmp_path, monkeypatch):
    request = _request(tmp_path, {**_DECL, "limits": {"min": 1.5}})
    _stub_ngspice(monkeypatch, raw=_rawfile(*_triangle_grid()))

    report = sim.run_sim(str(request))

    (measurement,) = report["corners"][0]["measurements"]
    assert measurement["status"] == "fail"
    assert report["status"] == "fail"


def test_insufficient_edges_is_an_error_with_a_code(tmp_path, monkeypatch):
    request = _request(
        tmp_path, {"name": "vavg", "cycle_mean": {**_BODY, "cycles": 10}}
    )
    _stub_ngspice(monkeypatch, raw=_rawfile(*_triangle_grid()))

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    (measurement,) = corner["measurements"]
    assert measurement["status"] == "error"
    assert measurement["value"] is None
    assert "cycle_mean" not in measurement
    (diagnostic,) = corner["diagnostics"]
    assert diagnostic["code"] == "measurement"
    assert diagnostic["message"].startswith("[cycle_mean_insufficient_edges] ")


def test_missing_vector_is_an_error_with_a_code(tmp_path, monkeypatch):
    request = _request(tmp_path, _DECL)
    _stub_ngspice(monkeypatch, raw=None)

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    (measurement,) = corner["measurements"]
    assert measurement["status"] == "error"
    assert measurement["value"] is None
    (diagnostic,) = corner["diagnostics"]
    assert diagnostic["code"] == "measurement"
    assert diagnostic["message"].startswith("[cycle_mean_vector_missing] ")
    assert "did not resolve" in diagnostic["message"]
