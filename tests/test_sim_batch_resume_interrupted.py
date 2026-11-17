"""``klt sim --resume`` re-submits a job that stayed ``interrupted`` instead of
re-attaching to it forever -- per shard (issue #5371) and on the unsharded
single-job path (issue #5459); split from test_sim_batch.py to keep it small."""

from __future__ import annotations

import json
import time

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim
from klayout_tools import sim_batch as sb
from test_sim_batch import (
    FAKE_BUCKET,
    _corner_points,
    _FakeRunner,
    _install_fake_batch_transport,
    _launches,
    _measurements_spec,
    _provision_script,
    _shard_resume_fixture,
    _sim_report,
    _status,
    _write_body,
)


def _stick_job_status(monkeypatch, states, *, default=None):
    """`_read_status` reports ``states[job_id]`` for those jobs, ``default``
    for every other job when one is given (how a resubmit that is itself
    reclaimed is staged), else the real state."""
    real = sb._read_status

    def fake(config, job_id, run):
        if job_id in states:
            return {"state": states[job_id]}
        if default is not None:
            return {"state": default}
        return real(config, job_id, run)

    monkeypatch.setattr(sb, "_read_status", fake)


def test_resume_resubmits_a_shard_whose_job_stays_interrupted(
    tmp_path, monkeypatch, capsys
):
    """Issue #5371: the prior run waited out the grace; resume resubmits."""
    monkeypatch.setattr(sb, "_RESUME_INTERRUPTED_GRACE_S", 0.01)
    runner, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    fleet()
    manifest = json.loads(manifest_path.read_text())
    stuck = manifest["shards"][1]["job_id"]
    launches_before = len(_launches(runner))
    _stick_job_status(monkeypatch, {stuck: "interrupted"})

    corners, _, environment = fleet(resume=True)

    assert len(_launches(runner)) == launches_before + 1
    assert [e["resumed"] for e in environment["fleet"]] == [True, False]
    assert not any(c.get("diagnostics") for c in corners)
    rewritten = json.loads(manifest_path.read_text())
    assert rewritten["shards"][0] == manifest["shards"][0]
    assert rewritten["shards"][1]["job_id"] != stuck
    assert f"batch job {stuck} stayed interrupted" in capsys.readouterr().err


def test_resume_still_reattaches_a_running_shard_and_returns_poll_timeout(
    tmp_path, monkeypatch
):
    """Issue #5371: only interrupted/failed shards are resubmitted."""
    monkeypatch.setattr(sb, "_RESUME_INTERRUPTED_GRACE_S", 0.01)
    runner, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    fleet()
    jobs = [e["job_id"] for e in json.loads(manifest_path.read_text())["shards"]]
    launches_before = len(_launches(runner))
    _stick_job_status(monkeypatch, {jobs[0]: "running", jobs[1]: "running"})

    corners, _, environment = fleet(
        resume=True,
        request={
            "netlist": "body.spice",
            "batch": {
                "bucket": FAKE_BUCKET,
                "provision_script_path": str(_provision_script(tmp_path)),
                "poll_interval_s": 0.0,
                "poll_timeout_s": 0.05,
            },
        },
    )

    assert len(_launches(runner)) == launches_before
    assert [e["resumed"] for e in environment["fleet"]] == [True, True]
    codes = {d["code"] for c in corners for d in c.get("diagnostics") or []}
    assert codes == {"batch_poll_timeout"}


# --------------------------------------------------------------------------- #
# The unsharded, single-job path (issue #5459)
# --------------------------------------------------------------------------- #


def _one_job_resume_fixture(tmp_path, monkeypatch, **batch):
    """A fake one-unit batch job and a ``run(resume=...)`` callable around
    ``_run_batch`` (1 unit, so never sharded) sharing one artifacts dir --
    the path every `klt characterize` cell takes.

    Both runs pass the *same* request document, as a real ``--resume`` rerun
    does: the receipt only re-attaches when its request digest matches.
    """
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)
    artifacts = tmp_path / "artifacts"
    request = {
        "netlist": "body.spice",
        "batch": {
            "bucket": FAKE_BUCKET,
            "provision_script_path": str(_provision_script(tmp_path)),
            "poll_interval_s": 0.0,
            **batch,
        },
    }

    def run(*, resume=False):
        return sb._run_batch(
            corner_points=_corner_points(1),
            netlist_path=str(_write_body(tmp_path)),
            models_lib=None,
            analysis={"kind": "tran", "args": "1n 1u"},
            measurements_spec=_measurements_spec(),
            timeout_s=30.0,
            keep_artifacts=False,
            want_waveforms=False,
            artifacts_dir=str(artifacts),
            max_workers=None,
            request=request,
            checkpoint=(
                sim._Checkpoint(str(tmp_path / "checkpoint.json"), "fp-1")
                if resume
                else None
            ),
        )

    return runner, run, artifacts / sb.BATCH_RECEIPT_FILENAME


