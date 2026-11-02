"""Tests for the sampled-offset ``crossings.level`` form (issue #4813).

Split from ``test_sim_crossings.py`` to keep that file under its size ceiling.
"""

from __future__ import annotations

import pytest

from klayout_tools import sim, sim_crossings

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _sampled_spec(sample_at=1.0, offset=-0.5):
    return [
        {
            "name": "t",
            "crossings": {
                "signal": "v(a)",
                "level": {"sample_at": sample_at, "offset": offset},
                "edge": "fall",
            },
        }
    ]


def _write_fall_raw(path):
    # 0 -> 1 V by t=1, then 1 -> 0 V by t=3 (sampled at 0, 1, 3).
    path.write_text(
        "Title: t\nPlotname: Transient Analysis\nFlags: real\n"
        "No. Variables: 2\nNo. Points: 3\nVariables:\n"
        "\t0\ttime\ttime\n\t1\tv(a)\tvoltage\nValues:\n"
        " 0\t0\n\t0\n 1\t1\n\t1\n 3\t3\n\t0\n"
    )


def test_sampled_offset_is_a_valid_declaration():
    sim._validate_measurement_forms(_sampled_spec())
    sim._validate_measurement_forms(_sampled_spec(sample_at="50n"))


@pytest.mark.parametrize(
    "level",
    [
        {"sample_at": 1.0},
        {"offset": -0.5},
        {"sample_at": 1.0, "offset": -0.5, "extra": 1},
        {"sample_at": 1.0, "offset": float("nan")},
        {"sample_at": 1.0, "offset": True},
        {"sample_at": "bogus", "offset": 0.1},
        {"sample_at": 1.0, "offset": 0.1, "frac_of": "vdd", "fraction": 0.5},
    ],
)
def test_malformed_sampled_offset_is_refused(level):
    with pytest.raises(sim.SimError, match="crossings.level"):
        sim._validate_measurement_forms(
            [{"name": "t", "crossings": {"signal": "v(a)", "level": level}}]
        )


@pytest.mark.parametrize(
    ("sample_at", "offset", "expected_level", "expected_time"),
    [
        (0.5, 0.0, 0.5, 2.0),  # interpolated between samples (0 and 1 V at t=0, 1)
        (1.0, -0.5, 0.5, 2.0),  # exactly on a sample point
        (3.0, 0.5, 0.5, 2.0),  # exactly on the last point
        (1.0, 0.5, 1.5, None),  # level never reached: zero crossings, no error
    ],
)
def test_sampled_offset_resolves_level(
    tmp_path, sample_at, offset, expected_level, expected_time
):
    _write_fall_raw(tmp_path / "crossings_0.raw")
    outcome = sim_crossings.extract_corner_crossings(
        _sampled_spec(sample_at, offset), str(tmp_path)
    )[0]
    assert outcome.error is None
    assert outcome.report()["level"] == pytest.approx(expected_level)
    if expected_time is None:
        assert outcome.times == []
    else:
        assert outcome.times == pytest.approx([expected_time])


def test_sampled_offset_out_of_range_is_a_measurement_error(tmp_path):
    _write_fall_raw(tmp_path / "crossings_0.raw")
    outcome = sim_crossings.extract_corner_crossings(
        _sampled_spec(sample_at=9.0), str(tmp_path)
    )[0]
    assert outcome.error is not None
    assert "sample_at" in outcome.error
    assert "'t'" in outcome.error
