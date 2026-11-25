"""Tests for the top-level ``batch_runner_version_mismatch`` diagnostic a
refused runner major.minor mismatch lifts into the report (#5061), and for
the #7857 pinned-client remedy both mismatch messages carry when the client
is the older side. Split out of ``test_sim_batch.py`` to keep it under its
file-size ceiling.
"""

from __future__ import annotations

import json

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim
from klayout_tools import sim_batch as sb
from klayout_tools.cli import main
from klayout_tools.sim_batch_features import pop_remote_diagnostics, refusal_diagnostic
from test_sim_batch import (
    _CLIENT_VERSION,
    _batch_request,
    _config,
    _envelope,
    _errored_report_without_include_code,
    _FakeRunner,
    _identity,
    _image,
    _install_fake_batch_transport,
    _pin_client_version,
    _publish_manifest,
    _run_one_job,
    _sim_report,
    _status,
    _write_body,
    _write_request,
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
    assert 'runner_version_check to "warn" to run anyway' in diagnostic["message"]
    # A client NEWER than the runner keeps its wording: the #7857 command is
    # for the older-client direction, where a pinned older klt reads the
    # refusal as a dead end.
    assert "uvx --from" not in diagnostic["message"]
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
    corners, _, environment = _run_one_job(tmp_path, monkeypatch, runner)
    assert environment["runner_version_diagnostic"]["accepted_client_release"] == "0.7"
    # The per-unit remedy names the warn opt-out too (moved here from
    # test_sim_batch.py to stay under its file-size ceiling, #5750).
    unit = corners[0]["diagnostics"][0]
    assert unit["code"] == "batch_job_failed"
    assert 'runner_version_check to "warn" to submit anyway' in unit["message"]


def test_failed_exit_87_job_reports_and_writes_report_out(
    tmp_path, capsys, monkeypatch
):
    """Pins the Observed-1 half of #5750: a terminal ``failed`` job whose
    preflight rejected it (exit 87, version mismatch) ends as a normal
    report on stdout, a written ``--report-out`` copy, and exit 4 from
    ``_exit_code_for`` -- never rc=1 with no envelope and no report. Both
    remedy messages name the ``runner_version_check: "warn"`` opt-out."""
    _pin_client_version(monkeypatch)
    runner = _FakeRunner()
    runner.default = fake_completed(
        _status("failed", exit_code="87", detail="job command exited 87")
    )
    runner.outputs = {"runner_identity.json": _identity("0.7.0")}
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    path = _write_request(tmp_path, _batch_request(tmp_path))
    report_out = tmp_path / "report-out.json"

    code = main(
        [
            "sim",
            str(path),
            "--backend",
            "batch",
            "--format",
            "json",
            "--report-out",
            str(report_out),
        ]
    )
    out = capsys.readouterr()

    assert code == 4
    report = json.loads(out.out)
    assert report["status"] == "error"
    unit = report["corners"][0]["diagnostics"][0]
    assert unit["code"] == "batch_job_failed"
    assert "runner preflight reject" in unit["message"]
    assert 'runner_version_check to "warn" to submit anyway' in unit["message"]
    top = report["diagnostics"][0]
    assert top["code"] == sb.BATCH_RUNNER_MISMATCH_CODE
    assert 'runner_version_check to "warn" to run anyway' in top["message"]
    assert json.loads(report_out.read_text()) == report


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


def test_exit_4_batch_job_fatal_message_carries_card_and_cause(tmp_path, monkeypatch):
    """Issue #7032: the back-filled `ngspice_fatal` diagnostic carries the
    echoed card and the cause line under the `Error on line N` header, so a
    remote unit's failure reason is readable from the report without a local
    rerun."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="4"))
    remote_log = "/var/tmp/eda-job-1/outputs/artifacts/tt_1.800_27/ngspice.log"
    runner.outputs = {
        "report.json": _errored_report_without_include_code(remote_log),
        "artifacts/tt_1.800_27/ngspice.log": (
            "Circuit: * deck\n"
            "Error on line 1204 or its substitute:\n"
            "  m.x1 d g s b nfet w=1u l=0.15u\n"
            "  could not find a valid modelname\n"
            "Simulation interrupted due to error!\n"
        ),
    }
    corners, _, _ = _run_one_job(tmp_path, monkeypatch, runner)

    diagnostics = corners[0]["diagnostics"]
    assert [d["code"] for d in diagnostics] == ["measurement", "ngspice_fatal"]
    message = diagnostics[1]["message"]
    assert message.startswith("Error on line 1204")
    assert "m.x1 d g s b nfet w=1u l=0.15u" in message
    assert "could not find a valid modelname" in message
    assert diagnostics[1]["analysis_started"] is False


def test_refusal_diagnostic_for_an_older_client_carries_the_command(tmp_path):
    # Issue #7857: the runner-side refusal names the accepted minor and the
    # concrete uvx command when the client is the older side; message text
    # only, every JSON field keeps its shape.
    diag = _diag(
        {
            "runner_compatibility": "mismatch",
            "runner_klt_version": "0.9.1",
            "client_klt_version": "0.8.4",
        }
    )
    assert diag["code"] == sb.BATCH_RUNNER_MISMATCH_CODE
    assert diag["severity"] == "error"
    assert diag["runner_klt_version"] == "0.9.1"
    assert diag["client_klt_version"] == "0.8.4"
    assert diag["accepted_client_release"] == "0.9"
    assert "submit from a client of klt 0.9.x" in diag["message"]
    assert (
        "run the same request from a klt 0.9 client via "
        'uvx --from "klayout-tools==0.9.*" klt sim ...' in diag["message"]
    )


def test_prelaunch_refusal_for_an_older_client_names_the_remedy(
    tmp_path, monkeypatch, capsys
):
    # Issue #7857: a client pinned to an older minor (here 0.7.x dev against
    # a 0.9.0 image) reads the bare refusal as a dead end; the pre-launch
    # message now names the image's major.minor and the pinned-client run.
    request = {"models": {"pdk": "sky130A"}}
    _pin_client_version(monkeypatch, "0.7.1.dev3+gabc1234")
    _publish_manifest(
        tmp_path, monkeypatch, [_image(klt_version="0.9.0", klt_ref="v0.9.0")]
    )
    assessment = sb._assess_published_runner(request, _config(tmp_path))
    assert assessment["status"] == "mismatch"
    assert (
        "run the same request from a klt 0.9 client via "
        'uvx --from "klayout-tools==0.9.*" klt sim ...' in assessment["message"]
    )
    assert "klt 0.9.0" in assessment["message"]

    with pytest.raises(sim.SimError) as excinfo:
        sb._enforce_published_runner_version(assessment)
    assert excinfo.value.code == sb.BATCH_RUNNER_MISMATCH_CODE
    assert 'uvx --from "klayout-tools==0.9.*" klt sim ...' in str(excinfo.value)
    assert "no instance was launched" in str(excinfo.value)

    # Under warn the same message (remedy included) goes to stderr and the
    # request proceeds.
    assessment["check"] = "warn"
    sb._enforce_published_runner_version(assessment)
    err = capsys.readouterr().err
    assert 'uvx --from "klayout-tools==0.9.*" klt sim ...' in err
    assert "submitting anyway" in err


def test_prelaunch_remedy_names_the_oldest_minor_of_a_mixed_fleet(
    tmp_path, monkeypatch
):
    # A historical multi-image pointer with differing minors: a client of the
    # oldest published minor turns the any-image-match gate into a match, so
    # that is the minor the remedy pins.
    request = {"models": {"pdk": "sky130A"}}
    _pin_client_version(monkeypatch, "0.5.0")
    _publish_manifest(
        tmp_path,
        monkeypatch,
        [
            _image(region="us-east-1", klt_version="0.6.2"),
            _image(region="us-west-2", klt_version="0.7.0"),
        ],
    )
    assessment = sb._assess_published_runner(request, _config(tmp_path, region=None))
    assert assessment["status"] == "mismatch"
    assert 'uvx --from "klayout-tools==0.6.*" klt sim ...' in assessment["message"]
    assert 'uvx --from "klayout-tools==0.7' not in assessment["message"]


def test_prelaunch_newer_client_keeps_its_wording(tmp_path, monkeypatch):
    # The #7857 command is only for the older-client direction: a newer
    # client (image 0.5.0, client 0.7.x dev) keeps the existing message.
    request = {"models": {"pdk": "sky130A"}}
    _pin_client_version(monkeypatch, "0.7.1.dev3+gabc1234")
    _publish_manifest(tmp_path, monkeypatch, [_image(klt_version="0.5.0")])
    assessment = sb._assess_published_runner(request, _config(tmp_path))
    assert assessment["status"] == "mismatch"
    assert "uvx --from" not in assessment["message"]
    assert "older than the image" not in assessment["message"]
