"""Known-answer and adversarial tests for ``klayout_tools.sim_transfer`` (#2922).

Oracles are specified independently of the implementation: each fixture is
built from stated dB / degree values via ``_t`` and the expected answers are
worked by hand in the comments. Tolerance for interpolated quantities is
``TOL`` (relative 1e-9 / absolute 1e-9); exact hits are compared with ``==``.
"""

from __future__ import annotations

import cmath
import json
import math

import pytest

from klayout_tools.sim_transfer import (
    REASON_INVALID_SAMPLES,
    REASON_NO_FALLING_UNITY_CROSSING,
    REASON_NO_PHASE_CROSSING,
    REASON_PHASE_PLATEAU,
    REASON_ZERO_DB_PLATEAU,
    TransferInputError,
    analyze_transfer,
)

TOL = 1e-9


def _t(db: float, deg: float) -> complex:
    return 10.0 ** (db / 20.0) * cmath.exp(1j * math.radians(deg))


def _run(freqs, db_deg):
    ts = [_t(db, deg) for db, deg in db_deg]
    return analyze_transfer(freqs, ts, [1.0] * len(ts))


def test_two_sample_unity_straddle_log_midpoint():
    # +20 dB @ 1 Hz, -20 dB @ 100 Hz: 0 dB at the log midpoint, 10 Hz.
    # Phase 0 -> -90 deg: -45 deg there, so PM = 135 deg.
    m = _run([1.0, 100.0], [(20.0, 0.0), (-20.0, -90.0)])
    assert m.unity_crossing_hz == pytest.approx(10.0, rel=TOL)
    assert m.phase_at_unity_deg == pytest.approx(-45.0, abs=TOL)
    assert m.phase_margin_deg == pytest.approx(135.0, abs=TOL)
    assert m.unity_crossing_count == 1
    assert m.low_frequency_magnitude == pytest.approx(10.0, rel=TOL)
    assert m.gain_margin_db is None
    assert m.reasons["gain_margin_db"] == REASON_NO_PHASE_CROSSING


def test_gain_margin_sign_positive():
    # Phase -170 -> -190 (unwrapped), dB -6 -> -14: crossing -180 at the
    # midpoint where dB = -10, so GM = +10 dB (stable).
    m = _run([1.0, 100.0], [(-6.0, -170.0), (-14.0, -190.0)])
    assert m.gain_margin_db == pytest.approx(10.0, abs=TOL)
    assert m.unity_crossing_count == 0


def test_gain_margin_sign_negative_and_wrapped_phase():
    # Phases 0, -120, -240, -300 wrap through the principal range; the
    # unwrapped branch must be recovered. dB 20, 10, 5, -5.
    # -180 crossing: midpoint of samples 1,2 -> dB 7.5 -> GM = -7.5 (unstable).
    # Unity: midpoint of samples 2,3 -> phase -270 -> PM = -90.
    m = _run(
        [1.0, 10.0, 100.0, 1000.0],
        [(20.0, 0.0), (10.0, -120.0), (5.0, -240.0), (-5.0, -300.0)],
    )
    assert [round(p) for p in m.phase_unwrapped_deg] == [0, -120, -240, -300]
    assert m.gain_margin_db == pytest.approx(-7.5, abs=TOL)
    assert m.phase_at_unity_deg == pytest.approx(-270.0, abs=TOL)
    assert m.phase_margin_deg == pytest.approx(-90.0, abs=TOL)
    # Unity is the midpoint (log) of 100 and 1000 Hz.
    assert m.unity_crossing_hz == pytest.approx(10**2.5, rel=TOL)


def test_rising_falling_mixture_first_falling_and_count():
    # dB: -10, +10, -10, +10, -10 -> 4 crossings; first falling is between
    # samples 1 and 2 (log midpoint of 10 and 100 Hz).
    m = _run(
        [1.0, 10.0, 100.0, 1e3, 1e4],
        [(-10.0, 0.0), (10.0, 0.0), (-10.0, 0.0), (10.0, 0.0), (-10.0, 0.0)],
    )
    assert m.unity_crossing_count == 4
    assert m.unity_crossing_hz == pytest.approx(10**1.5, rel=TOL)
    assert m.phase_margin_deg == pytest.approx(180.0, abs=TOL)


