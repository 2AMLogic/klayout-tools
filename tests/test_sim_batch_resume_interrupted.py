"""``klt sim --resume`` re-submits a shard whose recorded job stayed
``interrupted`` (issue #5371); split from test_sim_batch.py to keep it small."""

from __future__ import annotations

import json

from klayout_tools import sim_batch as sb
from test_sim_batch import (
    FAKE_BUCKET,
    _launches,
    _provision_script,
    _shard_resume_fixture,
)


def _stick_job_status(monkeypatch, states):
    """`_read_status` reports ``states[job_id]`` for those jobs (else real)."""
    real = sb._read_status

    def fake(config, job_id, run):
        if job_id in states:
            return {"state": states[job_id]}
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
