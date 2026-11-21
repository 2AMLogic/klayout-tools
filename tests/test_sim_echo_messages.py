"""Tests for the axis-length and worker-echo error text (issue #4885)."""

from __future__ import annotations

import pytest

from klayout_tools import sim

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _legacy_corner(point: sim.CornerPoint) -> dict:
    return {
        "corner_id": point.corner_id.split("/params:")[0],
        "status": "pass",
        "diagnostics": [],
    }


def test_length_mismatch_error_says_zipped_and_lists_lengths():
    with pytest.raises(sim.SimError) as raised:
        sim._expand_corners({"params": {"r": [1], "c": [1, 2, 3]}}, [])
    message = str(raised.value)
    assert "zipped by index" in message
    assert "r=1" in message and "c=3" in message
    assert "docs/cli/sim.md" in message


def test_echo_error_names_both_causes_and_job_identity():
    points = sim._expand_corners({"params": {"r": [1, 2]}}, [])
    env = {"fleet": [{"job_id": "job-a"}, {"job_id": "job-b"}]}
    with pytest.raises(sim.SimError) as raised:
        sim._require_params_echo(points, [_legacy_corner(p) for p in points], env=env)
    message = str(raised.value)
    assert "wrong request/shard" in message
    assert "built before the parameter axis" in message
    assert "job job-a, shard 0" in message and "job job-b, shard 1" in message
    assert raised.value.code == sim.PARAMS_UNSUPPORTED_WORKER_CODE