def _receipt_job_id(receipt_path):
    return json.loads(receipt_path.read_text())["job_id"]


def _codes(corners):
    return {d["code"] for c in corners for d in c.get("diagnostics") or []}


def test_resume_resubmits_a_single_unit_job_that_stays_interrupted(
    tmp_path, monkeypatch, capsys
):
    """Issue #5459: #5371's policy on the ``unit_count: 1`` path. The prior
    run already waited out ``interrupted_grace_s``, so resume must not pay it
    again -- and must make progress rather than failing identically forever."""
    monkeypatch.setattr(sb, "_RESUME_INTERRUPTED_GRACE_S", 0.01)
    grace_s = 2.0
    runner, run, receipt_path = _one_job_resume_fixture(
        tmp_path, monkeypatch, interrupted_grace_s=grace_s
    )
    run()
    stuck = _receipt_job_id(receipt_path)
    launches_before = len(_launches(runner))
    _stick_job_status(monkeypatch, {stuck: "interrupted"})

    started = time.monotonic()
    corners, _, environment = run(resume=True)
    elapsed = time.monotonic() - started

    # Reported after ~one poll, not after another full `interrupted_grace_s`.
    assert elapsed < grace_s / 2
    assert len(_launches(runner)) == launches_before + 1
    assert not _codes(corners)
    assert environment["job_id"] != stuck
    assert _receipt_job_id(receipt_path) == environment["job_id"]
    err = capsys.readouterr().err
    assert f"batch job {stuck} stayed interrupted" in err
    assert "submitting the request again" in err


def test_resume_keeps_an_unbounded_wait_when_the_request_disabled_the_grace(
    tmp_path, monkeypatch
):
    """Issue #5459: ``batch.interrupted_grace_s: 0`` stays unbounded on resume
    -- the short resume grace must not switch a disabled grace back on, so the
    poll budget is what ends the run and nothing is resubmitted."""
    monkeypatch.setattr(sb, "_RESUME_INTERRUPTED_GRACE_S", 0.01)
    runner, run, receipt_path = _one_job_resume_fixture(
        tmp_path, monkeypatch, interrupted_grace_s=0, poll_timeout_s=0.05
    )
    run()
    stuck = _receipt_job_id(receipt_path)
    launches_before = len(_launches(runner))
    _stick_job_status(monkeypatch, {stuck: "interrupted"})

    corners, _, _ = run(resume=True)

    assert _codes(corners) == {"batch_poll_timeout"}
    assert len(_launches(runner)) == launches_before
    assert _receipt_job_id(receipt_path) == stuck


def test_resume_still_reattaches_a_running_single_unit_job(tmp_path, monkeypatch):
    """Issue #5459: only an interrupted/failed job is resubmitted. A job still
    running comes back as ``batch_poll_timeout`` -- resubmitting it would bill
    the same unit twice."""
    monkeypatch.setattr(sb, "_RESUME_INTERRUPTED_GRACE_S", 0.01)
    runner, run, receipt_path = _one_job_resume_fixture(
        tmp_path, monkeypatch, poll_timeout_s=0.05
    )
    run()
    attached = _receipt_job_id(receipt_path)
    launches_before = len(_launches(runner))
    _stick_job_status(monkeypatch, {attached: "running"})

    corners, _, _ = run(resume=True)

    assert len(_launches(runner)) == launches_before
    assert _codes(corners) == {"batch_poll_timeout"}
    assert _receipt_job_id(receipt_path) == attached


def test_resume_reattaches_a_done_single_unit_job_without_relaunching(
    tmp_path, monkeypatch
):
    """Issue #5459: the ordinary #5350 resume is untouched."""
    monkeypatch.setattr(sb, "_RESUME_INTERRUPTED_GRACE_S", 0.01)
    runner, run, receipt_path = _one_job_resume_fixture(tmp_path, monkeypatch)
    run()
    attached = _receipt_job_id(receipt_path)
    launches_before = len(_launches(runner))

    corners, _, environment = run(resume=True)

    assert len(_launches(runner)) == launches_before
    assert environment["job_id"] == attached
    assert not _codes(corners)
    assert _receipt_job_id(receipt_path) == attached


