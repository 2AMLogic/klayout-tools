"""The harvest's silent skips (issue #7115): a killed client leaves a shard
map whose `launched`/`collected_at: null` entries can point at jobs the
fleet already finished, and a resume that skips them without a word re-runs
those units. The tests here record which skip path each reproduction takes
-- a transiently unreadable status read, a permanently unreadable one, an
unrecoverable grouping -- and pin the notice contract: every skip of a
launched, not-already-known entry says why on stderr, and a transiently
unreadable status is re-read before it is trusted as `unknown`. Split out
of test_sim_batch_resume_harvest.py to keep both under the per-file size
ratchet."""

from __future__ import annotations

import json

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_resume as sbr
from test_sim_batch import _corner_points
from test_sim_batch_resume_harvest import _harvest, _harvest_fixture, _report_ids
from test_sim_batch_shard_attach import _states

pytestmark = pytest.mark.usefixtures("bare_name_ngspice_resolution")


def _failing_status(monkeypatch, job_ids, *, failures):
    """Make each ``job_ids``'s ``status.json`` read fail (non-zero exit, so
    ``_read_status`` sees an unreadable read) until it has failed
    ``failures`` times, then pass through; every other call goes to the fake
    fleet. Returns the per-job failure count."""
    inner = sb._run_subprocess
    counts = {job_id: 0 for job_id in job_ids}

    def _wrapped(argv, timeout_s):
        joined = " ".join(argv)
        if joined.endswith("status.json -"):
            for job_id in job_ids:
                if job_id in joined and counts[job_id] < failures:
                    counts[job_id] += 1
                    return fake_completed("", returncode=1)
        return inner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _wrapped)
    return counts


def test_transiently_unreadable_status_is_reread_and_harvested(
    tmp_path, monkeypatch, capsys
):
    """Case (a), the reporter's shape: map entries ``launched`` with
    ``collected_at: null`` for remotely-``done`` jobs whose one status read
    fails transiently. Without the re-read that failure read as ``unknown``
    and the shard's units were re-dispatched -- the re-billed resume of the
    report -- so the harvest retries an unreadable read instead of trusting
    the first failure."""
    runner, request, payload = _harvest_fixture(tmp_path, monkeypatch)
    jobs = [s["job_id"] for s in payload["shards"]]
    before = json.loads(open(payload["manifest_path"]).read())
    assert all(
        shard.get("state") == "launched" and not shard.get("collected_at")
        for shard in before["shards"]
    )
    monkeypatch.setattr(sbr, "_STATUS_READ_BACKOFF_S", 0.0)
    failures = _failing_status(monkeypatch, [jobs[0]], failures=1)
    launches = len(runner.argvs_matching("launch"))

    corners, _, _ = _harvest(tmp_path, request)

    assert failures[jobs[0]] == 1  # exactly the one transient failure
    assert _report_ids(corners) == {f"tt_1.800_{i}" for i in range(10)}
    assert len(runner.argvs_matching("launch")) == launches  # never launches
    after = json.loads(open(payload["manifest_path"]).read())
    assert all(shard["state"] == "collected" for shard in after["shards"])
    assert "did not harvest" not in capsys.readouterr().err


def test_permanently_unreadable_status_skips_with_a_notice(
    tmp_path, monkeypatch, capsys
):
    """Case (b), also the reporter's outcome: a status read that never
    succeeds skips the shard (its units stay outstanding for the dispatch),
    but no longer silently -- one notice names the shard, its job and
    ``status unreadable`` -- and the shards that do read are still
    harvested."""
    runner, request, payload = _harvest_fixture(tmp_path, monkeypatch)
    jobs = [s["job_id"] for s in payload["shards"]]
    monkeypatch.setattr(sbr, "_STATUS_READ_BACKOFF_S", 0.0)
    failures = _failing_status(monkeypatch, [jobs[0]], failures=99)
    launches = len(runner.argvs_matching("launch"))

    corners, _, _ = _harvest(tmp_path, request)

    assert failures[jobs[0]] == sbr._STATUS_READ_ATTEMPTS  # bounded retries
    assert _report_ids(corners) == {f"tt_1.800_{i}" for i in range(4, 10)}
    assert len(runner.argvs_matching("launch")) == launches
    assert (
        f"klt sim: notice: --resume did not harvest shard 0 (job {jobs[0]}): "
        "status unreadable" in capsys.readouterr().err
    )
    after = json.loads(open(payload["manifest_path"]).read())
    assert after["shards"][0]["state"] != "collected"
    assert after["shards"][1]["state"] == "collected"


