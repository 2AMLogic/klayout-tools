"""`--resume`'s harvest of a retained batch shard map (issue #5775): terminal
shards are collected through the plain attach machinery, unlaunched/known
ones are skipped, stale maps are refused, and the map itself survives until
the sweep that owns it finishes (issue #3896). Split out of
test_sim_batch_shard_attach.py / test_sim_batch.py to keep both small; the
same harvest end to end through `run_sim` lives in
test_sim_batch_resume_harvest_run.py."""

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
    _install_fake_batch,
    _measurements_spec,
    _resume_request,
    _run,
    _status,
    _write_body,
    _write_request,
    job_id_of,
)
from test_sim_batch_async import _submit_shards
from test_sim_batch_shard_attach import _split_runner, _states

pytestmark = pytest.mark.usefixtures("bare_name_ngspice_resolution")

# --- `--resume`'s harvest of a retained shard map (issue #5775) ---------------


def _harvest(tmp_path, request, *, fingerprint="fp-async", known=()):
    return sbr.harvest_resume_shards(
        artifacts_dir=str(tmp_path / "artifacts"),
        config=sb.resolve_batch_config(request, corner_count=10, timeout_s=120.0),
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

    corners, engine_version, environment = _harvest(tmp_path, request)

    # The 10-unit/cap-4 split is 3 shards (4/4/2 units); only shard 0 is
    # terminal-done, so only its 4 units are harvested.
    assert _report_ids(corners) == {f"tt_1.800_{i}" for i in range(4)}
    assert all(c["status"] == "pass" for c in corners)
    assert engine_version == "46"
    assert len(environment["fleet"]) == 1
    assert environment["fleet"][0]["job_id"] == jobs[0]
    assert environment["fleet"][0]["resumed"] is True
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

    corners, _, _ = _harvest(tmp_path, request, known=known)

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

    corners, _, _ = _harvest(tmp_path, request)

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

    corners, _, _ = _harvest(tmp_path, request)

    assert len(_report_ids(corners)) == 10
    after = json.loads(open(payload["manifest_path"]).read())
    assert all(s["state"] == "collected" for s in after["shards"])


def test_resume_harvest_without_a_map_is_a_noop(tmp_path, monkeypatch):
    runner, request, payload = _harvest_fixture(tmp_path, monkeypatch)
    (tmp_path / "artifacts" / sb.BATCH_SHARDS_FILENAME).unlink()
    calls = len(runner.calls)

    assert _harvest(tmp_path, request) == ([], None, None)
    assert len(runner.calls) == calls


@pytest.mark.parametrize("empty_environment", [None, {}])
@pytest.mark.parametrize("all_empty", [False, True])
def test_resume_harvest_requires_a_remote_block_before_reusing_measurements(
    tmp_path, monkeypatch, empty_environment, all_empty
):
    _, request, payload = _harvest_fixture(tmp_path, monkeypatch)
    first_job = payload["shards"][0]["job_id"]
    real_collect = sb._run_batch_job

    def collect(**kwargs):
        corners, version, environment = real_collect(**kwargs)
        if all_empty or kwargs["job_id"] == first_job:
            environment = empty_environment
        return corners, version, environment

    monkeypatch.setattr(sb, "_run_batch_job", collect)
    corners, version, environment = _harvest(tmp_path, request)

    if empty_environment is None:
        if all_empty:
            assert (corners, version, environment) == ([], None, None)
        else:
            assert len(corners) == 6
            assert [entry["job_id"] for entry in environment["fleet"]] == [
                entry["job_id"] for entry in payload["shards"][1:]
            ]
        return
    assert len(corners) == 10
    assert version == "46"
    fleet = environment["fleet"]
    assert len(fleet) == 3
    expected = {"resumed": True}
    assert fleet[0] == expected
    if all_empty:
        assert fleet == [expected] * 3
    else:
        assert [entry["job_id"] for entry in fleet[1:]] == [
            entry["job_id"] for entry in payload["shards"][1:]
        ]


@pytest.mark.parametrize("sharded", [False, True])
def test_resume_environment_merges_jobs_without_changing_inputs(sharded):
    from copy import deepcopy

    collected = {"job_id": "collected", "instance_id": "i-collected", "resumed": True}
    prior = {"job_id": "reobserved", "state": "running"}
    current = {"job_id": "reobserved", "state": "done"}
    harvested = {"fleet": [collected, prior, None, {}]}
    dispatched = (
        {"fleet": [current, None], "interrupted": True, "max_concurrent_jobs": 1}
        if sharded
        else current
    )
    originals = deepcopy((harvested, dispatched))

    merged = sbr.merge_resume_environment(harvested, dispatched)

    expected_fleet = [collected, None, {}, current]
    expected = (
        {
            "fleet": expected_fleet + [None],
            "interrupted": True,
            "max_concurrent_jobs": 1,
        }
        if sharded
        else {"fleet": expected_fleet}
    )
    assert merged == expected
    assert (harvested, dispatched) == originals


def test_resume_environment_uses_latest_job_observation_for_shared_limit():
    recovered = {"job_id": "recovered", "max_launch_attempts": 2}
    prior = {"job_id": "reobserved"}
    current = {"job_id": "reobserved", "max_launch_attempts": 2}

    merged = sbr.merge_resume_environment({"fleet": [recovered, prior]}, current)

    assert merged == {"fleet": [recovered, current], "max_launch_attempts": 2}


@pytest.mark.parametrize("environment", [None, {"job_id": "one"}, {"fleet": [None]}])
def test_resume_environment_without_harvest_leaves_dispatch_unchanged(environment):
    assert sbr.merge_resume_environment(None, environment) is environment


def test_resume_environment_without_dispatch_keeps_harvest():
    environment = {"fleet": [{"job_id": "one", "resumed": True}]}
    assert sbr.merge_resume_environment(environment, None) is environment


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
