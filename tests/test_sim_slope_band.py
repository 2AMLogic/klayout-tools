"""Numerics for ``measurements[].slope_band`` -- the largest contiguous run of
a ``dc`` sweep around an anchor over which ``dy/dx`` stays in a closed band
(issue #5518).

:mod:`klayout_tools.sim_slope_band` is a pure function over two float lists, so
every test here runs without a simulator. The ``klt sim`` wiring around it
(request validation, deck emission, grading, runner feature) is exercised in
tests/test_sim_slope_band_meas.py.
"""

from __future__ import annotations

import pytest

from klayout_tools import sim_slope_band

# A ramp that saturates at both ends: slope is exactly 1 for 0.3 < x < 1.7 and
# 0 outside it, so the in-band run and every scalar are known by hand.
_XS = [i / 100 for i in range(201)]
_YS = [max(0.3, min(1.7, x)) for x in _XS]


def _analyze(xs, ys, **kwargs):
    body = {"anchor": 1.0, "min_slope": 0.8, "max_slope": 1.2, **kwargs}
    return sim_slope_band.analyze_slope_band(xs, ys, **body)


# --------------------------------------------------------------------------- #
# Numerics
# --------------------------------------------------------------------------- #


def test_known_in_band_run_returns_every_scalar():
    result = _analyze(_XS, _YS)

    # Slope at x=0.30 spans y(0.29)=0.30 .. y(0.31)=0.31 -> 0.5, out of band;
    # at x=0.31 it spans 0.30 .. 0.32 -> 1.0, in band. Same by symmetry at the
    # top, so the run is the closed sample interval [0.31, 1.69].
    assert result.to_dict() == {
        "y_lo": pytest.approx(0.31),
        "y_hi": pytest.approx(1.69),
        "y_span": pytest.approx(1.38),
        "x_lo": pytest.approx(0.31),
        "x_hi": pytest.approx(1.69),
        "x_span": pytest.approx(1.38),
        "edge_lo_at": "band",
        "edge_hi_at": "band",
        "points_in_run": 139,
    }


@pytest.mark.parametrize("report", ["y_span", "y_lo", "y_hi", "x_span", "x_lo", "x_hi"])
def test_every_report_selects_a_scalar_of_the_same_run(report):
    result = _analyze(_XS, _YS)
    assert result.select(report) == pytest.approx(result.to_dict()[report])


def test_a_run_reaching_each_sweep_end_says_so():
    # Slope 1 everywhere: the run is the whole sweep, both edges at the end.
    straight = list(_XS)
    result = _analyze(_XS, straight)
    assert (result.edge_lo_at, result.edge_hi_at) == ("sweep_end", "sweep_end")
    assert result.points_in_run == len(_XS)

    # Saturating only at the top: the low edge runs off the start of the sweep.
    top_only = [min(1.7, x) for x in _XS]
    result = _analyze(_XS, top_only)
    assert (result.edge_lo_at, result.edge_hi_at) == ("sweep_end", "band")

    # ...and the mirror image, anchored low.
    bottom_only = [max(0.3, x) for x in _XS]
    result = _analyze(_XS, bottom_only, anchor=1.5)
    assert (result.edge_lo_at, result.edge_hi_at) == ("band", "sweep_end")


def test_descending_sweep_gives_the_identical_result():
    assert _analyze(_XS[::-1], _YS[::-1]) == _analyze(_XS, _YS)


def test_a_second_in_band_region_not_containing_the_anchor_is_ignored():
    # Slope 1 on [0.3, 0.7] and again on [1.3, 1.7], flat in between and at
    # the ends. The anchor at 0.5 selects the first region only.
    ys = []
    acc = 0.0
    for i, x in enumerate(_XS):
        if i:
            step = _XS[i] - _XS[i - 1]
            acc += step if (0.3 < x <= 0.7 or 1.3 < x <= 1.7) else 0.0
        ys.append(acc)
    result = _analyze(_XS, ys, anchor=0.5)
    assert result.x_lo == pytest.approx(0.31)
    assert result.x_hi == pytest.approx(0.69)
    assert (result.edge_lo_at, result.edge_hi_at) == ("band", "band")

    # The far region is reachable only by anchoring inside it.
    far = _analyze(_XS, ys, anchor=1.5)
    assert far.x_lo == pytest.approx(1.31)
    assert far.x_hi == pytest.approx(1.69)


