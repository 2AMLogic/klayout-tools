"""Sharded ``--submit-only`` / ``--attach`` request-digest and shard-scoping
tests (issue #4878); split from test_sim_batch_async.py to keep it small."""

from __future__ import annotations

import json
import shutil

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim
from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_async as sba
from klayout_tools import sim_batch_shards as sbs
from test_sim_batch import _corner_points, _split_runner, _status, _write_body
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


def test_submit_only_into_a_dir_holding_another_requests_map_is_refused(
    tmp_path, monkeypatch
):
    """Issue #4979: a sibling sharded --submit-only into an occupied dir is
    refused with submit-only wording; the first map and the fleet are left
    untouched."""
    runner, _ = _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    request, payload = _submit_shards(tmp_path)
    manifest_path = payload["manifest_path"]
    before = open(manifest_path, "rb").read()
    calls = len(runner.calls)

    with pytest.raises(sim.SimError) as err:
        sba.submit_only(
            request=request,
            corner_points=_corner_points(10),
            netlist_path=str(_write_body(tmp_path)),
            timeout_s=120.0,
            keep_artifacts=False,
            want_waveforms=False,
            artifacts_dir=str(tmp_path / "artifacts"),
            shard_fingerprint="fp-OTHER",
        )

    message = str(err.value)
    assert err.value.code == sb.BATCH_SHARD_MANIFEST_CODE
    assert message.startswith("--submit-only:")
    assert "distinct -o directory" in message
    assert "--resume" not in message
    assert open(manifest_path, "rb").read() == before
    assert len(runner.calls) == calls  # no S3 write, no launch


# --- `--collect-partial` (issue #5000) ---------------------------------------


def _states(monkeypatch, states):
    """Answer `status.json` reads per job id (``{job_id: state}``); every
    other call goes to the fake fleet."""
    inner = sb._run_subprocess

    def _wrapped(argv, timeout_s):
        joined = " ".join(argv)
        if joined.endswith("status.json -"):
            for job_id, state in states.items():
                if job_id in joined:
                    return fake_completed(_status(state))
        return inner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _wrapped)
    return states


def _lost(corners):
    return [
        c
        for c in corners
        if any(d.get("code") == "lost_shard" for d in c.get("diagnostics") or [])
    ]


def test_collect_partial_merges_done_shards_and_lists_the_rest(tmp_path, monkeypatch):
    runner, _ = _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    request, payload = _submit_shards(tmp_path)
    jobs = [s["job_id"] for s in payload["shards"]]
    states = _states(monkeypatch, {jobs[1]: "running", jobs[2]: "interrupted"})
    manifest_before = open(payload["manifest_path"]).read()
    launches = len(runner.argvs_matching("launch"))

    corners, _, environment = _attach_shards(
        tmp_path, request, payload["manifest_path"], collect_partial=True
    )

    assert len(corners) == 10
    assert len(_lost(corners)) == 6
    assert all(c["status"] == "pass" for c in corners[:4])
    unfinished = environment["unfinished_shards"]
    assert [(u["index"], u["job_id"], u["state"]) for u in unfinished] == [
        (1, jobs[1], "running"),
        (2, jobs[2], "interrupted"),
    ]
    assert environment["resumable"] is True
    assert environment["shard_map"] == payload["manifest_path"]
    assert len(runner.argvs_matching("launch")) == launches
    # Collected state only: the two unfinished entries are untouched.
    after = json.loads(open(payload["manifest_path"]).read())
    before = json.loads(manifest_before)
    assert after["shards"][1] == before["shards"][1]
    assert after["shards"][2] == before["shards"][2]

    # A later plain attach finishes the remaining shards.
    states.clear()
    corners, _, environment = _attach_shards(
        tmp_path, request, payload["manifest_path"]
    )
    assert _lost(corners) == []
    assert "unfinished_shards" not in environment


def test_collect_partial_with_all_shards_done_equals_plain_attach(
    tmp_path, monkeypatch
):
    _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    request, payload = _submit_shards(tmp_path)

    partial, _, partial_env = _attach_shards(
        tmp_path, request, payload["manifest_path"], collect_partial=True
    )
    plain, _, _ = _attach_shards(tmp_path, request, payload["manifest_path"])

    assert partial == plain
    assert "unfinished_shards" not in partial_env


def test_collect_partial_with_no_shard_done_reports_every_unit_lost(
    tmp_path, monkeypatch
):
    _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    request, payload = _submit_shards(tmp_path)
    _states(monkeypatch, {s["job_id"]: "running" for s in payload["shards"]})

    corners, _, environment = _attach_shards(
        tmp_path, request, payload["manifest_path"], collect_partial=True
    )

    assert len(_lost(corners)) == 10
    assert len(environment["unfinished_shards"]) == 3


def test_collect_partial_never_launches_an_unlaunched_shard(tmp_path, monkeypatch):
    runner, _ = _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    request, payload = _submit_shards(tmp_path)
    document = json.loads(open(payload["manifest_path"]).read())
    document["shards"][0]["state"] = "submitted"
    open(payload["manifest_path"], "w").write(json.dumps(document))
    launches = len(runner.argvs_matching("launch"))

    _, _, environment = _attach_shards(
        tmp_path, request, payload["manifest_path"], collect_partial=True
    )

    assert environment["unfinished_shards"][0]["state"] == "unlaunched"
    assert len(runner.argvs_matching("launch")) == launches


def test_collect_partial_attaches_from_a_copied_map_in_an_empty_dir(
    tmp_path, monkeypatch
):
    """Cross-host attach: only the copied `batch-shards.json` is present."""
    _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    request, payload = _submit_shards(tmp_path)
    fresh = tmp_path / "other-host"
    fresh.mkdir()
    copied = fresh / sb.BATCH_SHARDS_FILENAME
    shutil.copy(payload["manifest_path"], copied)

    corners, _, environment = _attach_shards(
        tmp_path, request, str(copied), artifacts_dir=str(fresh)
    )

    assert len(corners) == 10
    assert _lost(corners) == []
    assert len(environment["fleet"]) == 3


def test_collect_partial_is_refused_without_a_shard_map():
    with pytest.raises(sim.SimError) as err:
        sim._apply_attach({}, None, "batch", 1, collect_partial=True)
    assert err.value.code == "collect_partial_requires_attach"
    with pytest.raises(sim.SimError):
        sim._apply_attach({}, "klt-sim-job", "batch", 1, collect_partial=True)