def test_non_terminal_remote_state_skips_with_a_notice(tmp_path, monkeypatch, capsys):
    """A still-``running`` remote job stays a skip, never an error -- but an
    announced one, so it is distinguishable from an unreadable status or a
    lost grouping."""
    runner, request, payload = _harvest_fixture(tmp_path, monkeypatch)
    jobs = [s["job_id"] for s in payload["shards"]]
    _states(monkeypatch, {jobs[1]: "running"})
    launches = len(runner.argvs_matching("launch"))

    corners, _, _ = _harvest(tmp_path, request)

    assert _report_ids(corners) == {f"tt_1.800_{i}" for i in (0, 1, 2, 3, 7, 8, 9)}
    assert len(runner.argvs_matching("launch")) == launches
    assert (
        f"klt sim: notice: --resume did not harvest shard 1 (job {jobs[1]}): "
        "remote state running" in capsys.readouterr().err
    )


def test_unrecoverable_grouping_skips_with_a_notice(tmp_path, monkeypatch, capsys):
    """Case (c), the reporter's outcome on a legacy map: ``units`` absent and
    a ``units_sha256`` no partition slice reproduces loses the grouping, the
    shard's units stay outstanding for the dispatch, and the notice says so
    instead of silence."""
    runner, request, payload = _harvest_fixture(tmp_path, monkeypatch)
    jobs = [s["job_id"] for s in payload["shards"]]
    document = json.loads(open(payload["manifest_path"]).read())
    document["shards"][0].pop("units", None)
    document["shards"][0]["units_sha256"] = "0" * 64
    open(payload["manifest_path"], "w").write(json.dumps(document))
    launches = len(runner.argvs_matching("launch"))

    corners, _, _ = _harvest(tmp_path, request)

    assert _report_ids(corners) == {f"tt_1.800_{i}" for i in range(4, 10)}
    assert len(runner.argvs_matching("launch")) == launches
    assert (
        f"klt sim: notice: --resume did not harvest shard 0 (job {jobs[0]}): "
        "shard grouping not recoverable" in capsys.readouterr().err
    )
    after = json.loads(open(payload["manifest_path"]).read())
    assert after["shards"][0]["state"] != "collected"
    assert after["shards"][1]["state"] == "collected"


@pytest.mark.parametrize("recoverable", [True, False])
def test_submitted_and_known_shards_stay_silent(
    tmp_path, monkeypatch, capsys, recoverable
):
    """A ``submitted`` (never launched) shard and one whose units the
    checkpoint already holds are deliberate skips: no notice, no launch."""
    runner, request, payload = _harvest_fixture(tmp_path, monkeypatch)
    document = json.loads(open(payload["manifest_path"]).read())
    submitted = document["shards"][0]
    submitted["state"] = "submitted"
    if not recoverable:
        submitted.pop("units")
        submitted["units_sha256"] = "0" * 64
    open(payload["manifest_path"], "w").write(json.dumps(document))
    # Shard 2's units (7-9) are exactly what the checkpoint already holds.
    known = [p.corner_id for p in _corner_points(10)][7:]
    launches = len(runner.argvs_matching("launch"))

    corners, _, _ = _harvest(tmp_path, request, known=known)

    assert _report_ids(corners) == {f"tt_1.800_{i}" for i in (4, 5, 6)}
    assert len(runner.argvs_matching("launch")) == launches
    assert "did not harvest" not in capsys.readouterr().err
