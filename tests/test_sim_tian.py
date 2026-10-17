"""Tests for ``measurements[].tian`` -- dual-injection stability metrics over
``analysis.passes`` results (issue #3160).

Synthetic tests build the two passes' complex vectors directly. Because
``T = (Tv*Ti - 1)/(Tv + Ti + 2)`` reduces to ``(x - 1)/2`` when ``Tv == Ti ==
x``, choosing ``x = 2*T + 1`` makes a stated ``T`` (with a closed-form
crossover / margins) come out of the formula. The ``real_ngspice`` test runs
two passes on a netlist whose per-pass vectors are known in closed form.
"""

from __future__ import annotations

import cmath
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import sim, sim_passes, sim_raw, sim_tian

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

real_ngspice = pytest.mark.skipif(
    shutil.which("ngspice") is None, reason="ngspice not installed"
)

_BODY = {"voltage_pass": "pv", "current_pass": "pi", "tv": "v(tv)", "ti": "i(vi)"}


def _spec(metric="phase_margin_deg", **body):
    return {
        "name": "m",
        "tian": {**_BODY, "metric": metric, **body},
        "limits": {"min": 0},
    }


def _pass(
    name, freqs, columns, names=("frequency", "v(tv)", "i(vi)")
) -> sim_passes.PassResult:
    points = [[complex(f), *[col[k] for col in columns]] for k, f in enumerate(freqs)]
    data = sim_raw.ComplexRawfile("AC Analysis", list(names), points)
    return sim_passes.PassResult(name=name, index=0, raw_path="", data=data)


def _failed(name, error="no rawfile") -> sim_passes.PassResult:
    return sim_passes.PassResult(name=name, index=0, raw_path="", error=error)


def _run(spec, pv, pi):
    return sim_tian.extract_corner_tians([spec], [pv, pi])[0]


def _from_t(ts):
    """The ``Tv == Ti`` vector whose Tian ratio is ``ts``."""
    return [2 * t + 1 for t in ts]


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"metric": "nope"}, "tian.metric"),
        ({"tv": "v(a b)"}, "tian.tv"),
        ({"ti": "x(a)"}, "tian.ti"),
        ({"tv": "v(a,b)"}, "tian.tv"),
        ({"voltage_pass": ""}, "voltage_pass"),
        ({"current_pass": 3}, "current_pass"),
        ({"extra": 1}, "unknown tian key"),
    ],
)
def test_malformed_tian_is_rejected(patch, message):
    with pytest.raises(sim.SimError, match=message):
        sim_tian.validate_form_exclusive(_spec(**patch))


def test_tian_must_be_an_object():
    with pytest.raises(sim.SimError, match="must be an object"):
        sim_tian.validate_form_exclusive({"name": "m", "tian": "x"})


@pytest.mark.parametrize(
    "other",
    [
        {"expr": "v(o)"},
        {"spice": ".meas ac a max vdb(o)"},
        {"transfer": {"num": "v(o)", "den": "v(i)", "metric": "phase_margin_deg"}},
    ],
)
def test_exactly_one_form_per_entry(other):
    with pytest.raises(sim.SimError, match="alternative forms"):
        sim._validate_measurement_forms([{**_spec(), **other}])


def test_all_five_metrics_are_accepted_and_selectors_normalised():
    for metric in sim_tian.METRICS:
        assert sim_tian.validate_form_exclusive(_spec(metric)) is True
    assert sim_tian.normalize_selector("V(Out)") == "v(out)"
    assert sim_tian.normalize_selector("I(VFI)") == "i(vfi)"
    assert sim_tian.normalize_selector("vdb(out)") is None


def test_context_requires_known_passes_and_unique_names():
    analysis = {"kind": "ac", "passes": [{"name": "pv"}, {"name": "pi"}]}
    sim_tian.validate_tian_context([_spec()], analysis)
    with pytest.raises(sim.SimError, match="requires analysis.passes"):
        sim_tian.validate_tian_context([_spec()], {"kind": "ac"})
    with pytest.raises(sim.SimError, match="not an analysis.passes name"):
        sim_tian.validate_tian_context([_spec(voltage_pass="zz")], analysis)
    with pytest.raises(sim.SimError, match="duplicate measurement name"):
        sim_tian.validate_tian_context([_spec(), _spec("gain_margin_db")], analysis)