def test_a_resubmitted_job_interrupted_again_reports_the_new_job_id(
    tmp_path, monkeypatch
):
    """Issue #5459: one resubmit per invocation, no loop. The fresh job pays
    the full (not the shortened resume) grace, and the diagnostic names *it*."""
    monkeypatch.setattr(sb, "_RESUME_INTERRUPTED_GRACE_S", 0.01)
    runner, run, receipt_path = _one_job_resume_fixture(
        tmp_path, monkeypatch, interrupted_grace_s=0.05
    )
    run()
    stuck = _receipt_job_id(receipt_path)
    launches_before = len(_launches(runner))
    # The recorded job *and* whatever is submitted next are both reclaimed.
    _stick_job_status(monkeypatch, {}, default="interrupted")

    corners, _, _ = run(resume=True)

    assert len(_launches(runner)) == launches_before + 1  # exactly one resubmit
    assert _codes(corners) == {"batch_job_interrupted"}
    resubmitted = _receipt_job_id(receipt_path)
    assert resubmitted != stuck
    messages = [d["message"] for c in corners for d in c.get("diagnostics") or []]
    assert all(resubmitted in message and stuck not in message for message in messages)


def test_resume_resubmits_a_single_unit_job_that_had_failed(tmp_path, monkeypatch):
    """Issue #5459: the shared helper gives the one-job path the same
    "every corner is terminal -> submit afresh" rule shards already had, so a
    receipt job that is terminally ``failed`` is resubmitted too."""
    monkeypatch.setattr(sb, "_RESUME_INTERRUPTED_GRACE_S", 0.01)
    runner, run, receipt_path = _one_job_resume_fixture(tmp_path, monkeypatch)
    run()
    stuck = _receipt_job_id(receipt_path)
    launches_before = len(_launches(runner))
    _stick_job_status(monkeypatch, {stuck: "failed"})

    corners, _, environment = run(resume=True)

    assert len(_launches(runner)) == launches_before + 1
    assert environment["job_id"] != stuck
    assert not _codes(corners)


# --------------------------------------------------------------------------- #
# `--resume` harvests the retained shard map (issue #5775)
# --------------------------------------------------------------------------- #


def test_shard_map_records_each_shards_unit_ids(tmp_path, monkeypatch):
    """Issue #5775: each map entry carries its ordered corner ids, so a
    later harvest (or `--attach`) can recover what a job ran without
    re-deriving the partition that minted it."""
    _, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    fleet()

    document = json.loads(manifest_path.read_text())
    assert [entry["units"] for entry in document["shards"]] == [
        ["tt/1.800V/0C", "tt/1.800V/1C"],
        ["tt/1.800V/2C", "tt/1.800V/3C"],
    ]


def test_replanned_partition_does_not_reproduce_old_shards():
    """The finding-4 measurement (issue #5775): a shrunken dispatch set
    re-plans to *different* shard groupings -- even a contiguous suffix of
    outstanding units regroups, because the split boundaries move -- so the
    manifest's per-index digest gate cannot be what provides reuse. The
    harvest (collect before planning) is."""
    full = _corner_points(27)
    from klayout_tools.sim_remote import _shard_corner_points

    old_shards = [
        [p.corner_id for p in points]
        for points in _shard_corner_points(full, 14)
        if points
    ]
    # Six outstanding units (a contiguous suffix, the friendliest case)...
    outstanding = full[21:]
    new_shards = [
        {p.corner_id for p in points}
        for points in _shard_corner_points(outstanding, 3)
        if points
    ]
    # ...still regroup: no new shard equals an old one.
    old_sets = [set(ids) for ids in old_shards]
    assert not any(new in old_sets for new in new_shards)
    # And a non-contiguous outstanding set regroups just the same.
    scattered = [full[0], full[5], full[11]]
    scattered_shards = [
        {p.corner_id for p in points}
        for points in _shard_corner_points(scattered, 2)
        if points
    ]
    assert not any(new in old_sets for new in scattered_shards)


def _harvest_run_fixture(tmp_path, monkeypatch, *, fail_second_shard):
    """A run_sim-level sharded batch run over 4 units / 2 hosts against the
    fake S3 transport, sharing one artifacts dir. The second shard's job
    reports terminally failed when `fail_second_shard` (exit code 1), so the
    first run ends partial: shard 0 collected, shard 1's units unrun."""
    from test_sim_batch import (
        _batch_request,
        _FakeRunner,
        _install_fake_batch_transport,
        _sim_report,
        _write_body,
        _write_request,
        uploaded_request_of,
    )

    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    submitted: list[list[str]] = []
    shard_count = {"n": 0}
    failed_jobs: set[str] = set()
    inner = sb._run_subprocess

    def _shard_status(argv, timeout_s):
        joined = " ".join(argv)
        if joined.endswith("status.json -") and any(
            job in joined for job in failed_jobs
        ):
            return fake_completed(_status("failed", exit_code="1"))
        return inner(argv, timeout_s)

    def _register_shard_report(argv, timeout_s):
        captured = uploaded_request_of(argv)
        if captured is not None:
            document, job_id = captured
            ids = [
                point.corner_id
                for point in sim._corner_points_from_wire(document["_explicit_points"])
            ]
            runner.outputs_by_job[job_id] = {
                "report.json": _sim_report(corner_ids=tuple(ids))
            }
            submitted.append(ids)
            shard_count["n"] += 1
            if fail_second_shard and shard_count["n"] == 2:
                failed_jobs.add(job_id)
        return _shard_status(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _register_shard_report)
    _write_body(tmp_path)
    request = _batch_request(
        tmp_path,
        corners={"supply_v": {"vdd": [1.6, 1.8, 2.0, 2.2]}, "temperature_c": [27]},
    )
    path = _write_request(tmp_path, request)
    artifacts = tmp_path / "artifacts"

    def run(**kwargs):
        return sim.run_sim(str(path), artifacts_dir=str(artifacts), hosts=2, **kwargs)

    return runner, run, submitted, artifacts


