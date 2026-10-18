"""Numerics of the butterfly static noise margin (issue #3895)."""

from __future__ import annotations

import math

import pytest

from helpers.snm_oracle import (
    brute_force_eye,
    tanh_inverter,
    vertical_gap_shortcut,
)
from klayout_tools import sim_snm

_H = 1e-6  # ramp half-width of the near-ideal step inverters


def _step_curve(xs: list[float], at: float) -> list[float]:
    """1 before ``at - h``, 0 after ``at + h``, linear in between."""
    out = []
    for x in xs:
        t = (x - (at - _H)) / (2 * _H)
        out.append(1.0 - min(max(t, 0.0), 1.0))
    return out


def _step_axis(*steps: float) -> list[float]:
    knots = {0.0, 1.0}
    for at in steps:
        knots.update({at - _H, at + _H})
    return sorted(knots)


def test_symmetric_step_inverters_have_the_analytic_square():
    xs = _step_axis(0.5)
    result = sim_snm.analyze_snm(xs, _step_curve(xs, 0.5), _step_curve(xs, 0.5))
    # Both eyes are 0.5 x 0.5 squares; the ramps shave O(h).
    assert result.snm == pytest.approx(0.5, abs=1e-4)
    assert result.eye_upper_left == pytest.approx(0.5, abs=1e-4)
    assert result.eye_lower_right == pytest.approx(0.5, abs=1e-4)


def test_asymmetric_pair_reports_the_smaller_eye():
    # y1 steps at 0.5, y2 steps at 0.3: eyes are 0.5 x 0.7 and 0.5 x 0.3.
    xs = _step_axis(0.5, 0.3)
    result = sim_snm.analyze_snm(xs, _step_curve(xs, 0.5), _step_curve(xs, 0.3))
    assert result.snm == pytest.approx(0.3, abs=1e-4)
    assert max(result.eye_upper_left, result.eye_lower_right) == pytest.approx(
        0.5, abs=1e-4
    )
    # Swapping the curves swaps the eyes, never the minimum.
    swapped = sim_snm.analyze_snm(xs, _step_curve(xs, 0.3), _step_curve(xs, 0.5))
    assert swapped.snm == pytest.approx(result.snm)
    assert swapped.eye_upper_left == pytest.approx(result.eye_lower_right)


def test_smooth_symmetric_pair_matches_a_geometric_search():
    xs = [i / 2000 for i in range(2001)]
    ys = [tanh_inverter(x) for x in xs]
    result = sim_snm.analyze_snm(xs, ys, ys)
    assert result.eye_upper_left == pytest.approx(result.eye_lower_right, rel=1e-6)
    assert result.snm == pytest.approx(brute_force_eye(), abs=2e-4)


def test_vertical_gap_shortcut_overreads_by_a_large_fraction():
    """The fixture would catch the ~30 % over-read class of error."""
    xs = [i / 2000 for i in range(2001)]
    ys = [tanh_inverter(x) for x in xs]
    snm = sim_snm.analyze_snm(xs, ys, ys).snm
    shortcut = vertical_gap_shortcut(xs, ys, ys)
    assert shortcut > 1.25 * snm
    assert snm == pytest.approx(brute_force_eye(), abs=2e-4)


def test_decreasing_sweep_axis_gives_the_same_value():
    xs = [i / 400 for i in range(401)]
    ys = [tanh_inverter(x) for x in xs]
    forward = sim_snm.analyze_snm(xs, ys, ys).snm
    reverse = sim_snm.analyze_snm(xs[::-1], ys[::-1], ys[::-1]).snm
    assert reverse == pytest.approx(forward, rel=1e-9)


def test_different_x_grids_are_handled_by_the_rotation_not_a_shared_axis():
    # y2 is sampled on its own (coarser) grid: build it from the same function
    # but feed the analysis a denser x so C1 and C2 knots interleave.
    xs = [i / 1000 for i in range(1001)]
    y1 = [tanh_inverter(x) for x in xs]
    y2 = [tanh_inverter(x) for x in xs]
    coarse = list(range(0, 1001, 7)) + [1000]
    cx = [xs[i] for i in coarse]
    result = sim_snm.analyze_snm(cx, [y1[i] for i in coarse], [y2[i] for i in coarse])
    assert result.snm == pytest.approx(brute_force_eye(), abs=2e-3)


@pytest.mark.parametrize(
    ("x", "y1", "y2", "reason"),
    [
        ([0, 1], [1, 0], [1, 0], sim_snm.REASON_INVALID),  # too short
        ([0, 0.5, 1], [1, 0.5], [1, 0.5, 0], sim_snm.REASON_INVALID),  # ragged
        (
            [0, 0.5, 1],
            [1, math.nan, 0],
            [1, 0.5, 0],
            sim_snm.REASON_INVALID,
        ),
        (
            [0, math.inf, 1],
            [1, 0.5, 0],
            [1, 0.5, 0],
            sim_snm.REASON_INVALID,
        ),
        (
            [0, 0.6, 0.4, 1],  # non-monotonic x
            [1, 0.5, 0.6, 0],
            [1, 0.5, 0.6, 0],
            sim_snm.REASON_NON_MONOTONIC,
        ),
        (
            [0, 0.25, 0.5, 0.75, 1],  # y1 rises: not a transfer curve
            [0, 0.5, 1, 0.5, 0],
            [1, 0.75, 0.5, 0.25, 0],
            sim_snm.REASON_NON_MONOTONIC,
        ),
        (
            [0, 0.5, 0.5, 1],  # repeated x
            [1, 1, 0, 0],
            [1, 1, 0, 0],
            sim_snm.REASON_NON_MONOTONIC,
        ),
    ],
)
def test_degenerate_input_is_an_error_with_a_reason(x, y1, y2, reason):
    with pytest.raises(sim_snm.SnmInputError) as raised:
        sim_snm.analyze_snm(x, y1, y2)
    assert raised.value.reason == reason


def test_a_single_eye_is_an_error_not_a_zero_or_pass():
    # A non-bistable pair: one inverter followed by a buffer-like identity
    # curve y = 1 - x for both -- C1 and C2 coincide, there is no eye.
    xs = [i / 10 for i in range(11)]
    line = [1 - x for x in xs]
    with pytest.raises(sim_snm.SnmInputError) as raised:
        sim_snm.analyze_snm(xs, line, line)
    assert raised.value.reason == sim_snm.REASON_NO_SECOND_EYE


def test_one_sided_butterfly_has_no_second_eye():
    # y2 is pinned high everywhere: C2 is a vertical-free line above C1's
    # lower eye, so only one eye exists.
    xs = _step_axis(0.5)
    y1 = _step_curve(xs, 0.5)
    y2 = [1.0] * len(xs)
    with pytest.raises(sim_snm.SnmInputError) as raised:
        sim_snm.analyze_snm(xs, y1, y2)
    assert raised.value.reason in (
        sim_snm.REASON_NO_SECOND_EYE,
        sim_snm.REASON_NON_MONOTONIC,
    )
