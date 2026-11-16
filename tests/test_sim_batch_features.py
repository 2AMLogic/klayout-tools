"""Issue #4863: batch preflight-reject diagnostics name the missing request
feature(s) and both klt versions. Split from `test_sim_batch.py` (size
ratchet, #2766); reuses its fake runner."""

from __future__ import annotations

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim_batch as sb
from test_sim_batch import (
    _CLIENT_VERSION,
    _envelope,
    _FakeRunner,
    _identity,
    _run_one_job,
    _status,
)

_STRESS_REQUEST = {
    "netlist": "body.spice",
    "measurements": [{"name": "s", "stress": {"a": "x", "b": "y"}}],
}


def test_feature_gap_names_missing_features_on_recovered_envelope(
    tmp_path, monkeypatch
):
    # a stress request against a 0.8.0 runner names `stress`.
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="87", detail="x"))
    runner.outputs = {
        "report.json": _envelope("predates stress", code=sb.BATCH_RUNNER_FEATURE_CODE),
        "runner_identity.json": _identity("0.8.0"),
    }
    corners, _, _ = _run_one_job(tmp_path, monkeypatch, runner, request=_STRESS_REQUEST)
    diagnostic = corners[0]["diagnostics"][0]
    assert diagnostic["missing_features"] == ["stress"]
    assert diagnostic["runner_klt_version"] == "0.8.0"
    assert diagnostic["client_klt_version"] == _CLIENT_VERSION


def test_feature_gap_names_missing_features_on_unrecovered_exit_87(
    tmp_path, monkeypatch
):
    runner = _FakeRunner()
    runner.default = fake_completed(
        _status("failed", exit_code="87", detail="job command exited 87")
    )
    runner.outputs = {"runner_identity.json": _identity("0.8.0")}
    corners, _, _ = _run_one_job(tmp_path, monkeypatch, runner, request=_STRESS_REQUEST)
    diagnostic = corners[0]["diagnostics"][0]
    assert diagnostic["missing_features"] == ["stress"]
    assert diagnostic["runner_klt_version"] == "0.8.0"
    assert diagnostic["client_klt_version"] == _CLIENT_VERSION
    assert "stress" in diagnostic["message"]
    assert "0.8.0" in diagnostic["message"]


def test_no_missing_features_adds_no_field(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="87", detail="x"))
    runner.outputs = {
        "report.json": _envelope("predates", code=sb.BATCH_RUNNER_FEATURE_CODE),
        "runner_identity.json": _identity("0.7.0"),
    }
    corners, _, _ = _run_one_job(tmp_path, monkeypatch, runner)
    assert "missing_features" not in corners[0]["diagnostics"][0]
