"""`--resume`'s harvest of a retained batch shard map (issue #5775): terminal
shards are collected through the plain attach machinery, unlaunched/known
ones are skipped, stale maps are refused, and the map itself survives until
the sweep that owns it finishes (issue #3896). Split out of
test_sim_batch_shard_attach.py / test_sim_batch.py to keep both small."""

from __future__ import annotations

import json

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim
from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_resume as sbr
from test_sim_batch import (
    _corner_points,
    _install_fake_batch,
    _measurements_spec,
    _resume_request,
    _run,
    _status,
    _write_body,
    job_id_of,
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