# --------------------------------------------------------------------------- #
# Numerics (synthetic complex tables)
# --------------------------------------------------------------------------- #

_T0, _F0 = 10.0, 1e3


def _three_pole(f):
    return _T0 / (1 + 1j * f / _F0) ** 3


_FREQS = [10 ** (k / 100) for k in range(100, 601)]  # 10 Hz .. 1 MHz, 100/dec


def _three_pole_passes():
    x = _from_t([_three_pole(f) for f in _FREQS])
    return (
        _pass("pv", _FREQS, [x, [0j] * len(x)]),
        _pass("pi", _FREQS, [[0j] * len(x), x]),
    )


def test_formula_matches_hand_computation_at_a_point():
    # Tv = 1+1j, Ti = 2-1j: num = (1+1j)(2-1j) - 1 = 2 + 1j,
    #                       den = 3 + 0j + 2 = 5  ->  T = 0.4 + 0.2j.
    pv = _pass("pv", [1.0, 2.0], [[1 + 1j, 1 + 1j], [0j, 0j]])
    pi = _pass("pi", [1.0, 2.0], [[0j, 0j], [2 - 1j, 2 - 1j]])
    vec = sim_tian.tian_vector(
        {"pv": pv, "pi": pi}, ("pv", "pi", "v(tv)", "i(vi)", "ratio")
    )
    assert not isinstance(vec, sim_tian._Failure)
    assert vec[1][0] == pytest.approx(0.4 + 0.2j, abs=1e-15)


def test_stability_metrics_match_the_closed_form_three_pole_loop():
    pv, pi = _three_pole_passes()
    results = {}
    for metric in sim_tian.METRICS:
        out = _run(_spec(metric), pv, pi)
        assert out.error is None, out.error
        results[metric] = out.value
    # |T| = 1  ->  (1 + u^2)^1.5 = T0, u = f/f0.
    u = math.sqrt(_T0 ** (2 / 3) - 1)
    pm = 180 - 3 * math.degrees(math.atan(u))
    assert results["unity_crossing_hz"] == pytest.approx(u * _F0, rel=2e-3)
    assert results["phase_at_unity_deg"] == pytest.approx(pm - 180, abs=0.05)
    assert results["phase_margin_deg"] == pytest.approx(pm, abs=0.05)
    # phase = -180 at u = sqrt(3), where |T| = T0 / 8.
    assert results["gain_margin_db"] == pytest.approx(
        -20 * math.log10(_T0 / 8), abs=0.01
    )
    # Monotone roll-off past the crossover: only the crossover itself counts.
    assert results["max_gain_above_crossover_db"] == 0.0


def test_max_gain_above_crossover_reports_a_repeak():
    freqs = [1.0, 2.0, 4.0, 8.0, 16.0]
    x = _from_t([4.0, 0.5, 2.0, 0.25, 0.1])
    pv = _pass("pv", freqs, [x, [0j] * 5])
    pi = _pass("pi", freqs, [[0j] * 5, x])
    out = _run(_spec("max_gain_above_crossover_db"), pv, pi)
    assert out.value == pytest.approx(20 * math.log10(2.0), abs=1e-9)


# --------------------------------------------------------------------------- #
# Deterministic errors (never pass a limit)
# --------------------------------------------------------------------------- #

_G = [1.0, 2.0, 4.0]
_ONE = [1 + 0j] * 3


def _code(spec, pv, pi):
    out = _run(spec, pv, pi)
    assert out.value is None and out.error
    return out.code


def test_missing_or_failed_pass():
    pv = _pass("pv", _G, [_ONE, _ONE])
    assert _code(_spec(), pv, _failed("pi")) == "tian_pass_missing"
    out = sim_tian.extract_corner_tians([_spec()], [pv])[0]
    assert out.code == "tian_pass_missing"


def test_missing_vector():
    pv = _pass("pv", _G, [_ONE, _ONE])
    pi = _pass("pi", _G, [_ONE], names=("frequency", "v(tv)"))
    assert _code(_spec(), pv, pi) == "tian_vector_missing"


