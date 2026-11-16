"""Analytic tests for ``crossings.reduce`` reductions (issue #56)."""

from __future__ import annotations

import math

import pytest

from klayout_tools import sim, sim_crossings
from klayout_tools.sim_crossings_reduce import reduce_crossings


def test_periodic_series_has_zero_spread():
    times = [i * 1e-9 for i in range(10)]
    r = reduce_crossings(times, "rise", ["period", "cycle_to_cycle", "tie"])
    assert r["period"]["mean"] == pytest.approx(1e-9)
    assert r["period"]["std"] == pytest.approx(0, abs=1e-18)
    assert r["cycle_to_cycle"]["rms"] == pytest.approx(0, abs=1e-18)
    assert r["tie"]["rms"] == pytest.approx(0, abs=1e-18)
    assert r["period"]["n"] == 9 and r["cycle_to_cycle"]["n"] == 8


def test_linear_drift_is_detrended_by_tie():
    times = [3e-9 + i * 1.25e-9 for i in range(8)]
    r = reduce_crossings(times, "fall", ["tie"])
    assert r["tie"]["rms"] == pytest.approx(0, abs=1e-18)
    assert r["tie"]["fit_period"] == pytest.approx(1.25e-9)


def test_displaced_edge_matches_analytic_values():
    # Five edges at 0..4 ns, edge 2 displaced by +d.
    d = 0.2e-9
    times = [0.0, 1e-9, 2e-9 + d, 3e-9, 4e-9]
    r = reduce_crossings(times, "rise", ["period", "cycle_to_cycle", "tie"])
    # periods: 1, 1+d, 1-d, 1 -> c2c: d, -2d, d
    assert r["cycle_to_cycle"]["peak"] == pytest.approx(2 * d)
    assert r["cycle_to_cycle"]["rms"] == pytest.approx(math.sqrt(6 * d * d / 3))
    # LSQ of a single displaced centre sample: fit is unchanged (centre point
    # has zero index weight), so the residual at i=2 is exactly d and the
    # others shift by -d/5 (intercept): peak-to-peak = d.
    assert r["tie"]["peak_to_peak"] == pytest.approx(d)
    assert r["tie"]["fit_period"] == pytest.approx(1e-9)
    assert r["period"]["max"] - r["period"]["min"] == pytest.approx(2 * d)


def test_either_edge_uses_full_cycle():
    times = [i * 0.5e-9 for i in range(9)]
    r = reduce_crossings(times, "either", ["period", "tie"])
    assert r["period"]["mean"] == pytest.approx(1e-9)
    assert r["tie"]["fit_period"] == pytest.approx(1e-9)


def test_too_few_crossings_omitted_not_zero():
    r = reduce_crossings([0.0, 1e-9], "rise", ["period", "cycle_to_cycle", "tie"])
    assert "omitted" in r["cycle_to_cycle"] and "omitted" in r["tie"]
    assert r["period"]["n"] == 1
    assert "omitted" in reduce_crossings([0.0], "rise", ["period"])["period"]


def test_unknown_reduce_name_refused_by_name():
    body = {"signal": "v(x)", "level": 0.5, "reduce": ["jitter"]}
    with pytest.raises(sim.SimError, match="jitter"):
        sim_crossings.validate_crossings_spec("m", body)
    body["reduce"] = "tie"
    with pytest.raises(sim.SimError, match="list"):
        sim_crossings.validate_crossings_spec("m", body)
    body["reduce"] = ["period", "tie"]
    sim_crossings.validate_crossings_spec("m", body)


def test_reduction_consumes_interpolated_not_nearest_sample_times():
    true_period = 1.0e-6
    dt = 0.07e-6  # coarse grid, ~14 points per cycle, off-commensurate
    n = 400
    scale = [i * dt for i in range(n)]
    values = [math.sin(2 * math.pi * t / true_period) for t in scale]
    out = sim_crossings.find_crossings(scale, values, 0.0, edge="rise")
    r = reduce_crossings(out.times, "rise", ["period"])
    assert abs(r["period"]["mean"] - true_period) < dt / 100
    assert r["period"]["std"] < dt / 20


def test_report_carries_reductions_only_when_set():
    o = sim_crossings.CrossingsOutcome(times=[0.0, 1.0, 2.0], edges=["rise"] * 3)
    assert "reductions" not in o.report()
    o.reductions = reduce_crossings(o.times, "rise", ["tie"])
    assert "tie" in o.report()["reductions"]
