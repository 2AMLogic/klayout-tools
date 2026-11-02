"""Tests for ``measurements[].settled`` (issue #4780). No ngspice."""

from __future__ import annotations

import json
import math

import pytest

from klayout_tools import sim, sim_batch, sim_settled, sim_validate
from klayout_tools.sim_errors import SimError

BODY = {
    "clock": "v(clk)",
    "level": 0.5,
    "cycles": 2,
    "mean": {"signal": "v(vout)", "tol_pct": 1.0},
    "peak": {"signal": "i(l1)", "tol_pct": 5.0},
}


def _wave(n_cycles: int, decay: float | None):
    """Clock of period 1 (rising edge at t = k + 0.25), plus a mean-tracking
    ramp ``1 - decay**k`` (flat ``1.0`` when ``decay`` is None) and a peak
    signal of amplitude 2 on top of it."""
    scale, clk, mean, peak = [], [], [], []
    steps = 40
    for i in range(n_cycles * steps + 1):
        t = i / steps
        k = int(t)
        scale.append(t)
        clk.append(1.0 if (t % 1.0) >= 0.25 and (t % 1.0) < 0.75 else 0.0)
        level = 1.0 if decay is None else 1.0 - decay**k
        mean.append(level)
        peak.append(2.0 * level)
    return scale, clk, mean, peak


def _compute(n_cycles, decay, **kw):
    scale, clk, mean, peak = _wave(n_cycles, decay)
    args = {
        "level": 0.5,
        "cycles": 2,
        "mean_tol_pct": 1.0,
        "peak_tol_pct": 5.0,
    }
    args.update(kw)
    return sim_settled.compute_settled(scale, clk, mean, peak, **args)


def test_decaying_ripple_not_settled_then_settled_when_longer():
    short = _compute(8, 0.8)
    assert short.error is None and short.settled is False and short.value == 0.0
    assert short.worst_delta_pct > 1.0
    longer = _compute(80, 0.8)
    assert longer.settled is True and longer.value == 1.0
    assert longer.worst_delta_pct <= 5.0


def test_windows_are_back_to_back_and_reported():
    out = _compute(9, None)
    # edges at ~0.24, 1.24, ... 8.24 -> 9 edges -> 8 cycles -> 4 windows of 2
    assert out.windows_total == 4
    assert [w["t_start"] for w in out.windows] == pytest.approx(
        [0.2375, 2.2375, 4.2375, 6.2375]
    )
    assert out.windows[0]["t_end"] == pytest.approx(out.windows[1]["t_start"])
    assert out.windows[-1]["mean"] == pytest.approx(1.0)
    assert out.windows[-1]["peak"] == pytest.approx(2.0)
    report = out.report()
    assert report["settled"] is True and report["windows_total"] == 4
    json.dumps(report)


def test_from_skips_early_edges():
    out = _compute(12, None, start=3.0)
    assert out.windows[0]["t_start"] == pytest.approx(3.2375)


def test_insufficient_windows_and_no_clock_crossing():
    few = _compute(5, None)  # edges 0.25..4.25 -> 4 cycles -> 2 windows
    assert few.code == "settled_insufficient_windows" and few.value is None
    scale, clk, mean, peak = _wave(9, None)
    flat = sim_settled.compute_settled(
        scale, [0.0] * len(scale), mean, peak,
        level=0.5, cycles=2, mean_tol_pct=1, peak_tol_pct=1,
    )  # fmt: skip
    assert flat.code == "settled_no_clock_crossing"
    past = _compute(9, None, start=100.0)
    assert past.code == "settled_no_clock_crossing"


def test_zero_mean_handling():
    assert sim_settled.relative_delta_pct(0.0, 0.0) == 0.0
    assert math.isinf(sim_settled.relative_delta_pct(0.0, 1e-9))
    assert sim_settled.relative_delta_pct(2.0, 1.98) == pytest.approx(1.0)
    out = sim_settled.SettledOutcome(
        settled=False, mean_delta_pct=math.inf, peak_delta_pct=0.0
    )
    report = out.report()
    assert report["mean_delta_pct"] is None
    json.dumps(report, allow_nan=False)


def test_peak_uses_abs_and_interpolated_endpoints():
    scale = [0.0, 1.0, 2.0]
    assert sim_settled.window_peak(scale, [0.0, -3.0, 0.0], 0.5, 1.5) == pytest.approx(
        3.0
    )
    assert sim_settled.window_peak(scale, [0.0, 0.0, 0.0], 0.0, 2.0) == 0.0
    assert sim_settled.window_peak(scale, [0.0, 2.0, 2.0], 0.5, 0.75) == pytest.approx(
        1.5
    )


def test_validation_errors():
    sim_settled.validate_settled_spec("m", BODY)
    sim_settled.validate_settled_spec("m", {**BODY, "from": "10u", "edge": "fall"})
    for bad in (
        {**BODY, "cycles": 0},
        {**BODY, "cycles": 2.5},
        {**BODY, "cycles": True},
        {**BODY, "edge": "either"},
        {**BODY, "bogus": 1},
        {**BODY, "level": "x"},
        {**BODY, "from": "abc"},
        {k: v for k, v in BODY.items() if k != "clock"},
        {k: v for k, v in BODY.items() if k != "mean"},
        {k: v for k, v in BODY.items() if k != "peak"},
        {**BODY, "mean": {"signal": "v(a)"}},
        {**BODY, "mean": {"signal": "v(a)", "tol_pct": -1}},
        {**BODY, "peak": {"signal": "v(a)", "tol_pct": float("nan")}},
        {**BODY, "peak": {"signal": "v(a)", "tol_pct": 1, "extra": 1}},
        {**BODY, "peak": "i(l1)"},
        "not-an-object",
    ):
        with pytest.raises(SimError):
            sim_settled.validate_settled_spec("m", bad)