def test_isolated_exact_hit_counts_once():
    # 10, 0, -10 dB: exact hit at sample 1, neighbours straddle -> one fall.
    m = _run([1.0, 10.0, 100.0], [(10.0, 0.0), (0.0, -30.0), (-10.0, -60.0)])
    assert m.unity_crossing_count == 1
    assert m.unity_crossing_hz == 10.0
    assert m.phase_at_unity_deg == pytest.approx(-30.0, abs=TOL)
    assert m.phase_margin_deg == pytest.approx(150.0, abs=TOL)


def test_exact_hit_rising_direction_not_a_falling_crossing():
    m = _run([1.0, 10.0, 100.0], [(-10.0, 0.0), (0.0, 0.0), (10.0, 0.0)])
    assert m.unity_crossing_count == 1
    assert m.unity_crossing_hz is None
    assert m.reasons["unity_crossing_hz"] == REASON_NO_FALLING_UNITY_CROSSING


def test_tangent_touch_is_not_a_crossing():
    m = _run([1.0, 10.0, 100.0], [(10.0, 0.0), (0.0, 0.0), (10.0, 0.0)])
    assert m.unity_crossing_count == 0
    assert m.unity_crossing_hz is None


@pytest.mark.parametrize(
    "db",
    [[0.0, -10.0, -20.0], [10.0, 5.0, 0.0]],
    ids=["start-endpoint", "end-endpoint"],
)
def test_endpoint_hit_is_not_a_crossing(db):
    m = _run([1.0, 10.0, 100.0], [(d, 0.0) for d in db])
    assert m.unity_crossing_count == 0
    assert m.unity_crossing_hz is None


def test_zero_db_plateau_is_indeterminate():
    m = _run(
        [1.0, 10.0, 100.0, 1e3],
        [(10.0, 0.0), (0.0, -10.0), (0.0, -20.0), (-10.0, -30.0)],
    )
    assert m.zero_db_plateaus == [[1, 2]]
    for name in (
        "unity_crossing_hz",
        "phase_at_unity_deg",
        "phase_margin_deg",
        "unity_crossing_count",
    ):
        assert getattr(m, name) is None
        assert m.reasons[name] == REASON_ZERO_DB_PLATEAU
    assert m.low_frequency_magnitude == pytest.approx(10**0.5, rel=TOL)


def test_phase_plateau_blocks_gain_margin():
    # Phase exactly -90, -180, -180, -270 (principal 90 wraps to -270).
    ts = [-1j, -1.0 + 0j, -1.0 + 0j, 1j]
    m = analyze_transfer([1.0, 10.0, 100.0, 1e3], ts, [1.0] * 4)
    assert m.phase_plateaus == [[1, 2]]
    assert m.gain_margin_db is None
    assert m.reasons["gain_margin_db"] == REASON_PHASE_PLATEAU


def test_crossings_outside_sweep_not_extrapolated():
    above = _run([1.0, 10.0], [(20.0, 0.0), (10.0, -45.0)])
    assert above.unity_crossing_count == 0
    assert above.unity_crossing_hz is None
    assert above.reasons["unity_crossing_hz"] == REASON_NO_FALLING_UNITY_CROSSING
    below = _run([1.0, 10.0], [(-10.0, 0.0), (-20.0, -45.0)])
    assert below.unity_crossing_count == 0
    assert below.unity_crossing_hz is None


def test_single_sample():
    m = analyze_transfer([1.0], [2.0], [1.0])
    assert m.low_frequency_magnitude == 2.0
    assert m.unity_crossing_count == 0


