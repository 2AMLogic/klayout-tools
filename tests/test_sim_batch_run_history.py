"""Whole-run observations are scoped by request and by actual job membership."""

from __future__ import annotations

import json

import pytest

from klayout_tools import sim, sim_batch_resume, sim_batch_shards
from klayout_tools import sim_batch_history as history

# The "Units that never ran" block of `sim.DIAGNOSTIC_CODES`.
NEVER_RAN = (
    "budget_exceeded",
    "orphaned",
    "lost_shard",
    "timeout_budget_unreachable",
    "batch_job_failed",
    "batch_job_timeout",
    "batch_poll_timeout",
    "batch_job_interrupted",
)


def test_submitted_jobs_and_unrelated_receipts_do_not_count(tmp_path):
    root = str(tmp_path)
    request = {"analysis": {"type": "op"}}
    state = history.begin(root, "fp", request, 8, False, False, None)
    digest = sim_batch_shards.receipt_request_sha256(request)
    stamp = "2026-01-01T08:00:00+08:00"
    pending = {"job_id": "pending", "state": "submitted", "submitted_at": stamp}
    (tmp_path / "batch-shards.json").write_text(
        json.dumps({"fingerprint": "fp", "shards": [pending]})
    )
    for job, sha in (("pending", "another-request"), ("another-run", digest)):
        (tmp_path / f"batch-receipt-{job}.json").write_text(
            json.dumps({"job_id": job, "request_sha256": sha, "launched_at": stamp})
        )
    remote = {"fleet": [None], "unlaunched_shards": 1, "resumable": True}
    missing = [{"diagnostics": [{"code": "lost_shard"}, {"code": "orphaned"}]}]
    first = history.finish(state, remote, missing, 1)
    assert history.finish(state, remote, missing, 1) == first
    assert "run" not in remote
    summary = first["run"]
    assert summary["declared_hosts"] == 8 and summary["observed_launched_jobs"] == 0
    assert summary["first_launch"] is None and summary["last_terminal"] is None
    assert summary["completed_units"] == 0 and not summary["complete"]

    # Host count is a per-dispatch override. Keep the original declaration.
    resumed = history.begin(root, "fp", request, 1, True, False, None)
    report = {
        "job_id": "pending",
        "state": "done",
        "client_launched_at": stamp,
        "client_status_terminal_at": "2026-01-01T00:00:02Z",
    }
    corners = [{"status": "fail", "diagnostics": []}]
    run = history.finish(resumed, report, corners, 1)["run"]
    assert run["declared_hosts"] == 8 and run["rounds"] == 2
    assert run["observed_launched_jobs"] == 1 and run["complete"]
    assert run["first_launch"] == "2026-01-01T00:00:00.000000+00:00"
    assert run["last_terminal"] == "2026-01-01T00:00:02.000000+00:00"
    repeat = {**report, "client_status_terminal_at": "2026-01-03T00:00:00Z"}
    assert history.finish(resumed, repeat, corners, 1)["run"] == run


def test_legacy_checkpoint_only_and_unsharded_modes(tmp_path):
    missing = history.begin(str(tmp_path), "fp", {}, 4, True, True, None)
    old = history.finish(missing, None, [{"diagnostics": []}], 1)["run"]
    assert old["history_coverage"] == "partial" and old["rounds"] is None
    assert old["observed_launched_jobs"] is None and old["first_launch"] is None
    assert old["complete"]

    assert history.begin(str(tmp_path), None, {}, 4, False, False, None) is None
    assert history.begin(str(tmp_path), "fp", {}, 4, False, False, "job-1") is None
    assert history.begin(str(tmp_path), "fp", {}, 1, False, False, None) is None
    single = {"job_id": "only"}
    policy = {"batch": {"job_ceiling_check": "split"}}
    optional = history.begin(str(tmp_path), "fp", policy, 1, False, False, None)
    assert history.finish(optional, single, [{"diagnostics": []}], 1) is single


def test_never_ran_vocabulary_matches_the_diagnostic_codes():
    assert set(NEVER_RAN) == sim_batch_resume.NEVER_RAN_CODES
    assert sim_batch_resume.NEVER_RAN_CODES <= sim.DIAGNOSTIC_CODES


@pytest.mark.parametrize("code", NEVER_RAN)
def test_a_unit_that_never_ran_is_not_completed_and_keeps_history_open(tmp_path, code):
    state = history.begin(str(tmp_path), "fp", {}, 2, False, False, None)
    corners = [
        {"status": "pass", "diagnostics": []},
        {"status": "error", "diagnostics": [{"code": code}]},
    ]
    run = history.finish(state, None, corners, 2)["run"]
    assert run["completed_units"] == 1 and run["total_units"] == 2
    assert run["complete"] is False
    # `--resume` only reuses a history that is still open.
    on_disk = json.loads((tmp_path / "batch-run-history.json").read_text())
    assert on_disk["closed"] is False
    resumed = history.begin(str(tmp_path), "fp", {}, 2, True, False, None)
    assert resumed.from_start and resumed.rounds == 2 and resumed.declared_hosts == 2