def test_grid_mismatch_is_not_resampled():
    pv = _pass("pv", _G, [_ONE, _ONE])
    shifted = _pass("pi", [1.0, 2.0, 4.0000001], [_ONE, _ONE])
    assert _code(_spec(), pv, shifted) == "tian_grid_mismatch"
    short = _pass("pi", _G[:2], [_ONE[:2], _ONE[:2]])
    assert _code(_spec(), pv, short) == "tian_grid_mismatch"


@pytest.mark.parametrize("freqs", [[1.0, 1.0, 2.0], [1.0, 3.0, 2.0], [0.0, 1.0, 2.0]])
def test_invalid_grid(freqs):
    pv = _pass("pv", freqs, [_ONE, _ONE])
    pi = _pass("pi", freqs, [_ONE, _ONE])
    assert _code(_spec(), pv, pi) == "tian_grid_invalid"


def test_nonfinite_samples():
    bad = [1 + 0j, complex(float("nan"), 0), 1 + 0j]
    pv = _pass("pv", _G, [bad, _ONE])
    pi = _pass("pi", _G, [_ONE, _ONE])
    assert _code(_spec(), pv, pi) == "tian_invalid_samples"


def test_singular_denominator():
    # Tv = Ti = -1  ->  Tv + Ti + 2 == 0 at the middle sample.
    tv = [1 + 0j, -1 + 0j, 1 + 0j]
    pv = _pass("pv", _G, [tv, _ONE])
    pi = _pass("pi", _G, [_ONE, tv])
    out = _run(_spec(), pv, pi)
    assert out.code == "tian_singular_denominator"
    assert "index 1" in out.error


def test_missing_crossing_is_unavailable_not_zero():
    x = _from_t([0.5, 0.2, 0.1])
    pv = _pass("pv", _G, [x, _ONE])
    pi = _pass("pi", _G, [_ONE, x])
    out = _run(_spec("unity_crossing_hz"), pv, pi)
    assert out.value is None
    assert out.code == "tian_metric_unavailable"
    assert "no_falling_unity_crossing" in out.error


def test_unity_crossover_only_rising_is_unavailable():
    x = _from_t([0.1, 0.5, 4.0])
    pv = _pass("pv", _G, [x, _ONE])
    pi = _pass("pi", _G, [_ONE, x])
    assert _code(_spec("max_gain_above_crossover_db"), pv, pi) == (
        "tian_metric_unavailable"
    )


def test_tuple_is_computed_once_and_shared(monkeypatch):
    pv, pi = _three_pole_passes()
    calls = []
    real = sim_tian.tian_vector
    monkeypatch.setattr(
        sim_tian, "tian_vector", lambda *a, **k: calls.append(1) or real(*a, **k)
    )
    specs = [
        {**_spec(m), "name": f"m_{m}"}
        for m in ("phase_margin_deg", "gain_margin_db", "unity_crossing_hz")
    ]
    # Case-folded spellings of the same tuple still share it.
    specs.append(
        {**_spec("phase_at_unity_deg", tv="V(TV)", voltage_pass="PV"), "name": "z"}
    )
    outcomes = sim_tian.extract_corner_tians(specs, [pv, pi])
    assert len(outcomes) == 4 and all(o.error is None for o in outcomes.values())
    assert len(calls) == 1
    # A different selector tuple is a different computation.
    other = {**_spec(ti="v(tv)"), "name": "other"}
    sim_tian.extract_corner_tians([*specs, other], [pv, pi])
    assert len(calls) == 3


# --------------------------------------------------------------------------- #
# Integration through klt sim
# --------------------------------------------------------------------------- #

_R1, _R2, _R3, _C, _K = 1e3, 2e3, 1e3, 1e-7, 1000.0
_ARGS = "dec 50 10 1e7"
_PASSES = [
    {
        "name": "inj_v",
        "alter": [{"target": "VA", "value": 1}, {"target": "VB", "value": 0}],
    },
    {
        "name": "inj_i",
        "alter": [{"target": "VA", "value": 0}, {"target": "VB", "value": 1}],
    },
]
_TIAN = {
    "voltage_pass": "inj_v",
    "current_pass": "inj_i",
    "tv": "v(tvn)",
    "ti": "v(tin)",
}


def _expected_t(freq):
    y = 1 / _R1 + 1 / _R2 + 1 / _R3 + 2j * math.pi * freq * _C
    tv = _K * (1 / _R1) / y
    ti = _K * (1 / _R2) / y
    return (tv * ti - 1) / (tv + ti + 2)