def test_numerator_denominator_ratio():
    # T = num/den: (4+0j)/(2j) = -2j.
    m = analyze_transfer([1.0], [4.0], [2j])
    assert m.low_frequency_magnitude == pytest.approx(2.0)
    assert m.phase_unwrapped_deg == [pytest.approx(-90.0)]


@pytest.mark.parametrize(
    "freqs,nums,dens",
    [
        ([1.0, 2.0], [1.0], [1.0, 1.0]),
        ([1.0], [1.0, 1.0], [1.0]),
        ([], [], []),
        ([2.0, 1.0], [1, 1], [1, 1]),
        ([1.0, 1.0], [1, 1], [1, 1]),
        ([0.0, 1.0], [1, 1], [1, 1]),
        ([-1.0, 1.0], [1, 1], [1, 1]),
        ([1.0, float("nan")], [1, 1], [1, 1]),
        ([1.0, float("inf")], [1, 1], [1, 1]),
        (["a", 2.0], [1, 1], [1, 1]),
    ],
    ids=[
        "short-num",
        "short-freq",
        "empty",
        "unordered",
        "duplicate",
        "zero-freq",
        "negative-freq",
        "nan-freq",
        "inf-freq",
        "non-numeric",
    ],
)
def test_malformed_arrays_raise(freqs, nums, dens):
    with pytest.raises(TransferInputError):
        analyze_transfer(freqs, nums, dens)


@pytest.mark.parametrize(
    "bad_num,bad_den",
    [
        (1.0, 0.0),
        (float("nan"), 1.0),
        (complex(float("inf"), 0), 1.0),
        (1.0, complex(0, float("nan"))),
        (0.0, 1.0),
    ],
    ids=["zero-den", "nan-num", "inf-num", "nan-den", "zero-ratio"],
)
def test_invalid_sample_kinds(bad_num, bad_den):
    nums = [_t(10.0, 0.0), bad_num, _t(-10.0, 0.0)]
    dens = [1.0, bad_den, 1.0]
    m = analyze_transfer([1.0, 10.0, 100.0], nums, dens)
    assert m.invalid_samples == [1]
    assert m.magnitude_db[1] is None and m.phase_unwrapped_deg[1] is None
    # Nothing is interpolated across the gap: no crossing, count indeterminate.
    assert m.unity_crossing_hz is None
    assert m.unity_crossing_count is None
    assert m.reasons["unity_crossing_count"] == REASON_INVALID_SAMPLES
    assert m.reasons["unity_crossing_hz"] == REASON_INVALID_SAMPLES
    assert m.low_frequency_magnitude == pytest.approx(10**0.5, rel=TOL)


def test_crossing_before_gap_is_reported_but_count_is_not():
    nums = [_t(10.0, 0.0), _t(-10.0, -60.0), 1.0, _t(10.0, 0.0)]
    dens = [1.0, 1.0, 0.0, 1.0]
    m = analyze_transfer([1.0, 10.0, 100.0, 1e3], nums, dens)
    assert m.unity_crossing_hz == pytest.approx(10**0.5, rel=TOL)
    assert m.phase_margin_deg == pytest.approx(150.0, abs=TOL)
    assert m.unity_crossing_count is None
    assert m.reasons["unity_crossing_count"] == REASON_INVALID_SAMPLES


def test_crossing_after_gap_is_not_reported():
    # A crossing may hide in the unverifiable interval before sample 2.
    nums = [_t(10.0, 0.0), 1.0, _t(5.0, 0.0), _t(-5.0, 0.0)]
    dens = [1.0, 0.0, 1.0, 1.0]
    m = analyze_transfer([1.0, 10.0, 100.0, 1e3], nums, dens)
    assert m.unity_crossing_hz is None
    assert m.reasons["unity_crossing_hz"] == REASON_INVALID_SAMPLES
    assert m.phase_margin_deg is None


def test_first_sample_invalid_uses_first_valid_for_low_frequency():
    m = analyze_transfer([1.0, 10.0], [1.0, 3.0], [0.0, 1.0])
    assert m.low_frequency_magnitude == 3.0


