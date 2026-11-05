"""Tests for ``environment.remote.exit_reason`` (issue #4964). Split out of
``test_sim_batch.py`` to keep that file under its size ceiling."""

from __future__ import annotations

import json

from klayout_tools.sim_exit_reason import exit_reason_env
from test_sim_batch import (
    _FakeRunner,
    _run_failed_job,
    _sim_report,
    _status,
    fake_completed,
)


def _run(tmp_path, monkeypatch, exit_code, report=None):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code=exit_code))
    if report is not None:
        runner.outputs = {"report.json": json.dumps(report)}
    return _run_failed_job(tmp_path, monkeypatch, runner, points=1)


def test_exit_4_with_collected_report_names_the_reason(tmp_path, monkeypatch):
    """An op-only job (nothing checked) ends failed/exit 4 with per-corner
    pass; the environment names why."""
    report = json.loads(_sim_report())
    report["status"] = "not_checked"
    report["coverage"] = {
        "measurements_declared": 0,
        "nothing_checked": True,
        "nothing_checked_reasons": [],
    }
    corners, _, environment = _run(tmp_path, monkeypatch, "4", report)
    assert corners[0]["status"] == "pass"
    reason = environment["exit_reason"]
    assert reason["exit_code"] == 4
    assert reason["report_status"] == "not_checked"
    assert "no measurements declared" in reason["message"]


def test_exit_3_with_collected_report_names_the_reason(tmp_path, monkeypatch):
    report = json.loads(_sim_report())
    report["status"] = "fail"
    _, _, environment = _run(tmp_path, monkeypatch, "3", report)
    reason = environment["exit_reason"]
    assert reason["exit_code"] == 3
    assert "exited 3" in reason["message"]


def test_other_exit_codes_have_no_exit_reason():
    assert exit_reason_env({"exit_code": "0"}, {}) == {}
    assert exit_reason_env({"exit_code": ""}, {}) == {}
    assert exit_reason_env({}, {"coverage": "junk"}) == {}
    assert (
        exit_reason_env({"exit_code": "4"}, {"coverage": "junk"})["exit_reason"][
            "exit_code"
        ]
        == 4
    )