def _write_request(tmp_path: Path, measurements, *, passes=_PASSES) -> Path:
    (tmp_path / "body.spice").write_text(
        "VA a 0 DC 0 AC 0\n"
        "VB b 0 DC 0 AC 0\n"
        f"R1 a o {_R1}\n"
        f"R2 b o {_R2}\n"
        f"R3 o 0 {_R3}\n"
        f"C1 o 0 {_C}\n"
        f"E1 tvn 0 o 0 {_K}\n"
        f"E2 tin 0 o 0 {_K}\n"
    )
    doc = {
        "netlist": "body.spice",
        "analysis": {"kind": "ac", "args": _ARGS, "passes": passes},
        "measurements": measurements,
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(doc))
    return path


def _crossover_oracle() -> float:
    lo, hi = 10.0, 1e7  # |T| falls through 1 in this band
    for _ in range(80):
        mid = math.sqrt(lo * hi)
        lo, hi = (mid, hi) if abs(_expected_t(mid)) > 1 else (lo, mid)
    return lo


@real_ngspice
def test_real_ngspice_dual_injection_with_passing_and_failing_limits(tmp_path):
    ugf = _crossover_oracle()
    pm = 180 + math.degrees(cmath.phase(_expected_t(ugf)))
    measurements = [
        {"name": "ugf", "tian": {**_TIAN, "metric": "unity_crossing_hz"}},
        {
            "name": "pm_ok",
            "tian": {**_TIAN, "metric": "phase_margin_deg"},
            "limits": {"min": pm - 5},
        },
        {
            "name": "pm_bad",
            "tian": {**_TIAN, "metric": "phase_margin_deg"},
            "limits": {"min": pm + 5},
        },
        {
            "name": "peak",
            "tian": {**_TIAN, "metric": "max_gain_above_crossover_db"},
            "limits": {"max": 1.0},
        },
    ]
    report = sim.run_sim(str(_write_request(tmp_path, measurements)))
    corner = report["corners"][0]
    by_name = {m["name"]: m for m in corner["measurements"]}
    assert by_name["ugf"]["value"] == pytest.approx(ugf, rel=0.02)
    assert by_name["pm_ok"]["value"] == pytest.approx(pm, abs=1.0)
    assert by_name["pm_ok"]["status"] == "pass"
    assert by_name["pm_bad"]["status"] == "fail"
    assert by_name["peak"]["value"] == 0.0 and by_name["peak"]["status"] == "pass"
    assert by_name["ugf"]["tian"]["metric"] == "unity_crossing_hz"
    assert [p["status"] for p in corner["passes"]] == ["ok", "ok"]
    assert corner["status"] == "fail"


@real_ngspice
def test_real_ngspice_unknown_vector_is_an_error_that_cannot_pass(tmp_path):
    measurements = [
        {
            "name": "pm",
            "tian": {**_TIAN, "tv": "v(nope)", "metric": "phase_margin_deg"},
            "limits": {"min": -1000},
        }
    ]
    report = sim.run_sim(str(_write_request(tmp_path, measurements)))
    corner = report["corners"][0]
    meas = corner["measurements"][0]
    assert meas["status"] == "error" and meas["value"] is None
    assert any(
        d["message"].startswith("[tian_vector_missing]") for d in corner["diagnostics"]
    )


def test_tian_with_other_forms_alongside_passes_is_still_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sim.subprocess, "run", lambda *a, **k: pytest.fail("engine must not launch")
    )
    request = _write_request(
        tmp_path,
        [
            {"name": "pm", "tian": {**_TIAN, "metric": "phase_margin_deg"}},
            {"name": "m", "expr": "v(o)"},
        ],
    )
    with pytest.raises(sim.SimError, match="only supports measurements"):
        sim.run_sim(str(request))


