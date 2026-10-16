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