def test_run_sim_resume_harvests_a_retained_shard_map(tmp_path, monkeypatch, capsys):
    """Issue #5775 end to end: a partial sharded run without `--resume`
    writes no checkpoint, so the `--resume` rerun must harvest the retained
    shard map -- reusing the collected shard's units, submitting only the
    outstanding ones, and merging the full matrix back in order."""
    runner, run, submitted, artifacts = _harvest_run_fixture(
        tmp_path, monkeypatch, fail_second_shard=True
    )
    first = run()
    ids = [c["corner_id"] for c in first["corners"]]
    lost_ids = sorted(c["corner_id"] for c in first["corners"] if c["status"] != "pass")
    assert first["status"] == "error"
    assert len(lost_ids) == 2
    manifest_path = artifacts / sb.BATCH_SHARDS_FILENAME
    assert manifest_path.exists()
    assert not (artifacts / "checkpoint.json").exists()
    assert first["environment"]["remote"]["resumable"] is True
    first_jobs = [
        entry["job_id"] for entry in json.loads(manifest_path.read_text())["shards"]
    ]
    submitted.clear()
    launches_before = len(_launches(runner))

    second = run(resume=True)

    # Only the two outstanding (unrun) units were submitted, as one 1-unit
    # shard each under hosts=2; the collected shard's job was never
    # re-submitted or re-launched.
    assert sorted(map(tuple, submitted)) == [(unit,) for unit in lost_ids]
    new_launches = _launches(runner)[launches_before:]
    assert all(job not in " ".join(argv) for job in first_jobs for argv in new_launches)
    assert [c["corner_id"] for c in second["corners"]] == ids
    assert all(c["status"] == "pass" for c in second["corners"])
    assert second["environment"]["resume"]["resumed_corners"] == 2
    assert "harvested 2 already-completed unit(s) from 1 shard(s)" in (
        capsys.readouterr().err
    )
    # The completed sweep leaves nothing to resume from.
    assert not manifest_path.exists()
    assert not (artifacts / "checkpoint.json").exists()


def test_run_sim_resume_with_every_shard_done_dispatches_nothing(
    tmp_path, monkeypatch, capsys
):
    """The all-done edge of the harvest (issue #5775): the first run ended
    partial with the map retained, and by the time `--resume` runs every
    recorded job is terminal -- so the harvest covers the whole matrix, the
    dispatch set is empty, and the rerun is equivalent to a plain attach."""
    from test_sim_batch import _status as _batch_status

    runner, run, submitted, artifacts = _harvest_run_fixture(
        tmp_path, monkeypatch, fail_second_shard=True
    )
    first = run()
    ids = [c["corner_id"] for c in first["corners"]]
    lost_ids = {c["corner_id"] for c in first["corners"] if c["status"] != "pass"}
    assert len(lost_ids) == 2
    manifest_path = artifacts / sb.BATCH_SHARDS_FILENAME
    failed_job = next(
        entry["job_id"]
        for entry in json.loads(manifest_path.read_text())["shards"]
        if entry["state"] != "collected"
    )
    # The interrupted shard's job later finished on the fleet (the fleet has
    # no cancel): its status now reads done, persistently.
    inner = sb._run_subprocess

    def _finished(argv, timeout_s):
        joined = " ".join(argv)
        if joined.endswith("status.json -") and failed_job in joined:
            return fake_completed(_batch_status("done"))
        return inner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _finished)
    submitted.clear()
    launches_before = len(_launches(runner))

    second = run(resume=True)

    assert submitted == []  # nothing left to dispatch
    assert len(_launches(runner)) == launches_before
    assert [c["corner_id"] for c in second["corners"]] == ids
    assert all(c["status"] == "pass" for c in second["corners"])
    assert second["environment"]["resume"]["resumed_corners"] == 4
    assert "harvested 4 already-completed unit(s) from 2 shard(s)" in (
        capsys.readouterr().err
    )
    assert not manifest_path.exists()
    assert not (artifacts / "checkpoint.json").exists()
