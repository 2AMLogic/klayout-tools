"""``measurements[].noise`` -- native noise-spectrum measurements (#2938)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from klayout_tools import sim, sim_noise

_SKIP_NO_NGSPICE = pytest.mark.skipif(
    shutil.which("ngspice") is None or os.environ.get("KLT_SKIP_NGSPICE_TESTS") == "1",
    reason="ngspice is not installed on this machine",
)


def _write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


ARGS = "v(out) V1 dec 50 10 1e7"
ANALYSIS = {"kind": "noise", "args": ARGS}


def _spec(**noise):
    return {"name": "n0", "noise": {"quantity": "inoise", **noise}}


def _validate(spec, analysis=ANALYSIS):
    sim._validate_measurement_forms([spec])
    sim_noise.validate_noise_context([spec], analysis)


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "spec, match",
    [
        (_spec(), "exactly one of 'at_hz'"),
        (_spec(at_hz=1e3, band_hz=[1e3, 1e4]), "exactly one of 'at_hz'"),
        (_spec(band_hz=[1e4, 1e3]), "lo < hi"),
        (_spec(band_hz=[1e3, 1e3]), "lo < hi"),
        (_spec(band_hz=[1e3]), "two positive"),
        (_spec(at_hz=-1), "positive finite"),
        (_spec(at_hz=True), "positive finite"),
        (_spec(at_hz=1e3, bogus=1), "unknown noise key"),
        ({"name": "n0", "noise": {"quantity": "vnoise", "at_hz": 1}}, "quantity"),
        (
            {"name": "n0", "noise": {"quantity": "inoise", "at_hz": 5}, "expr": "1"},
            "n0",
        ),
        ({"name": "n0", "noise": [1]}, "must be an object"),
        ({"name": "a.b", "noise": {"quantity": "inoise", "at_hz": 5}}, "a.b"),
    ],
)
def test_malformed_entries_fail_naming_the_entry(spec, match):
    with pytest.raises(sim.SimError, match=match) as exc:
        _validate(spec)
    assert spec["name"] in str(exc.value)


def test_frequency_outside_swept_range_is_refused():
    with pytest.raises(sim.SimError, match=r"n0.*outside the swept range"):
        _validate(_spec(at_hz=1e9))
    with pytest.raises(sim.SimError, match=r"n0.*outside the swept range"):
        _validate(_spec(band_hz=[1.0, 1e3]))


def test_noise_under_a_non_noise_analysis_is_refused():
    with pytest.raises(sim.SimError, match=r"n0.*analysis.kind \"noise\""):
        _validate(_spec(at_hz=1e3), {"kind": "ac", "args": "dec 5 1 1e6"})


def test_duplicate_name_is_refused():
    specs = [_spec(at_hz=1e3), {"name": "N0", "expr": "1"}]
    with pytest.raises(sim.SimError, match="more than once"):
        sim._validate_measurement_forms(specs)


def test_noise_is_refused_with_analysis_steps():
    from klayout_tools import sim_steps

    with pytest.raises(sim.SimError, match="analysis_steps"):
        sim_steps._refuse_unsupported(_spec(at_hz=1e3))


def test_suffixed_sweep_args_are_parsed():
    swept = sim_noise.swept_range({"args": "v(out) V1 dec 10 1k 10meg 1"})
    assert swept == ("dec", 1e3, 1e7)


# ------------------------------------------------------------------ emission


def _control(spec, analysis=ANALYSIS):
    lowered = sim_noise.lower_measurements([spec], analysis)
    return sim._corner_deck_control_lines(
        point=SimpleNamespace(supply_v={}, params={}),
        analysis=analysis,
        measurements_spec=lowered,
        raw_path=None,
        osdi_preload=(),
    )


def test_emission_has_helpers_before_the_named_let_and_hides_nothing_else():
    lines = _control(_spec(band_hz=[1e3, 1e5]))
    final = next(i for i, ln in enumerate(lines) if ln.startswith("let n0 = "))
    assert lines[final + 1] == "print n0"
    assert any("__klt_nz_n0_" in ln for ln in lines[:final])
    assert lines.index("noise " + ARGS) < final
    assert "noise1.inoise_spectrum" in "\n".join(lines)


def test_spot_form_uses_log_interpolation_on_dec_and_linear_on_lin():
    dec = "\n".join(_control(_spec(at_hz=1e3)))
    assert "ln(" in dec
    lin_analysis = {"kind": "noise", "args": "v(out) V1 lin 100 10 1e5"}
    lin = "\n".join(_control(_spec(at_hz=1e3), lin_analysis))
    assert "ln(" not in lin


def test_other_forms_are_untouched():
    spec = {"name": "m", "expr": "v(out)"}
    assert sim_noise.lower_measurements([spec], ANALYSIS) == [spec]


# --------------------------------------------------------------- integration


def _write_rc(tmp_path: Path) -> None:
    (tmp_path / "rc.spice").write_text(
        "V1 in 0 DC 0 AC 1\nR1 in out 10k\nC1 out 0 1n\n"
    )


def _run(tmp_path, analysis, measurements):
    request = _write_request(
        tmp_path,
        {"netlist": "rc.spice", "analysis": analysis, "measurements": measurements},
    )
    report = sim.run_sim(str(request))
    (corner,) = report["corners"]
    assert corner["diagnostics"] == [], corner["diagnostics"]
    return {m["name"]: m["value"] for m in corner["measurements"]}


@_SKIP_NO_NGSPICE
def test_band_value_matches_ngspice_total_on_a_band_limited_rerun(tmp_path):
    _write_rc(tmp_path)
    full = _run(
        tmp_path,
        {"kind": "noise", "args": "v(out) V1 dec 50 10 1e7"},
        [
            {"name": "band", "noise": {"quantity": "onoise", "band_hz": [1e3, 1e5]}},
            {"name": "total", "expr": "onoise_total"},
        ],
    )
    limited = _run(
        tmp_path,
        {"kind": "noise", "args": "v(out) V1 dec 50 1e3 1e5"},
        [{"name": "total", "expr": "onoise_total"}],
    )
    assert full["band"] == pytest.approx(limited["total"], rel=1e-3)
    assert full["band"] < full["total"]


@_SKIP_NO_NGSPICE
def test_band_edges_between_grid_points_stay_close_to_the_dense_answer(tmp_path):
    _write_rc(tmp_path)
    off_grid = [1234.0, 56789.0]
    coarse = _run(
        tmp_path,
        {"kind": "noise", "args": "v(out) V1 dec 50 10 1e7"},
        [{"name": "b", "noise": {"quantity": "onoise", "band_hz": off_grid}}],
    )
    limited = _run(
        tmp_path,
        {"kind": "noise", "args": f"v(out) V1 lin 20000 {off_grid[0]} {off_grid[1]}"},
        [{"name": "t", "expr": "onoise_total"}],
    )
    assert coarse["b"] == pytest.approx(limited["t"], rel=2e-3)


@_SKIP_NO_NGSPICE
def test_spot_density_matches_the_analytic_rc_spectrum(tmp_path):
    import math

    _write_rc(tmp_path)
    r, c, kt4 = 10e3, 1e-9, 4 * 1.380649e-23 * 300.15

    def analytic(f):
        return math.sqrt(kt4 * r) / math.sqrt(1 + (2 * math.pi * f * r * c) ** 2)

    vals = _run(
        tmp_path,
        {"kind": "noise", "args": "v(out) V1 dec 50 10 1e7"},
        [
            {"name": "on_grid", "noise": {"quantity": "onoise", "at_hz": 1e3}},
            {"name": "between", "noise": {"quantity": "onoise", "at_hz": 12345.0}},
            {"name": "top", "noise": {"quantity": "onoise", "at_hz": 1e7}},
            {"name": "inp", "noise": {"quantity": "inoise", "at_hz": 1e3}},
        ],
    )
    assert vals["on_grid"] == pytest.approx(analytic(1e3), rel=5e-3)
    assert vals["between"] == pytest.approx(analytic(12345.0), rel=5e-3)
    assert vals["top"] == pytest.approx(analytic(1e7), rel=5e-3)
    assert vals["inp"] == pytest.approx(analytic(1e3), rel=5e-3)  # V1 gain is 1 at 1k
    assert json.dumps(vals)
