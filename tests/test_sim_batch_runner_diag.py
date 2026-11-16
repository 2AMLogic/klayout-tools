"""Tests for the top-level ``batch_runner_version_mismatch`` diagnostic a
refused runner major.minor mismatch lifts into the report (#5061). Split out
of ``test_sim_batch.py`` to keep it under its file-size ceiling.
"""

from __future__ import annotations

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim_batch as sb
from klayout_tools.sim_batch_features import pop_remote_diagnostics, refusal_diagnostic
from test_sim_batch import (
    _CLIENT_VERSION,
    _envelope,
    _FakeRunner,
    _identity,
    _run_one_job,
    _sim_report,
    _status,
)


def test_refused_mismatch_carries_one_top_level_diagnostic_payload(
    tmp_path, monkeypatch
):
    # Issue #5061: a refused major.minor mismatch gets a lift-ready diagnostic
    # naming both versions and the accepted client release, with the remedy.
    runner = _FakeRunner()
    runner.default = fake_completed(
        _status("failed", exit_code="87", detail="job command exited 87")
    )
    runner.outputs = {
        "report.json": _envelope("skew", code=sb.BATCH_RUNNER_MISMATCH_CODE),
        "runner_identity.json": _identity("0.4.2"),
    }
    corners, _, environment = _run_one_job(tmp_path, monkeypatch, runner)

    diagnostic = environment["runner_version_diagnostic"]
    assert diagnostic["code"] == sb.BATCH_RUNNER_MISMATCH_CODE
    assert diagnostic["severity"] == "error"
    assert diagnostic["runner_klt_version"] == "0.4.2"
    assert diagnostic["client_klt_version"] == _CLIENT_VERSION
    assert diagnostic["accepted_client_release"] == "0.4"
    assert "submit from a client of klt 0.4.x" in diagnostic["message"]
    assert "update the runner image" in diagnostic["message"]
    # Per-corner diagnostics are unchanged.
    assert corners[0]["diagnostics"][0]["runner_code"] == sb.BATCH_RUNNER_MISMATCH_CODE


def test_unrecovered_exit_87_mismatch_also_carries_the_diagnostic(
    tmp_path, monkeypatch
):
    runner = _FakeRunner()
    runner.default = fake_completed(
        _status("failed", exit_code="87", detail="job command exited 87")
    )
    runner.outputs = {"runner_identity.json": _identity("0.7.0")}
    _, _, environment = _run_one_job(tmp_path, monkeypatch, runner)
    assert environment["runner_version_diagnostic"]["accepted_client_release"] == "0.7"


def _diag(identity):
    return refusal_diagnostic(
        identity, sb._runner_gate_release, sb.BATCH_RUNNER_MISMATCH_CODE
    )


def test_no_runner_version_diagnostic_for_match_or_unknown_or_proceeding_run(
    tmp_path, monkeypatch
):
    assert _diag({"runner_compatibility": "match"}) is None
    assert _diag({"runner_compatibility": "unknown"}) is None
    assert (
        _diag({"runner_compatibility": "mismatch", "runner_klt_version": None}) is None
    )
    # A job that ran (e.g. runner_version_check: warn) is never refused, so
    # the post-run identity read does not emit the diagnostic.
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {
        "report.json": _sim_report(),
        "runner_identity.json": _identity("0.4.2"),
    }
    _, _, environment = _run_one_job(tmp_path, monkeypatch, runner)
    assert environment["runner_compatibility"] == "mismatch"
    assert "runner_version_diagnostic" not in environment


def test_pop_remote_diagnostics_lifts_one_entry_per_kind_from_a_fleet():
    diag = {"code": sb.BATCH_RUNNER_MISMATCH_CODE}
    remote = {
        "request_feature_diagnostic": {"code": "feature"},
        "fleet": [
            {"runner_version_diagnostic": dict(diag)},
            None,
            {"runner_version_diagnostic": dict(diag)},
        ],
    }
    assert pop_remote_diagnostics(remote) == [{"code": "feature"}, diag]
    assert all("runner_version_diagnostic" not in m for m in remote["fleet"] if m)
    assert pop_remote_diagnostics(None) == []
