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


# --- issue #5019: per-unit summary and --job-status parity ---


def _mixed_report(errored=7, passed=3):
    report = json.loads(_sim_report(tuple(f"u{i}" for i in range(errored + passed))))
    report["status"] = "error"
    for i, corner in enumerate(report["corners"]):
        if i < errored:
            corner["status"] = "error"
            corner["diagnostics"] = [{"code": "sim_timeout", "message": f"unit {i}"}]
    return report


def test_unit_summary_counts_and_bounds_named_units():
    reason = exit_reason_env({"exit_code": "4"}, _mixed_report())["exit_reason"]
    units = reason["units"]
    assert units["total"] == 10
    assert units["by_status"] == {"error": 7, "pass": 3}
    assert len(units["first_not_passed"]) == 5
    assert units["first_not_passed"][0] == {
        "corner_id": "u0",
        "status": "error",
        "code": "sim_timeout",
        "message": "unit 0",
    }
    assert "not the whole-job ceiling" in reason["message"]


def test_unit_summary_absent_for_empty_or_junk_corners():
    for corners in ([], "junk", None):
        reason = exit_reason_env({"exit_code": "4"}, {"corners": corners})
        assert "units" not in reason["exit_reason"]


def test_exit_124_gets_no_verdict_reason():
    assert exit_reason_env({"exit_code": "124"}, _mixed_report()) == {}


def _job_status(tmp_path, monkeypatch, state, exit_code, report_result=None):
    from klayout_tools import sim_batch_async as sba
    from test_sim_batch import _batch_request, _install_fake_batch_transport

    runner = _FakeRunner()
    runner.queue("status.json", fake_completed(_status(state, exit_code=exit_code)))
    if report_result is not None:
        runner.queue("outputs/report.json", report_result)
    _install_fake_batch_transport(monkeypatch, runner=runner)
    status = sba.read_job_status(_batch_request(tmp_path), "klt-sim-deadbeef")
    return status, runner


def test_job_status_failed_exit_4_carries_exit_reason(tmp_path, monkeypatch):
    status, _ = _job_status(
        tmp_path,
        monkeypatch,
        "failed",
        "4",
        fake_completed(json.dumps(_mixed_report())),
    )
    reason = status["exit_reason"]
    assert reason["exit_code"] == 4
    assert reason["units"]["by_status"] == {"error": 7, "pass": 3}


def test_job_status_unreadable_report_is_not_an_error(tmp_path, monkeypatch):
    for bad in (fake_completed("not json"), fake_completed("", returncode=1)):
        status, _ = _job_status(tmp_path, monkeypatch, "failed", "4", bad)
        assert status["state"] == "failed"
        assert "exit_reason" not in status


def test_job_status_skips_report_read_unless_failed_verdict_exit(tmp_path, monkeypatch):
    for state, code in (
        ("running", ""),
        ("done", "0"),
        ("failed", "1"),
        ("timeout", "124"),
    ):
        status, runner = _job_status(tmp_path, monkeypatch, state, code)
        assert "exit_reason" not in status
        assert not any("report.json" in " ".join(c) for c in runner.calls)
