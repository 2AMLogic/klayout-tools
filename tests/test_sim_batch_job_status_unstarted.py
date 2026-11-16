"""``klt sim --job-status``: never-started diagnostic (#5332)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim_batch_async as sba
from test_sim_batch import (
    _batch_request,
    _FakeRunner,
    _install_fake_batch_transport,
)


def _pending_with_job_age(tmp_path, monkeypatch, written, now):
    runner = _FakeRunner()
    runner.queue("status.json", fake_completed("", returncode=1))
    runner.queue("head-object", fake_completed(json.dumps({"LastModified": written})))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    return sba.read_job_status(_batch_request(tmp_path), "klt-sim-deadbeef", now=now)


def test_job_status_reports_never_started_past_provision_slack(tmp_path, monkeypatch):
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    status = _pending_with_job_age(
        tmp_path, monkeypatch, "2026-11-11T10:00:00+00:00", now
    )

    assert status["state"] == "pending"
    assert status["never_started"] is True
    assert status["unstarted_seconds"] == 7200
    assert "never started" in status["diagnostic"]


def test_job_status_pending_within_provision_slack_has_no_diagnostic(
    tmp_path, monkeypatch
):
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    status = _pending_with_job_age(
        tmp_path, monkeypatch, "2026-11-11T11:50:00+00:00", now
    )

    assert status["state"] == "pending"
    assert "never_started" not in status
    assert "diagnostic" not in status
