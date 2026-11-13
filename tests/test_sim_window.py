"""The shared window helpers behind the windowed ``klt sim`` measurement forms.

The per-form cases below are the regression guard for issue #5472: before it,
``cycle_mean``, ``eye``, ``settled`` and ``step_resolution`` all shared one
bound parser but let its ``crossings`` default label leak, so a bad
``cycle_mean.from`` was reported as a ``crossings`` problem. Each form must
name itself.
"""

from __future__ import annotations

import pytest

from klayout_tools import (
    sim_crossings,
    sim_cycle_mean,
    sim_energy,
    sim_eye,
    sim_settled,
    sim_step_resolution,
    sim_stress,
    sim_window,
)
from klayout_tools.sim_errors import SimError

#: One otherwise-valid body per form, with the validator that polices it.
_FORMS = {
    "crossings": (
        sim_crossings.validate_crossings_spec,
        {"signal": "v(out)", "level": 0.9},
    ),
    "cycle_mean": (
        sim_cycle_mean.validate_cycle_mean_spec,
        {
            "signal": "v(out)",
            "align": "v(clk)",
            "level": 0.0,
            "edge": "rise",
            "cycles": 2,
        },
    ),
    "energy": (
        sim_energy.validate_energy_spec,
        {"source": "vdd", "voltage": "v(a)"},
    ),
    "eye": (sim_eye.validate_eye_spec, {"signal": "v(rx)", "ui": "100p", "uis": 8}),
    "settled": (
        sim_settled.validate_settled_spec,
        {
            "clock": "v(clk)",
            "level": 0.5,
            "cycles": 2,
            "mean": {"signal": "v(vout)", "tol_pct": 1.0},
            "peak": {"signal": "i(l1)", "tol_pct": 5.0},
        },
    ),
    "step_resolution": (
        sim_step_resolution.validate_step_resolution_spec,
        {"signal": "v(sw)", "level": 2.5, "window": "10n"},
    ),
    "stress": (sim_stress.validate_stress_spec, {"a": "d1"}),
}


@pytest.mark.parametrize("form", sorted(_FORMS))
def test_bad_from_names_its_own_form(form):
    """A bad ``from`` is reported as ``<form>.from`` -- never another form's."""
    validate, body = _FORMS[form]
    validate("m1", body)  # the body itself is valid
    with pytest.raises(SimError) as excinfo:
        validate("m1", {**body, "from": "soon"})
    message = str(excinfo.value)
    assert f"{form}.from must be a time in seconds" in message
    for other in _FORMS:
        if other != form:
            assert f"{other}." not in message


@pytest.mark.parametrize("form", sorted(_FORMS))
def test_good_from_is_accepted_in_every_form(form):
    validate, body = _FORMS[form]
    validate("m1", {**body, "from": "5n"})
    validate("m1", {**body, "from": 5e-9})


def test_window_bound_absent_is_none_and_literals_parse():
    assert sim_window.window_bound("m", {}, "from", form="energy") is None
    assert sim_window.window_bound("m", {"from": "10n"}, "from", form="energy") == (
        pytest.approx(1e-8)
    )
    assert sim_window.window_bound("m", {"from": 3}, "from", form="energy") == 3.0


def test_window_bound_refuses_non_finite():
    with pytest.raises(SimError, match="must be a time in seconds"):
        sim_window.window_bound("m", {"to": float("inf")}, "to", form="stress")


def test_field_overrides_the_reported_path():
    """``crossings.level.sample_at`` is nested, so ``key`` alone misnames it."""
    with pytest.raises(SimError) as excinfo:
        sim_window.window_bound(
            "m",
            {"sample_at": "soon"},
            "sample_at",
            form="crossings",
            field="level.sample_at",
        )
    assert "crossings.level.sample_at must be a time" in str(excinfo.value)


@pytest.mark.parametrize("form", ["energy", "stress"])
def test_window_outside_tran_names_its_own_form(form):
    declared = [{"name": "m1", form: {"from": "1n", "to": "500n"}}]
    analysis = {"kind": "tran", "args": "0.1n 100n"}
    with pytest.raises(SimError) as excinfo:
        sim_window.validate_window_inside_tran(declared, analysis, form=form)
    assert f"{form}.to (" in str(excinfo.value)
    assert "is outside the transient analysis window" in str(excinfo.value)


def test_window_inside_tran_accepts_the_edges_and_skips_unparseable_args():
    declared = [{"name": "m1", "energy": {"from": 0.0, "to": "100n"}}]
    sim_window.validate_window_inside_tran(
        declared, {"kind": "tran", "args": "0.1n 100n"}, form="energy"
    )
    # No tstop token to compare against: diagnosed where the sweep is emitted.
    sim_window.validate_window_inside_tran(
        declared, {"kind": "tran", "args": "0.1n"}, form="energy"
    )


def test_value_at_interpolates_and_clamps():
    scale, values = [0.0, 1.0, 2.0], [0.0, 10.0, 20.0]
    assert sim_window.value_at(scale, values, 0.5) == pytest.approx(5.0)
    assert sim_window.value_at(scale, values, 1.0) == pytest.approx(10.0)
    assert sim_window.value_at(scale, values, -1.0) == 0.0
    assert sim_window.value_at(scale, values, 99.0) == 20.0
    # A repeated timepoint takes the later sample rather than dividing by zero.
    assert sim_window.value_at([0.0, 1.0, 1.0], [0.0, 5.0, 7.0], 1.0) == 5.0