def test_phase_restarts_from_principal_after_gap():
    # Before the gap the unwrapped phase has run to -360; after it, the
    # phase restarts at the principal argument (0), not -360.
    degs = [0.0, -120.0, -240.0, -360.0]
    nums = [_t(10.0, d) for d in degs] + [1.0, _t(10.0, -10.0)]
    dens = [1.0] * 4 + [0.0, 1.0]
    m = analyze_transfer([1, 2, 3, 4, 5, 6], nums, dens)
    assert m.phase_unwrapped_deg[3] == pytest.approx(-360.0, abs=1e-9)
    assert m.phase_unwrapped_deg[4] is None
    assert m.phase_unwrapped_deg[5] == pytest.approx(-10.0, abs=1e-9)


def test_all_invalid():
    m = analyze_transfer([1.0, 2.0], [1.0, 1.0], [0.0, 0.0])
    assert m.low_frequency_magnitude is None
    assert m.unity_crossing_count is None
    assert set(m.reasons) == {
        "low_frequency_magnitude",
        "unity_crossing_hz",
        "phase_at_unity_deg",
        "phase_margin_deg",
        "gain_margin_db",
        "unity_crossing_count",
    }


def test_to_dict_is_strict_json_and_null_has_reason():
    m = analyze_transfer([1.0, 10.0, 100.0], [10.0, 1.0, 0.0], [1.0, 0.0, 1.0])
    d = m.to_dict()
    json.dumps(d, allow_nan=False)
    for name in (
        "low_frequency_magnitude",
        "unity_crossing_hz",
        "phase_at_unity_deg",
        "phase_margin_deg",
        "gain_margin_db",
        "unity_crossing_count",
    ):
        assert (d[name] is None) == (name in d["reasons"])


def test_deterministic():
    args = ([1.0, 100.0], [_t(20, 0), _t(-20, -90)], [1.0, 1.0])
    assert analyze_transfer(*args).to_dict() == analyze_transfer(*args).to_dict()


# ---------------------------------------------------------------------------
# All-crossing margins (#3088). Fixtures are piecewise-linear (dB, degrees)
# against log10(f) keypoints, sampled densely; expected values are worked by
# hand in the comments.
# ---------------------------------------------------------------------------

from klayout_tools.sim_transfer import (  # noqa: E402
    REASON_CROSSING_NOT_BRACKETED,
    REASON_LOW_FREQUENCY_ON_AXIS,
    REASON_TOP_NOT_BELOW_UNITY,
    analyze_margins,
    grade_margins,
    wrap_phase_margin_deg,
)


def _dense(keys, per_decade=50):
    """keys: [(log10 f, dB, deg)] -> freqs and T samples."""
    freqs, ts = [], []
    lo, hi = keys[0][0], keys[-1][0]
    count = round((hi - lo) * per_decade)
    for i in range(count + 1):
        x = lo + (hi - lo) * i / count
        for (x0, d0, p0), (x1, d1, p1) in zip(keys, keys[1:], strict=False):
            if x0 <= x <= x1:
                w = (x - x0) / (x1 - x0)
                ts.append(_t(d0 + w * (d1 - d0), p0 + w * (p1 - p0)))
                break
        freqs.append(10.0**x)
    return freqs, ts


def _margins(keys, **kw):
    freqs, ts = _dense(keys)
    return analyze_margins(freqs, ts, [1.0] * len(ts), **kw)


# (b) conditionally stable: unity falls at logf 1.667 (PM 140), rises at
# 1.25 (see below), falls again at logf 2.6 where phase is -170 (PM 10).
COND = [(0, 20, 0), (1, -10, -60), (2, 30, -200), (3, -20, -150), (5, -80, -150)]
# (c) unstable: phase hits -180 at logf 1.333 where |T| = +3.3 dB.
UNSTABLE = [(0, 30, 0), (2, 10, -270), (5, -50, -270)]
# (a) simple loop: -20 dB/dec, phase 0 -> -135.
SIMPLE = [(0, 40, -5), (4, -40, -125), (6, -80, -135)]


