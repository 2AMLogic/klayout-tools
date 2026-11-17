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
from test_sim_batch import (
    _corner_points,
    _measurements_spec,
    _split_runner,
    _status,
    _write_body,
    job_id_of,
)
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


# --- `--resume`'s harvest of a retained shard map (issue #5775) ---------------


def _harvest(tmp_path, request, *, fingerprint="fp-async", known=()):
    return sbs.harvest_resume_shards(
        artifacts_dir=str(tmp_path / "artifacts"),
        config=sb._resolve_batch_config(request, corner_count=10, timeout_s=120.0),
        corner_points=_corner_points(10),
        request=request,
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=120.0,
        keep_artifacts=False,
        want_waveforms=False,
        measurements_spec=_measurements_spec(),
        request_dir=None,
        fingerprint=fingerprint,
        known=frozenset(known),
    )


def _harvest_fixture(tmp_path, monkeypatch):
    runner, _ = _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    request, payload = _submit_shards(tmp_path)
    return runner, request, payload


def _report_ids(corners):
    return {c["corner_id"] for c in corners}


def test_resume_harvest_collects_terminal_shards_and_skips_the_rest(
    tmp_path, monkeypatch, capsys
):
    """Issue #5775: `--resume` harvests the retained map before dispatch --
    a terminal shard's units come back, running/interrupted ones are left
    for the dispatch, nothing is launched, and the collected entry is
    stamped `collected` in the map."""
    runner, request, payload = _harvest_fixture(tmp_path, monkeypatch)
    jobs = [s["job_id"] for s in payload["shards"]]
    _states(monkeypatch, {jobs[1]: "running", jobs[2]: "interrupted"})
    launches = len(runner.argvs_matching("launch"))
    before = json.loads(open(payload["manifest_path"]).read())

    corners, engine_version = _harvest(tmp_path, request)

    # The 10-unit/cap-4 split is 3 shards (4/4/2 units); only shard 0 is
    # terminal-done, so only its 4 units are harvested.
    assert _report_ids(corners) == {f"tt_1.800_{i}" for i in range(4)}
    assert all(c["status"] == "pass" for c in corners)
    assert engine_version == "46"
    assert len(runner.argvs_matching("launch")) == launches  # never launches
    after = json.loads(open(payload["manifest_path"]).read())
    assert after["shards"][0]["state"] == "collected"
    assert after["shards"][1] == before["shards"][1]
    assert after["shards"][2] == before["shards"][2]
    assert "harvested 4 already-completed unit(s) from 1 shard(s)" in (
        capsys.readouterr().err
    )


def test_resume_harvest_refuses_a_mismatched_fingerprint(tmp_path, monkeypatch):
    runner, request, payload = _harvest_fixture(tmp_path, monkeypatch)
    before = open(payload["manifest_path"], "rb").read()
    calls = len(runner.calls)

    with pytest.raises(sim.SimError) as err:
        _harvest(tmp_path, request, fingerprint="fp-OTHER")

    assert err.value.code == sb.BATCH_SHARD_MANIFEST_CODE
    assert str(err.value).startswith("--resume: shard map")
    assert open(payload["manifest_path"], "rb").read() == before
    assert len(runner.calls) == calls  # refused before any S3 read


def test_resume_harvest_skips_unlaunched_and_already_known_units(
    tmp_path, monkeypatch, capsys
):
    """A shard the map records `submitted` (never launched) is left for the
    dispatch to launch under its recorded id; a shard whose units the
    checkpoint already holds is not collected again."""
    runner, request, payload = _harvest_fixture(tmp_path, monkeypatch)
    jobs = [s["job_id"] for s in payload["shards"]]
    document = json.loads(open(payload["manifest_path"]).read())
    document["shards"][0]["state"] = "submitted"
    open(payload["manifest_path"], "w").write(json.dumps(document))
    # The 10-unit/cap-4 split is 3 balanced shards (4/3/3 units); shard 2's
    # units (7-9) are exactly what the checkpoint already holds.
    known = [p.corner_id for p in _corner_points(10)][7:]
    launches = len(runner.argvs_matching("launch"))

    corners, _ = _harvest(tmp_path, request, known=known)

    # Only shard 1 (units 4-6) is terminal-done, launched and not known.
    assert _report_ids(corners) == {f"tt_1.800_{i}" for i in (4, 5, 6)}
    assert len(runner.argvs_matching("launch")) == launches
    collected = {
        job_id_of(" ".join(argv))
        for argv in runner.argvs_matching("outputs/", "--recursive")
    }
    assert collected == {jobs[1]}
    assert "harvested 3 already-completed unit(s) from 1 shard(s)" in (
        capsys.readouterr().err
    )


def test_resume_harvest_leaves_a_terminally_failed_shard_outstanding(
    tmp_path, monkeypatch, capsys
):
    """A terminally failed job's corners come back unrun (so the caller's
    `_record_batch_checkpoint` filter leaves them outstanding) and its entry
    is not stamped collected."""
    runner, request, payload = _harvest_fixture(tmp_path, monkeypatch)
    jobs = [s["job_id"] for s in payload["shards"]]
    inner = sb._run_subprocess

    def _failed_first(argv, timeout_s):
        joined = " ".join(argv)
        if joined.endswith("status.json -") and jobs[0] in joined:
            return fake_completed(_status("failed", exit_code="1"))
        return inner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _failed_first)

    corners, _ = _harvest(tmp_path, request)

    shard_0_ids = [p.corner_id for p in _corner_points(10)][:4]
    shard_0 = [c for c in corners if c["corner_id"] in set(shard_0_ids)]
    assert len(shard_0) == 4
    assert {d["code"] for c in shard_0 for d in c["diagnostics"]} >= {
        "batch_job_failed"
    }
    after = json.loads(open(payload["manifest_path"]).read())
    assert after["shards"][0]["state"] != "collected"
    assert after["shards"][1]["state"] == "collected"
    assert "harvested 6 already-completed unit(s) from 2 shard(s)" in (
        capsys.readouterr().err
    )


def test_resume_harvest_on_a_legacy_map_without_units_or_state(tmp_path, monkeypatch):
    """A pre-#5775 map records neither `units` nor `state`: the digest
    fallback recovers each entry's grouping and every shard is harvested."""
    runner, request, payload = _harvest_fixture(tmp_path, monkeypatch)
    document = json.loads(open(payload["manifest_path"]).read())
    for entry in document["shards"]:
        entry.pop("units", None)
        entry.pop("state", None)
    open(payload["manifest_path"], "w").write(json.dumps(document))

    corners, _ = _harvest(tmp_path, request)

    assert len(_report_ids(corners)) == 10
    after = json.loads(open(payload["manifest_path"]).read())
    assert all(s["state"] == "collected" for s in after["shards"])


def test_resume_harvest_without_a_map_is_a_noop(tmp_path, monkeypatch):
    runner, request, payload = _harvest_fixture(tmp_path, monkeypatch)
    (tmp_path / "artifacts" / sb.BATCH_SHARDS_FILENAME).unlink()
    calls = len(runner.calls)

    assert _harvest(tmp_path, request) == ([], None)
    assert len(runner.calls) == calls