def test_tian_without_passes_is_refused(tmp_path):
    (tmp_path / "body.spice").write_text("R1 a 0 1k\n")
    doc = {
        "netlist": "body.spice",
        "analysis": {"kind": "ac", "args": _ARGS},
        "measurements": [
            {"name": "pm", "tian": {**_TIAN, "metric": "phase_margin_deg"}}
        ],
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(sim.SimError, match="requires analysis.passes"):
        sim.run_sim(str(path))


# --------------------------------------------------------------------------- #
# series_injection (issue #3737)
# --------------------------------------------------------------------------- #

_SERIES = {"load_node": "b", "source": "vinj"}


def _series_spec(metric="phase_margin_deg", **body):
    return {
        "name": "m",
        "tian": {
            "voltage_pass": "pv",
            "current_pass": "pi",
            "series_injection": _SERIES,
            "metric": metric,
            **body,
        },
        "limits": {"min": 0},
    }


_SERIES_NAMES = ("frequency", "v(b)", "i(vinj)")


def _series_passes(ts, split=0.4):
    """Passes whose ``x = -v(b)`` and ``y = i(vinj) + 1`` satisfy
    ``x + y == 1/(1+T)`` with ``x = split/(1+T)``."""
    xs = [split / (1 + t) for t in ts]
    ys = [(1 - split) / (1 + t) for t in ts]
    zeros = [0j] * len(ts)
    return (
        _pass("pv", _FREQS, [[-x for x in xs], zeros], names=_SERIES_NAMES),
        _pass("pi", _FREQS, [zeros, [y - 1 for y in ys]], names=_SERIES_NAMES),
    )


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"tv": "v(a)"}, "alternative forms"),
        ({"ti": "i(v1)"}, "alternative forms"),
        ({"series_injection": {"load_node": "b"}}, "series_injection must be"),
        ({"series_injection": {**_SERIES, "extra": 1}}, "series_injection must be"),
        ({"series_injection": {**_SERIES, "source": "v inj"}}, "series_injection"),
        ({"series_injection": {**_SERIES, "load_node": "b,c"}}, "series_injection"),
        ({"series_injection": {**_SERIES, "load_node": 3}}, "series_injection"),
        ({"series_injection": "b"}, "series_injection must be"),
    ],
)
def test_malformed_series_injection_is_rejected(patch, message):
    with pytest.raises(sim.SimError, match=message):
        sim_tian.validate_form_exclusive(_series_spec(**patch))


def test_neither_injection_form_is_rejected():
    spec = _series_spec()
    del spec["tian"]["series_injection"]
    with pytest.raises(sim.SimError, match="either series_injection or both tv and ti"):
        sim_tian.validate_form_exclusive(spec)


def test_valid_series_injection_is_accepted_and_echoed():
    spec = _series_spec(
        "gain_margin_db", series_injection={"load_node": "B", "source": "VInj"}
    )
    assert sim_tian.validate_form_exclusive(spec) is True
    report = sim_tian.TianOutcome().report(spec)
    assert report == {
        "voltage_pass": "pv",
        "current_pass": "pi",
        "series_injection": {"load_node": "B", "source": "VInj"},
        "metric": "gain_margin_db",
    }
    assert "tv" not in report and "ti" not in report


def test_series_ratio_recovers_t_from_x_and_y():
    ts = [_three_pole(f) for f in _FREQS]
    pv, pi = _series_passes(ts, split=0.3)
    vec = sim_tian.tian_vector(
        {"pv": pv, "pi": pi}, ("pv", "pi", "v(b)", "i(vinj)", "series")
    )
    assert not isinstance(vec, sim_tian._Failure)
    assert vec[1] == pytest.approx(ts, rel=1e-12)
    out = _run(_series_spec(), pv, pi)
    assert out.error is None
    assert out.value == pytest.approx(
        _run(_spec(), *_three_pole_passes()).value, abs=1e-9
    )


def test_series_selectors_are_case_insensitive():
    pv, pi = _series_passes([_three_pole(f) for f in _FREQS])
    spec = _series_spec(
        series_injection={"load_node": "B", "source": "VINJ"}, voltage_pass="PV"
    )
    assert _run(spec, pv, pi).error is None


def test_series_missing_current_vector():
    pv, pi = _series_passes([_three_pole(f) for f in _FREQS])
    pi = _pass("pi", _FREQS, [[0j] * len(_FREQS)], names=("frequency", "v(b)"))
    assert _code(_series_spec(), pv, pi) == "tian_vector_missing"