def test_wrap_phase_margin_is_modulo_360():
    assert wrap_phase_margin_deg(-135.0) == pytest.approx(45.0)
    assert wrap_phase_margin_deg(-400.0) == pytest.approx(140.0)  # = -40
    assert wrap_phase_margin_deg(170.0) == pytest.approx(-10.0)  # = -190


def test_simple_loop_single_crossing_matches_first_crossing_metrics():
    freqs, ts = _dense(SIMPLE)
    first = analyze_transfer(freqs, ts, [1.0] * len(ts))
    rep = analyze_margins(freqs, ts, [1.0] * len(ts))
    assert len(rep.phase_margins) == 1
    assert rep.phase_margins[0]["freq_hz"] == pytest.approx(first.unity_crossing_hz)
    assert rep.phase_margin_min_deg == pytest.approx(first.phase_margin_deg)
    assert rep.gain_margins == [] and rep.gain_margin_min_db is None
    assert rep.reasons["gain_margin_min_db"] == "no_phase_crossing"
    assert rep.ambiguity == [] and rep.encirclements == 0
    assert grade_margins(rep, min_phase_margin_deg=45.0) == "pass"


def test_conditionally_stable_min_pm_below_first_crossing_pm():
    rep = _margins(COND)
    assert [p["direction"] for p in rep.phase_margins] == ["fall", "rise", "fall"]
    first, second, third = (p["phase_margin_deg"] for p in rep.phase_margins)
    assert first == pytest.approx(140.0, abs=0.5)  # phase -40 at logf 1.667
    assert second == pytest.approx(85.0, abs=0.5)  # phase -95 at logf 1.25
    assert third == pytest.approx(10.0, abs=0.5)  # phase -170 at logf 2.6
    assert rep.phase_margin_min_deg == pytest.approx(third)
    # -180 crossed down then back up, both with |T| > 1: net encirclement 0.
    assert [g["direction"] for g in rep.gain_margins] == ["down", "up"]
    assert all(g["gain_margin_db"] < 0 for g in rep.gain_margins)
    assert rep.encirclements == 0
    # First-crossing PM passes a 45 deg limit; the all-crossing minimum fails.
    assert analyze_transfer(*_split(COND)).phase_margin_deg > 45.0
    assert grade_margins(rep, min_phase_margin_deg=45.0) == "fail"
    assert grade_margins(rep, min_phase_margin_deg=5.0) == "pass"


def _split(keys):
    freqs, ts = _dense(keys)
    return freqs, ts, [1.0] * len(ts)


def test_unstable_loop_has_clockwise_encirclements():
    rep = _margins(UNSTABLE)
    assert rep.encirclements == 2
    assert rep.gain_margin_min_db < 0.0
    assert rep.phase_margin_min_deg < 0.0
    assert grade_margins(rep, require_no_encirclement=True) == "fail"


def test_encirclement_separates_conditional_from_unstable_without_unwrap():
    # Same crossing count machinery, different -180 bookkeeping.
    assert _margins(COND).encirclements == 0
    assert _margins(UNSTABLE).encirclements == 2


def test_no_phase_crossing_is_neither_failure_nor_infinite():
    rep = _margins([(0, 20, 0), (3, -40, -90), (5, -80, -90)])
    assert rep.gain_margin_min_db is None
    assert rep.reasons["gain_margin_min_db"] == "no_phase_crossing"
    assert rep.encirclements == 0
    assert grade_margins(rep, min_gain_margin_db=6.0) == "pass"


def test_top_above_unity_is_ambiguous_not_pass():
    rep = _margins([(0, 40, 0), (3, 10, -100)])
    assert rep.band["mag_below_unity_at_top"] is False
    assert REASON_TOP_NOT_BELOW_UNITY in rep.ambiguity
    assert rep.encirclements is None
    assert rep.to_dict()["encirclement"] == "indeterminate"
    assert grade_margins(rep, min_phase_margin_deg=45.0) == "ambiguous"


