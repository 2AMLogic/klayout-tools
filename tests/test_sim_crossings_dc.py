"""Tests for ``crossings`` on a ``dc`` sweep (issue #5703)."""

from __future__ import annotations

import pytest

from klayout_tools import sim, sim_crossings

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

# A regulator-like transfer curve: tracks the input up to 2 V, then the
# output saturates at 1.8 V. Sweep 0..4 V in 0.5 V steps.
_VIN = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
_VOUT = [0.0, 0.5, 1.0, 1.5, 1.8, 1.8, 1.8, 1.8, 1.8]


def _write_raw(path, scale=_VIN, values=_VOUT):
    rows = "".join(
        f" {i}\t{x}\n\t{y}\n"
        for i, (x, y) in enumerate(zip(scale, values, strict=True))
    )
    path.write_text(
        "Title: t\nPlotname: DC transfer characteristic\nFlags: real\n"
        f"No. Variables: 2\nNo. Points: {len(scale)}\nVariables:\n"
        "\t0\tv(v-sweep)\tvoltage\n\t1\tv(out)\tvoltage\nValues:\n" + rows
    )


def _spec(level, edge="rise", **extra):
    body = {"signal": "v(out)", "level": level, "edge": edge, **extra}
    return [{"name": "vx", "crossings": body}]


_DC = {"kind": "dc", "args": "V1 0 4 0.5"}


def _run(tmp_path, spec, **raw):
    _write_raw(tmp_path / "crossings_0.raw", **raw)
    return sim_crossings.extract_corner_crossings(spec, str(tmp_path), kind="dc")[0]


def test_dc_is_accepted_and_other_kinds_still_refused():
    sim_crossings.validate_crossings_context(_spec(0.9), _DC)
    with pytest.raises(sim.SimError, match='"tran" or "dc"'):
        sim_crossings.validate_crossings_context(_spec(0.9), {"kind": "ac"})


def test_fraction_level_resolves_against_the_signal_at_sample_at(tmp_path):
    # 0.9 * v(out)(2.0 V in) = 0.9 * 1.8 = 1.62 -> crosses between 1.5 and 2.0.
    outcome = _run(tmp_path, _spec({"sample_at": 2.0, "fraction": 0.9}))
    assert outcome.error is None
    assert outcome.report()["level"] == pytest.approx(1.62)
    # 1.5 + (1.62 - 1.5) / (1.8 - 1.5) * 0.5 = 1.7
    assert outcome.graded_value() == pytest.approx(1.7)
    report = outcome.report()
    assert report["abscissae"] == pytest.approx([1.7])
    assert report["count"] == 1 and "times" not in report


def test_offset_level_works_on_the_sweep_axis(tmp_path):
    outcome = _run(tmp_path, _spec({"sample_at": 2.0, "offset": -0.8}))
    assert outcome.graded_value() == pytest.approx(1.0)


def test_value_is_first_abscissa_and_count_stays_in_member(tmp_path):
    outcome = _run(
        tmp_path,
        _spec(1.0, edge="either"),
        scale=[0, 1, 2, 3, 4],
        values=[0.0, 2.0, 0.0, 2.0, 0.0],
    )
    assert outcome.graded_value() == pytest.approx(0.5)
    assert outcome.report()["count"] == 4
    assert outcome.report()["abscissae"] == pytest.approx([0.5, 1.5, 2.5, 3.5])


def test_falling_edge(tmp_path):
    outcome = _run(
        tmp_path, _spec(1.0, edge="fall"), scale=[0, 1, 2], values=[2.0, 0.0, 0.0]
    )
    assert outcome.graded_value() == pytest.approx(0.5)


def test_descending_sweep_resolves_sample_at(tmp_path):
    outcome = _run(
        tmp_path,
        _spec({"sample_at": 4.0, "fraction": 0.5}, edge="either"),
        scale=list(reversed(_VIN)),
        values=list(reversed(_VOUT)),
    )
    assert outcome.error is None
    assert outcome.report()["level"] == pytest.approx(0.9)


def test_no_crossing_is_an_error_not_a_zero(tmp_path):
    outcome = _run(tmp_path, _spec(5.0))
    assert outcome.error is not None and "never crosses" in outcome.error
    result, diags = sim._crossings_measurement_result(
        {**_spec(5.0)[0], "limits": {"min": 0}}, outcome, save_mode="all"
    )
    assert result["status"] == "error" and result["value"] is None
    assert diags[0]["code"] == "measurement"


def test_window_bounds_are_in_sweep_units(tmp_path):
    outcome = _run(tmp_path, _spec(1.0, **{"from": 1.2}))
    assert outcome.error is not None  # the only crossing (at 1.0) is excluded


def test_limits_produce_a_status(tmp_path):
    outcome = _run(tmp_path, _spec({"sample_at": 2.0, "fraction": 0.9}))
    base = _spec(0.0)[0]
    for limits, status in (({"min": 1.5, "max": 2.0}, "pass"), ({"max": 1.6}, "fail")):
        result, _ = sim._crossings_measurement_result(
            {**base, "limits": limits}, outcome, save_mode="all"
        )
        assert result["status"] == status
        assert result["value"] == pytest.approx(1.7)


def test_per_corner_reference_varies(tmp_path):
    # Corner B saturates at 1.0 instead of 1.8 -> a different threshold.
    sat = [0.0, 0.5, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    spec = _spec({"sample_at": 4.0, "fraction": 0.5})
    a = _run(tmp_path, spec)
    b = _run(tmp_path, spec, values=sat)
    assert a.report()["level"] == pytest.approx(0.9)
    assert b.report()["level"] == pytest.approx(0.5)
    assert a.graded_value() != b.graded_value()


@pytest.mark.parametrize(
    ("level", "extra", "match"),
    [
        ({"sample_at": 9.0, "fraction": 0.9}, {}, "outside the dc sweep range"),
        (0.9, {"reduce": ["period"]}, "not supported on a dc sweep"),
    ],
)
def test_misspecified_dc_requests_are_refused_before_a_run(level, extra, match):
    with pytest.raises(sim.SimError, match=match):
        sim_crossings.validate_crossings_context(_spec(level, **extra), _DC)


def test_fraction_level_is_refused_on_tran():
    with pytest.raises(sim.SimError, match='only defined for analysis.kind "dc"'):
        sim_crossings.validate_crossings_context(
            _spec({"sample_at": 1e-9, "fraction": 0.9}), {"kind": "tran"}
        )


@pytest.mark.parametrize(
    "level",
    [
        {"sample_at": 1.0, "fraction": float("nan")},
        {"sample_at": 1.0, "fraction": True},
        {"sample_at": 1.0, "fraction": 0.5, "offset": 0.1},
    ],
)
def test_malformed_fraction_level_is_refused(level):
    with pytest.raises(sim.SimError, match="crossings.level"):
        sim._validate_measurement_forms(_spec(level))


def test_tran_value_is_still_the_count(tmp_path):
    (tmp_path / "crossings_0.raw").write_text(
        "Title: t\nPlotname: Transient Analysis\nFlags: real\n"
        "No. Variables: 2\nNo. Points: 3\nVariables:\n"
        "\t0\ttime\ttime\n\t1\tv(out)\tvoltage\nValues:\n"
        " 0\t0\n\t0\n 1\t1\n\t2\n 2\t2\n\t0\n"
    )
    outcome = sim_crossings.extract_corner_crossings(
        _spec(1.0, edge="either"), str(tmp_path)
    )[0]
    assert outcome.graded_value() == 2
    assert "times" in outcome.report()