def test_series_singular_sum_and_invalid_samples():
    # x + y == 0 at the middle sample: v(b) = 1, i(vinj) + 1 = 1 -> i = 0.
    pv = _pass("pv", _G, [[-1 + 0j, 1 + 0j, -1 + 0j], [0j] * 3], names=_SERIES_NAMES)
    pi = _pass("pi", _G, [[0j] * 3, [0j, 0j, 0j]], names=_SERIES_NAMES)
    out = _run(_series_spec(), pv, pi)
    assert out.code == "tian_singular_denominator" and "index 1" in out.error
    nan = complex(float("nan"), 0)
    pv = _pass("pv", _G, [[-1 + 0j, nan, -1 + 0j], [0j] * 3], names=_SERIES_NAMES)
    assert _code(_series_spec(), pv, pi) == "tian_invalid_samples"


def test_series_grid_mismatch_is_not_resampled():
    pv = _pass("pv", _G, [_ONE, _ONE], names=_SERIES_NAMES)
    pi = _pass("pi", [1.0, 2.0, 4.0000001], [_ONE, _ONE], names=_SERIES_NAMES)
    assert _code(_series_spec(), pv, pi) == "tian_grid_mismatch"


def test_series_and_ratio_forms_with_same_selectors_do_not_share_a_tuple(monkeypatch):
    pv, pi = _series_passes([_three_pole(f) for f in _FREQS])
    calls = []
    real = sim_tian.tian_vector
    monkeypatch.setattr(
        sim_tian, "tian_vector", lambda *a, **k: calls.append(a[1]) or real(*a, **k)
    )
    ratio = {**_spec(tv="v(b)", ti="i(vinj)"), "name": "ratio"}
    series = {**_series_spec(), "name": "series"}
    sim_tian.extract_corner_tians(
        [ratio, series, {**series, "name": "again"}], [pv, pi]
    )
    assert [key[4] for key in calls] == ["ratio", "series"]


def test_context_checks_series_pass_names():
    analysis = {"kind": "ac", "passes": [{"name": "pv"}, {"name": "pi"}]}
    sim_tian.validate_tian_context([_series_spec()], analysis)
    with pytest.raises(sim.SimError, match="not an analysis.passes name"):
        sim_tian.validate_tian_context([_series_spec(current_pass="zz")], analysis)


# A real series-injection bench around a three-pole loop with known source /
# load impedance ratio at the break: the driver's Thevenin source ``Ee`` (gain
# -_SI_GAIN, behind _SI_RS) drives node a; Vinj (+ on a, - on b) breaks the
# loop into the load _SI_RL at b, which the buffered RC chain senses. The true
# loop gain is T(s) = _SI_GAIN * RL/(RS+RL) / prod(1 + s/wk).
_SI_GAIN, _SI_RS, _SI_RL = 10.0, 1e3, 3e3
_SI_POLES_HZ = (1e3, 3e3, 1e4)
_SI_ARGS = "dec 100 10 1e6"
_SI_BODY = (
    "Vinj a b DC 0 AC 0\n"
    "Iinj 0 b DC 0 AC 0\n"
    "Eb0 f0 0 b 0 1\n"
    "R1 f0 f1 1k\n"
    "C1 f1 0 159.155n\n"
    "Eb1 f2 0 f1 0 1\n"
    "R2 f2 f3 1k\n"
    "C2 f3 0 53.0516n\n"
    "Eb2 f4 0 f3 0 1\n"
    "R3 f4 f5 1k\n"
    "C3 f5 0 15.9155n\n"
    f"Ee e 0 f5 0 -{_SI_GAIN}\n"
    f"Rs e a {_SI_RS}\n"
    f"RL b 0 {_SI_RL}\n"
)
_SI_PASSES = [
    {
        "name": "inj_v",
        "alter": [{"target": "VINJ", "value": 1}, {"target": "IINJ", "value": 0}],
    },
    {
        "name": "inj_i",
        "alter": [{"target": "VINJ", "value": 0}, {"target": "IINJ", "value": 1}],
    },
]


def _si_t(freq):
    t = _SI_GAIN * _SI_RL / (_SI_RS + _SI_RL)
    for pole in _SI_POLES_HZ:
        t /= 1 + 1j * freq / pole
    return t