def test_coarse_sampling_is_ambiguous():
    freqs, ts = _dense(SIMPLE, per_decade=1)
    rep = analyze_margins(freqs, ts, [1.0] * len(ts))
    assert rep.band["crossings_bracketed"] is False
    assert REASON_CROSSING_NOT_BRACKETED in rep.ambiguity
    assert grade_margins(rep, min_phase_margin_deg=45.0) == "ambiguous"


def test_ambiguous_is_distinct_from_pass_and_fail():
    amb = _margins([(0, 40, 0), (3, 10, -100)])
    assert grade_margins(amb, min_phase_margin_deg=45.0) not in ("pass", "fail")
    # A found violation is still a sound fail even with an unclean band.
    bad = _margins([(0, 40, 0), (3, 10, -100)])
    bad.phase_margin_min_deg = 5.0
    assert grade_margins(bad, min_phase_margin_deg=45.0) == "fail"


def test_benign_high_frequency_phase_excursion_is_not_a_negative_margin():
    # Phase dips to -200 where |T| = -30 dB, then recovers. Unity PM is the
    # clean low-frequency one; the -180 crossings carry +30 dB margin.
    keys = [
        (0, 20, -20),
        (2, -20, -100),
        (3, -30, -200),
        (4, -40, -100),
        (6, -80, -100),
    ]
    rep = _margins(keys)
    assert len(rep.phase_margins) == 1
    assert rep.phase_margin_min_deg > 60.0
    assert len(rep.gain_margins) == 2
    assert all(g["gain_margin_db"] > 25.0 for g in rep.gain_margins)
    assert rep.encirclements == 0
    assert (
        grade_margins(rep, min_phase_margin_deg=45.0, min_gain_margin_db=6.0) == "pass"
    )


def test_first_sample_exactly_on_negative_real_axis_is_ambiguous():
    # T starts at +20 dB exactly at -180 deg (offset snaps to 0.0), then |T|
    # crosses unity with a healthy margin: only the start sits on the line.
    rep = _margins([(0, 20, -180), (2, -10, -100), (4, -50, -100)])
    assert rep.band["low_freq_mag_db"] == pytest.approx(20.0)
    assert REASON_LOW_FREQUENCY_ON_AXIS in rep.ambiguity
    assert grade_margins(rep, min_phase_margin_deg=45.0) == "ambiguous"


@pytest.mark.parametrize(
    ("deg", "flagged"), [(-180.0 + 1e-10, True), (-170.0, True), (-100.0, False)]
)
def test_low_frequency_axis_guard_boundaries(deg, flagged):
    rep = _margins([(0, 20, deg), (2, -20, -100), (4, -60, -100)])
    assert (REASON_LOW_FREQUENCY_ON_AXIS in rep.ambiguity) is flagged


def test_margins_edge_cases():
    # Single point, invalid sample, zero-magnitude, plateau, duplicates.
    rep = analyze_margins([1.0], [2.0], [1.0])
    assert rep.phase_margins == [] and "top_not_below_unity" in rep.ambiguity
    rep = analyze_margins([1.0, 10.0, 100.0], [2.0, 0.0, 0.5], [1.0] * 3)
    assert "invalid_samples" in rep.ambiguity
    rep = analyze_margins([1.0, 10.0, 100.0], [2.0, 1.0, 1.0], [1.0] * 3)
    assert rep.reasons["phase_margin_min_deg"] == REASON_ZERO_DB_PLATEAU
    assert grade_margins(rep) == "ambiguous"
    with pytest.raises(TransferInputError):
        analyze_margins([1.0, 1.0], [1.0, 1.0], [1.0, 1.0])
    json.dumps(analyze_margins(*_split(COND)).to_dict(), allow_nan=False)
