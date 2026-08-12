"""Tests for `scripts/research/flute_congestion_correlation.py` (issue #785).

Every test here exercises pure statistics/manifest logic against synthetic
`Trial`/`TrialResult` objects -- none of it needs the `klt_congestion_native`
extension or a real placement DEF (that native-extension-dependent path is
`run_pre_check`, a thin wrapper around `klayout_tools.congestion` already
covered by `test_congestion.py`'s own tests), so this file always runs,
mirroring `nets_from_def`'s own "no native extension needed" posture.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts" / "research"
sys.path.insert(0, str(SCRIPTS_DIR))

import flute_congestion_correlation as fcc  # noqa: E402

# --------------------------------------------------------------------------- #
# Trial / manifest
# --------------------------------------------------------------------------- #


def test_trial_badness_uses_drc_violation_count_when_routed():
    trial = fcc.Trial(
        name="a", place_def="a.def", status="routed", drc_violation_count=3
    )
    assert trial.badness() == 3.0


def test_trial_badness_zero_for_clean_route():
    trial = fcc.Trial(
        name="a", place_def="a.def", status="routed", drc_violation_count=0
    )
    assert trial.badness() == 0.0


def test_trial_badness_uses_sentinel_when_did_not_converge():
    trial = fcc.Trial(name="a", place_def="a.def", status="did_not_converge")
    assert trial.badness() == fcc.BADNESS_SENTINEL_DID_NOT_CONVERGE


def test_trial_badness_rejects_routed_without_drc_count():
    trial = fcc.Trial(name="a", place_def="a.def", status="routed")
    with pytest.raises(ValueError, match="requires drc_violation_count"):
        trial.badness()


def test_trial_badness_rejects_did_not_converge_with_drc_count():
    trial = fcc.Trial(
        name="a", place_def="a.def", status="did_not_converge", drc_violation_count=0
    )
    with pytest.raises(ValueError, match="must not set drc_violation_count"):
        trial.badness()


def test_load_manifest_round_trips(tmp_path):
    manifest = [
        {
            "name": "trial1",
            "place_def": "trial1.def",
            "status": "routed",
            "drc_violation_count": 0,
            "place_wallclock_s": 1.2,
            "route_wallclock_s": 71.0,
        },
        {
            "name": "trial3",
            "place_def": "trial3.def",
            "status": "did_not_converge",
            "route_wallclock_s": 2400.0,
        },
    ]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    trials = fcc.load_manifest(str(manifest_path))

    assert [t.name for t in trials] == ["trial1", "trial3"]
    assert trials[0].badness() == 0.0
    assert trials[1].badness() == fcc.BADNESS_SENTINEL_DID_NOT_CONVERGE


# --------------------------------------------------------------------------- #
# pearson_correlation
# --------------------------------------------------------------------------- #


def test_pearson_correlation_none_below_two_points():
    assert fcc.pearson_correlation([1.0], [2.0]) is None


def test_pearson_correlation_none_when_x_has_no_variance():
    assert fcc.pearson_correlation([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None


def test_pearson_correlation_perfect_positive():
    r = fcc.pearson_correlation([1.0, 2.0, 3.0], [10.0, 20.0, 30.0])
    assert r == pytest.approx(1.0)


def test_pearson_correlation_perfect_negative():
    r = fcc.pearson_correlation([1.0, 2.0, 3.0], [30.0, 20.0, 10.0])
    assert r == pytest.approx(-1.0)


# --------------------------------------------------------------------------- #
# summarize
# --------------------------------------------------------------------------- #


def _result(name, score, status, drc=None, route_s=None):
    trial = fcc.Trial(
        name=name,
        place_def=f"{name}.def",
        status=status,
        drc_violation_count=drc,
        route_wallclock_s=route_s,
    )
    return fcc.TrialResult(
        trial=trial,
        congestion_score=score,
        net_count=10,
        overflow_tile_fraction=0.0,
        max_density_um_per_um2=1.0,
        pre_check_wallclock_s=0.01,
    )


def test_summarize_correlates_higher_score_with_worse_outcome():
    results = [
        _result("clean_low", 0.2, "routed", drc=0, route_s=80.0),
        _result("clean_mid", 0.4, "routed", drc=0, route_s=150.0),
        _result("bad_high", 0.95, "did_not_converge", route_s=2400.0),
    ]
    summary = fcc.summarize(results, reject_threshold=0.8)

    assert summary["trial_count"] == 3
    assert summary["correlation"]["pearson_r"] > 0.5
    assert summary["rejected_candidates"] == ["bad_high"]
    assert summary["kept_candidates"] == ["clean_low", "clean_mid"]
    assert summary["false_rejects"] == []
    assert summary["false_keeps"] == []
    # The rejected candidate's own route wall-clock is what a DSE sweep
    # would have skipped paying for.
    assert summary["wallclock"]["route_wallclock_saved_s"] == pytest.approx(2400.0)


def test_summarize_flags_false_reject():
    # A DRC-clean candidate whose congestion score happens to clear the
    # threshold -- exactly the failure mode AC3 warns against.
    results = [
        _result("actually_clean", 0.9, "routed", drc=0, route_s=90.0),
    ]
    summary = fcc.summarize(results, reject_threshold=0.8)

    assert summary["false_rejects"] == ["actually_clean"]


def test_summarize_flags_false_keep():
    # A candidate the threshold would have kept (sent to route) that then
    # failed to converge -- a miss, not a correctness bug, but worth
    # surfacing.
    results = [
        _result("missed", 0.3, "did_not_converge", route_s=2400.0),
    ]
    summary = fcc.summarize(results, reject_threshold=0.8)

    assert summary["false_keeps"] == ["missed"]


def test_summarize_correlation_is_none_for_single_trial():
    results = [_result("only", 0.5, "routed", drc=0, route_s=80.0)]
    summary = fcc.summarize(results, reject_threshold=0.8)

    assert summary["correlation"]["pearson_r"] is None
    assert summary["correlation"]["note"] is not None