def _si_closed_form():
    lo, hi = 10.0, 1e6
    for _ in range(80):
        mid = math.sqrt(lo * hi)
        lo, hi = (mid, hi) if abs(_si_t(mid)) > 1 else (lo, mid)
    ugf = lo
    pm = 180 + math.degrees(cmath.phase(_si_t(ugf)))

    # Phase crosses -180 deg: bisect on the (monotone) unwrapped phase.
    def phase(f):
        return -sum(math.degrees(math.atan(f / p)) for p in _SI_POLES_HZ)

    lo, hi = 10.0, 1e6
    for _ in range(80):
        mid = math.sqrt(lo * hi)
        lo, hi = (mid, hi) if phase(mid) > -180 else (lo, mid)
    gm = -20 * math.log10(abs(_si_t(lo)))
    return ugf, pm, gm


def _si_control_deck(tmp_path: Path) -> dict[str, float]:
    """The same bench graded by an ngspice ``.control`` script with ``meas``."""
    deck = tmp_path / "control.cir"
    deck.write_text(
        "series injection control\n" + _SI_BODY + ".control\n"
        "alter vinj ac = 1\nalter iinj ac = 0\n"
        f"ac {_SI_ARGS}\n"
        "let x = -v(b)\n"
        "alter vinj ac = 0\nalter iinj ac = 1\n"
        f"ac {_SI_ARGS}\n"
        "let y = i(vinj) + 1\n"
        "let t = 1/(ac1.x + y) - 1\n"
        "let tdb = db(t)\nlet tph = cph(t)*180/pi\n"
        "meas ac ugf when tdb=0 fall=1\n"
        "meas ac phu find tph when tdb=0 fall=1\n"
        "meas ac gmv find tdb when tph=-180 fall=1\n"
        ".endc\n.end\n"
    )
    out = subprocess.run(
        ["ngspice", "-b", str(deck)], capture_output=True, text=True, check=True
    ).stdout
    found = {
        m.group(1): float(m.group(2))
        for m in re.finditer(r"^(ugf|phu|gmv)\s*=\s*(\S+)", out, re.MULTILINE)
    }
    return {"ugf": found["ugf"], "pm": 180 + found["phu"], "gm": -found["gmv"]}


@real_ngspice
def test_real_ngspice_series_injection_matches_closed_form_and_control_meas(tmp_path):
    ugf, pm, gm = _si_closed_form()
    (tmp_path / "body.spice").write_text(_SI_BODY)
    series = {
        "voltage_pass": "inj_v",
        "current_pass": "inj_i",
        "series_injection": {"load_node": "b", "source": "vinj"},
    }
    measurements = [
        {"name": "ugf", "tian": {**series, "metric": "unity_crossing_hz"}},
        {"name": "pm", "tian": {**series, "metric": "phase_margin_deg"}},
        {"name": "gm", "tian": {**series, "metric": "gain_margin_db"}},
        {
            "name": "peak",
            "tian": {**series, "metric": "max_gain_above_crossover_db"},
            "limits": {"max": 1.0},
        },
    ]
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "ac", "args": _SI_ARGS, "passes": _SI_PASSES},
                "measurements": measurements,
            }
        )
    )
    report = sim.run_sim(str(request), backend="local")
    corner = report["corners"][0]
    by_name = {m["name"]: m for m in corner["measurements"]}
    assert [p["status"] for p in corner["passes"]] == ["ok", "ok"]
    assert by_name["ugf"]["value"] == pytest.approx(ugf, rel=0.005)
    assert by_name["pm"]["value"] == pytest.approx(pm, abs=0.3)
    assert by_name["gm"]["value"] == pytest.approx(gm, abs=0.1)
    assert by_name["peak"]["value"] == 0.0 and by_name["peak"]["status"] == "pass"
    assert by_name["pm"]["tian"]["series_injection"] == series["series_injection"]
    # Agreement with the ngspice `.control` + `meas` extraction of the same deck
    # (meas interpolates linearly, klt in log-frequency: 0.5 % / 0.3 deg / 0.1 dB).
    control = _si_control_deck(tmp_path)
    assert by_name["ugf"]["value"] == pytest.approx(control["ugf"], rel=0.005)
    assert by_name["pm"]["value"] == pytest.approx(control["pm"], abs=0.3)
    assert by_name["gm"]["value"] == pytest.approx(control["gm"], abs=0.1)