def test_exclusive_with_other_forms():
    with pytest.raises(SimError, match="alternative forms"):
        sim_validate._validate_measurement_form(
            {"name": "m", "expr": "v(x)", "settled": BODY}
        )
    with pytest.raises(SimError, match="alternative forms"):
        sim_validate._validate_measurement_form(
            {"name": "m", "cycle_mean": {}, "settled": BODY}
        )
    with pytest.raises(SimError, match="optional"):
        sim_validate._validate_measurement_form(
            {"name": "m", "settled": BODY, "optional": True}
        )
    sim_validate._validate_measurement_form({"name": "m", "settled": BODY})


def test_context_requires_tran():
    spec = [{"name": "m", "settled": BODY}]
    with pytest.raises(SimError, match="tran"):
        sim_settled.validate_settled_context(
            spec, {"kind": "ac", "args": "dec 10 1 1e9"}
        )
    sim_settled.validate_settled_context(spec, {"kind": "tran", "args": "1n 1u"})


def test_control_lines(tmp_path):
    lines = sim_settled.control_lines([{"name": "m", "settled": BODY}], str(tmp_path))
    assert lines == [
        "let __klt_settled_0_clk = v(clk)",
        "let __klt_settled_0_mean = v(vout)",
        "let __klt_settled_0_peak = i(l1)",
        f"write {tmp_path}/settled_0.raw __klt_settled_0_clk "
        "__klt_settled_0_mean __klt_settled_0_peak",
    ]
    assert sim_settled.control_lines([{"name": "x", "expr": "v(a)"}], "d") == []


def test_required_save_vectors_cover_all_three_signals():
    vectors = sim._required_save_vectors((), [{"name": "m", "settled": BODY}])
    assert {"v(clk)", "v(vout)", "i(l1)"} <= set(vectors)


def test_extract_from_rawfile_matches_direct_read(tmp_path):
    scale, clk, mean, peak = _wave(9, None)
    lines = [
        "Title: t",
        "Plotname: Transient Analysis",
        "Flags: real",
        "No. Variables: 4",
        f"No. Points: {len(scale)}",
        "Variables:",
        "\t0\ttime\ttime",
        "\t1\tv(clk)\tvoltage",
        "\t2\tv(vout)\tvoltage",
        "\t3\ti(l1)\tcurrent",
        "Values:",
    ]
    for i, row in enumerate(zip(scale, clk, mean, peak, strict=True)):
        lines.append(f" {i}\t{row[0]!r}")
        lines += [f"\t{v!r}" for v in row[1:]]
    (tmp_path / "settled_0.raw").write_text("\n".join(lines) + "\n")
    spec = [{"name": "m", "settled": BODY}]
    out = sim_settled.extract_corner_settleds(spec, str(tmp_path))[0]
    assert out.error is None and out.settled is True and out.windows_total == 4
    missing = sim_settled.extract_corner_settleds(spec, str(tmp_path / "nope"))[0]
    assert missing.code == "settled_vector_missing"


def test_measurement_result_shape_grading_and_error():
    spec = {"name": "m", "limits": {"min": 1}}
    result, _ = sim._settled_measurement_result(
        spec, _compute(80, 0.8), save_mode="all"
    )
    assert result["value"] == 1.0 and result["status"] == "pass"
    assert set(result["settled"]) >= {"settled", "worst_delta_pct", "windows"}
    result, _ = sim._settled_measurement_result(spec, _compute(8, 0.8), save_mode="all")
    assert result["value"] == 0.0 and result["status"] == "fail"
    result, diags = sim._settled_measurement_result(
        spec, _compute(5, None), save_mode="all"
    )
    assert result["status"] == "error" and result["value"] is None
    assert diags[0]["message"].startswith("[settled_insufficient_windows]")
    result, diags = sim._settled_measurement_result(spec, None, save_mode="all")
    assert diags[0]["message"].startswith("[settled_vector_missing]")


def test_runner_feature_gate_registered():
    assert "settled" in sim_batch._RUNNER_REQUEST_FEATURES
    assert "settled" in sim_batch._MANDATORY_RUNNER_FEATURES
    detect = sim_batch._RUNNER_REQUEST_FEATURES["settled"][1]
    assert detect({"measurements": [{"name": "e", "settled": {}}]})
    assert not detect({"measurements": [{"name": "e", "expr": "v(a)"}]})


def test_refused_with_analysis_steps(tmp_path):
    (tmp_path / "body.spice").write_text("R1 a 0 1k\n")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis_steps": [
                    {
                        "name": "s1",
                        "analysis": {"kind": "tran", "args": "1n 1u"},
                        "measurements": [{"name": "m", "settled": BODY}],
                    }
                ],
            }
        )
    )
    with pytest.raises(SimError, match="not supported with analysis_steps"):
        sim.run_sim(str(request))