def test_a_band_so_tight_only_the_anchor_is_in_it_is_a_zero_span_run():
    # y = x^2/2 on [0,2]: slope = x, so only the single sample nearest x=1
    # has a slope inside [0.995, 1.005].
    ys = [x * x / 2 for x in _XS]
    result = _analyze(_XS, ys, anchor=1.0, min_slope=0.995, max_slope=1.005)
    assert result.points_in_run == 1
    assert result.x_span == pytest.approx(0.0)
    assert result.y_span == pytest.approx(0.0)
    assert (result.edge_lo_at, result.edge_hi_at) == ("band", "band")


def test_an_anchor_at_a_sweep_end_is_accepted():
    result = _analyze(_XS, list(_XS), anchor=_XS[0])
    assert result.edge_lo_at == "sweep_end"
    assert result.x_lo == pytest.approx(_XS[0])


def test_the_anchor_is_the_nearest_sweep_point_not_an_exact_match():
    # 0.333 falls between samples; the run is anchored at 0.33.
    result = _analyze(_XS, _YS, anchor=0.333)
    assert result.x_lo == pytest.approx(0.31)


def test_slopes_are_central_differences_on_the_solvers_own_points():
    xs = [0.0, 1.0, 2.0, 4.0]
    ys = [0.0, 1.0, 4.0, 8.0]
    # ends one-sided: (1-0)/(1-0)=1 and (8-4)/(4-2)=2;
    # interior central: (4-0)/(2-0)=2 and (8-1)/(4-1)=7/3.
    assert sim_slope_band.local_slopes(xs, ys) == pytest.approx([1.0, 2.0, 7 / 3, 2.0])


@pytest.mark.parametrize(
    ("xs", "ys", "kwargs", "reason"),
    [
        (_XS, _YS[:-1], {}, sim_slope_band.REASON_INVALID),
        (_XS[:2], _YS[:2], {}, sim_slope_band.REASON_INVALID),
        (
            [0.0, 1.0, float("nan")],
            [0.0, 1.0, 2.0],
            {"anchor": 0.0},
            sim_slope_band.REASON_INVALID,
        ),
        # A repeated x value makes the local slope undefined.
        (
            [0.0, 1.0, 1.0, 2.0],
            [0.0, 1.0, 1.0, 2.0],
            {"anchor": 0.0},
            sim_slope_band.REASON_NON_MONOTONIC,
        ),
        (
            [0.0, 2.0, 1.0, 3.0],
            [0.0, 1.0, 2.0, 3.0],
            {"anchor": 0.0},
            sim_slope_band.REASON_NON_MONOTONIC,
        ),
        (_XS, _YS, {"anchor": 9.0}, sim_slope_band.REASON_ANCHOR_OUTSIDE),
        (_XS, _YS, {"anchor": -1.0}, sim_slope_band.REASON_ANCHOR_OUTSIDE),
        # The anchor sits in the flat saturated tail, outside the band.
        (_XS, _YS, {"anchor": 0.05}, sim_slope_band.REASON_ANCHOR_OUT_OF_BAND),
    ],
)
def test_each_bad_sweep_raises_its_own_reason(xs, ys, kwargs, reason):
    with pytest.raises(sim_slope_band.SlopeBandInputError) as excinfo:
        _analyze(xs, ys, **kwargs)
    assert excinfo.value.reason == reason


def test_an_out_of_band_anchor_is_an_error_never_a_zero_span():
    # The design choice behind `slope_band_anchor_out_of_band`: a span of 0
    # would grade against `limits` as a real (usually failing) number and hide
    # that the curve never satisfied the rule at the row's own anchor.
    with pytest.raises(sim_slope_band.SlopeBandInputError):
        _analyze(_XS, _YS, anchor=0.05)
