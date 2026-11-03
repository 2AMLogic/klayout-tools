"""Sharded ``--submit-only`` / ``--attach`` request-digest and shard-scoping
tests (issue #4878); split from test_sim_batch_async.py to keep it small."""

from __future__ import annotations

import json

import pytest

from klayout_tools import sim
from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_async as sba
from klayout_tools import sim_batch_shards as sbs
from test_sim_batch import _corner_points, _split_runner
from test_sim_batch_async import _attach_shards, _submit_shards


def test_attach_shard_map_ignores_submission_only_batch_fields(tmp_path, monkeypatch):
    """Issue #4878: `--wait-for-capacity` / `--max-concurrent-jobs` steer
    submission only and `--attach` cannot repeat them, so they must not break
    the digest."""
    runner, _ = _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    request, payload = _submit_shards(tmp_path)
    submitted_with = {
        **request,
        "batch": {**request["batch"], "capacity_wait_s": 600, "max_concurrent_jobs": 2},
    }
    assert sba.request_sha256(submitted_with) == sba.request_sha256(request)

    corners, _, _ = _attach_shards(tmp_path, submitted_with, payload["manifest_path"])

    assert len(corners) == 10


def test_attach_shard_map_mismatch_names_the_differing_field(tmp_path, monkeypatch):
    _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    request, payload = _submit_shards(tmp_path)

    other = {**request, "batch": {**request["batch"], "poll_timeout_s": 12345}}
    with pytest.raises(sim.SimError) as err:
        _attach_shards(tmp_path, other, payload["manifest_path"])

    assert "differing request field(s): batch.poll_timeout_s" in str(err.value)


def test_attach_shard_map_accepts_a_legacy_full_request_digest(tmp_path, monkeypatch):
    _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    request, payload = _submit_shards(tmp_path)
    path = payload["manifest_path"]
    document = json.loads(open(path).read())
    document["request_sha256"] = sbs.legacy_request_sha256(request)
    open(path, "w").write(json.dumps(document))

    corners, _, _ = _attach_shards(tmp_path, request, path)

    assert len(corners) == 10


def test_shard_points_for_job_scopes_to_that_shards_points(tmp_path, monkeypatch):
    _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    _, payload = _submit_shards(tmp_path)
    points = _corner_points(10)
    artifacts = str(tmp_path / "artifacts")

    second = payload["shards"][1]
    scoped = sbs.shard_points_for_job(artifacts, second["job_id"], points)

    expected = points[
        second["unit_start"] : second["unit_start"] + second["unit_count"]
    ]
    assert [p.corner_id for p in scoped] == [p.corner_id for p in expected]
    assert sbs.shard_points_for_job(artifacts, "klt-sim-unknown", points) is None
