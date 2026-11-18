"""`--resume`'s harvest of a retained batch shard map (issue #5775): terminal
shards are collected through the plain attach machinery, unlaunched/known
ones are skipped, stale maps are refused, and the map itself survives until
the sweep that owns it finishes (issue #3896). Split out of
test_sim_batch_shard_attach.py / test_sim_batch.py to keep both small."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim
from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_resume as sbr
from test_sim_batch import (
    _batch_request,
    _corner_points,
    _FakeRunner,
    _install_fake_batch,
    _install_fake_batch_transport,
    _launches,
    _measurements_spec,
    _resume_request,
    _run,
    _sim_report,
    _status,
    _write_body,
    _write_request,
    job_id_of,
    uploaded_request_of,
)
from test_sim_batch_async import _submit_shards
from test_sim_batch_shard_attach import _split_runner, _states

# --- `--resume`'s harvest of a retained shard map (issue #5775) ---------------


def _harvest(tmp_path, request, *, fingerprint="fp-async", known=()):
    return sbr.harvest_resume_shards(
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


def test_run_sim_zero_unit_batch_request_dispatches_nothing(
    tmp_path, monkeypatch, bare_name_ngspice_resolution
):
    """An empty corner grid never stages inputs or mints a billed job (#6540)."""
    _write_body(tmp_path)
    request = _batch_request(tmp_path, corners={"supply_v": {"vdd": []}})
    path = _write_request(tmp_path, request)

    def unexpected_subprocess(argv, timeout_s):
        raise AssertionError(f"zero-unit batch request invoked a subprocess: {argv}")

    monkeypatch.setattr(sb, "_run_subprocess", unexpected_subprocess)

    plan = _run(path, tmp_path, dry_run=True)
    assert plan["unit_count"] == 0
    assert plan["units"] == []

    report = _run(path, tmp_path)
    assert report["status"] == "not_checked"
    assert report["corners"] == []
    assert "remote" not in report["environment"]


@pytest.mark.parametrize("state", ["pending", *sb._ShardManifest._STATE_FIELDS])
@pytest.mark.parametrize("collected_at", [None, "2026-11-17T00:00:00+00:00"])
def test_mark_map_collected_preserves_completed_entries_and_timestamps(
    tmp_path, state, collected_at
):
    path = tmp_path / sb.BATCH_SHARDS_FILENAME
    entry = {"index": 0, "state": state, "collected_at": collected_at}
    untouched = {"index": 1, "state": "launched", "collected_at": None}
    document = {"shards": [dict(entry), dict(untouched)]}

    sbr._mark_map_collected(str(path), document, [0])

    after = json.loads(path.read_text())["shards"]
    assert after[1] == untouched
    if state == "collected":
        assert after[0] == entry
    else:
        assert after[0]["state"] == "collected"
        if collected_at:
            assert after[0]["collected_at"] == collected_at
        else:
            stamped = datetime.fromisoformat(after[0]["collected_at"])
            assert stamped.tzinfo == timezone.utc


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


@pytest.mark.parametrize("lose_a_corner", [False, True])
def test_shard_map_is_removed_only_when_the_sweep_finishes(
    tmp_path, monkeypatch, lose_a_corner
):
    path = _resume_request(tmp_path)
    manifest_path = tmp_path / "artifacts" / sb.BATCH_SHARDS_FILENAME
    manifest_path.parent.mkdir(parents=True)
    # A well-formed inert map (no shards listed): issue #5775's harvest
    # reads it, finds nothing to collect, and leaves it alone.
    manifest_path.write_text(
        json.dumps({"schema_version": 1, "fingerprint": "fp", "hosts": 2, "shards": []})
    )
    probe = _install_fake_batch(monkeypatch, fleet=True)
    first = _run(path, tmp_path, hosts=2)
    assert probe
    if lose_a_corner:
        manifest_path.write_text(
            json.dumps(
                {"schema_version": 1, "fingerprint": "fp", "hosts": 2, "shards": []}
            )
        )
        lost = first["corners"][1]["corner_id"]
        _install_fake_batch(monkeypatch, lost_ids={lost}, fleet=True)
        _run(path, tmp_path, hosts=2)
    assert manifest_path.exists() is lose_a_corner


# --- the same harvest, end to end through run_sim (issue #5775) ---------------


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
    assert "harvested 4 already-completed unit(s) from 2 shard(s)" in (
        capsys.readouterr().err
    )
    assert not manifest_path.exists()
    assert not (artifacts / "checkpoint.json").exists()
