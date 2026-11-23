"""`--resume`'s harvest of a retained batch shard map, end to end through
`run_sim` (issue #5775): a partial sharded run leaves its shard map behind,
and the `--resume` rerun reuses the collected shard's units, submits only the
outstanding ones, and merges the full matrix back in order. Split out of
test_sim_batch_resume_harvest.py (which covers `harvest_resume_shards`
itself) to keep both modules under the per-file size ratchet."""

from __future__ import annotations

import json

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim
from klayout_tools import sim_batch as sb
from test_sim_batch import (
    _batch_request,
    _capture,
    _FakeRunner,
    _install_fake_batch_transport,
    _launches,
    _sim_report,
    _status,
    _write_body,
    _write_request,
    uploaded_request_of,
)

pytestmark = pytest.mark.usefixtures("bare_name_ngspice_resolution")


def _harvest_run_fixture(tmp_path, monkeypatch, *, fail_second_shard):
    """A run_sim-level sharded batch run over 4 units / 2 hosts against the
    fake S3 transport, sharing one artifacts dir. The second shard's job
    reports terminally failed when `fail_second_shard` (exit code 1), so the
    first run ends partial: shard 0 collected, shard 1's units unrun."""
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
                "report.json": _sim_report(corner_ids=tuple(ids)),
                "worker_environment.json": _capture(cpu_model=job_id),
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
    harvested_job = next(
        entry["job_id"]
        for entry in json.loads(manifest_path.read_text())["shards"]
        if entry["state"] == "collected"
    )
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
    assert second["environment"]["models_lib_sha256_scope"] == "client"
    fleet = second["environment"]["remote"]["fleet"]
    assert len(fleet) == 3  # harvested shard plus the two replacement shards
    assert fleet[0]["job_id"] == harvested_job
    assert set(first_jobs) & {entry["job_id"] for entry in fleet} == {harvested_job}
    assert [entry["resumed"] for entry in fleet] == [True, False, False]
    assert len({entry["job_id"] for entry in fleet}) == 3
    assert fleet[0]["instance_id"] == "i-0fake"
    assert fleet[0]["worker_environment"]["cpu_model"] == harvested_job
    assert all(entry["worker_environment"]["capture"] == "ok" for entry in fleet)
    assert fleet[0]["client_submitted_at"] is None
    assert all(entry["client_collected_at"] for entry in fleet)
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
            return fake_completed(_status("done"))
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
    assert second["environment"]["models_lib_sha256_scope"] == "client"
    fleet = second["environment"]["remote"]["fleet"]
    assert [entry["job_id"] for entry in fleet] == [
        entry["job_id"] for entry in first["environment"]["remote"]["fleet"]
    ]
    assert all(entry["resumed"] for entry in fleet)
    assert all(entry["client_collected_at"] for entry in fleet)
    assert all(entry["client_submitted_at"] is None for entry in fleet)
    assert all(entry["client_launched_at"] is None for entry in fleet)
    assert all(entry["instance_id"] == "i-0fake" for entry in fleet)
    assert all(entry["worker_environment"]["capture"] == "ok" for entry in fleet)
    assert all(
        entry["worker_environment"]["cpu_model"] == entry["job_id"] for entry in fleet
    )
    assert "harvested 4 already-completed unit(s) from 2 shard(s)" in (
        capsys.readouterr().err
    )
    assert not manifest_path.exists()
    assert not (artifacts / "checkpoint.json").exists()


@pytest.mark.parametrize("with_params", [True, False], ids=["params", "plain"])
def test_resume_single_job_keeps_outstanding_draws(tmp_path, monkeypatch, with_params):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    transport = sb._run_subprocess
    failed_jobs = set()
    uploads = []
    resuming = False

    def worker(argv, timeout_s):
        payload = uploaded_request_of(argv)
        if payload is not None:
            document, job = payload
            wire = document["_explicit_points"]
            uploads.append(wire)
            assert not {"corners", "monte_carlo", "exclude"}.intersection(document)
            points = sim._corner_points_from_wire(wire)
            if not resuming and any(p.sample_index == 29 for p in points):
                failed_jobs.add(job)
            report = json.loads(_sim_report([p.corner_id for p in points]))
            for corner, point in zip(report["corners"], points, strict=True):
                corner.update(
                    params=point.params,
                    monte_carlo={
                        "sample_index": point.sample_index,
                        "seed": point.mc_seed["rndseed"],
                    },
                    runtime_s=2.0 if resuming else 1.0,
                    measurements=[
                        dict(name="vout", value=1.0, margin=None, status="pass")
                    ],
                )
            runner.outputs_by_job[job] = {"report.json": json.dumps(report)}
        elif "cp" in argv and any(
            job + "/outputs" in " ".join(argv) for job in failed_jobs
        ):
            return fake_completed(
                "", returncode=1, stderr="NoSuchKey: lost shard outputs"
            )
        return transport(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", worker)
    deck = _write_body(tmp_path)
    deck.write_text(".param rval=1000\n" + deck.read_text())
    request = _batch_request(
        tmp_path,
        corners={"params": {"rval": [1000]}} if with_params else {},
        monte_carlo={"n": 30, "seed": 42, "vary": "process"},
        measurements=[{"name": "vout", "expr": "v(out)", "reduce": "max"}],
    )
    request["batch"].update(max_units_per_job=10, max_concurrent_jobs=1)
    path = str(_write_request(tmp_path, request))
    artifacts = str(tmp_path / "artifacts")
    first = sim.run_sim(path, artifacts_dir=artifacts)
    completed = {c["corner_id"]: c for c in first["corners"] if c["status"] == "pass"}
    lost = [c for c in first["corners"] if c["corner_id"] not in completed]
    assert len(completed) == 20 and len(lost) == 10
    assert all(any(d["code"] == "lost_shard" for d in c["diagnostics"]) for c in lost)
    assert len(uploads) == 3
    original = next(
        wire for wire in uploads if any(p["sample_index"] == 29 for p in wire)
    )

    resuming = True
    uploads.clear()
    resumed = sim.run_sim(path, artifacts_dir=artifacts, resume=True)
    assert uploads == [original]
    assert resumed["status"] == "pass"
    assert resumed["environment"]["resume"]["resumed_corners"] == 20
    assert resumed["measurements"][0]["monte_carlo"]["n"] == 30
    corners = resumed["corners"]
    assert [c["corner_id"] for c in corners] == [
        c["corner_id"] for c in first["corners"]
    ]
    assert [c["monte_carlo"]["sample_index"] for c in corners] == list(range(30))
    assert {
        c["corner_id"]: c for c in corners if c["corner_id"] in completed
    } == completed
