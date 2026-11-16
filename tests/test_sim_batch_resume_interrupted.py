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
