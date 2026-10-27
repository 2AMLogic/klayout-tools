"""Tests for `klt sim --submit-only` / `--job-status` (issue #3073,
`src/klayout_tools/sim_batch_async.py`). Every test injects the fake command
runner from `test_sim_batch.py`; nothing here reaches `aws` or S3."""

from __future__ import annotations

import json

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim
from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_async as sba
from klayout_tools.cli import main
from test_sim_batch import (
    FAKE_BUCKET,
    _batch_request,
    _FakeRunner,
    _install_fake_batch_transport,
    _sim_report,
    _status,
    _write_body,
    _write_request,
)


def _stderr_json(capsys):
    """The JSON envelope on stderr (skipping any `klt: warning:` prefix)."""
    err = capsys.readouterr().err
    return json.loads(err[err.index("{") :])


def _setup(tmp_path, monkeypatch, runner, **request_overrides):
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    return _write_request(tmp_path, _batch_request(tmp_path, **request_overrides))


def _submit(path):
    return sim.run_sim(str(path), submit_only=True)


def test_submit_only_returns_identity_without_polling(tmp_path, monkeypatch):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner)

    result = _submit(path)

    assert result["status"] == "submitted"
    assert result["job_id"].startswith("klt-sim-")
    assert result["bucket"] == FAKE_BUCKET
    assert result["region"] == "us-east-1"
    assert len(result["request_sha256"]) == 64
    assert result["launched_at"].endswith("Z")
    assert result["unit_count"] == 1
    # One launch, and neither a status poll nor an outputs collect.
    assert len(runner.argvs_matching("launch")) == 1
    assert runner.argvs_matching("status.json") == []
    assert runner.argvs_matching("outputs/") == []


def test_submit_only_refuses_non_batch_backend_before_any_s3_write(
    tmp_path, monkeypatch
):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner, backend="local")

    with pytest.raises(sim.SimError) as err:
        _submit(path)

    assert err.value.code == sba.SUBMIT_ONLY_UNSUPPORTED_CODE
    assert runner.calls == []


def test_submit_only_refuses_sharded_request_before_any_s3_write(tmp_path, monkeypatch):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner)

    with pytest.raises(sim.SimError) as err:
        sim.run_sim(str(path), submit_only=True, hosts=2)

    assert err.value.code == sba.SUBMIT_ONLY_UNSUPPORTED_CODE
    assert "hosts" in str(err.value)
    assert runner.calls == []


def test_submit_only_with_resume_is_refused_before_any_s3_write(tmp_path, monkeypatch):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner)

    with pytest.raises(sim.SimError):
        sim.run_sim(str(path), submit_only=True, resume=True)

    assert runner.calls == []


@pytest.mark.parametrize("state", ["running", "interrupted", "done", "failed"])
def test_job_status_reports_state_fields(tmp_path, monkeypatch, state):
    runner = _FakeRunner()
    runner.default = fake_completed(_status(state, detail="executing"))
    _install_fake_batch_transport(monkeypatch, runner=runner)

    status = sba.read_job_status(_batch_request(tmp_path), "klt-sim-deadbeef")

    assert status["state"] == state
    assert status["terminal"] is (state in sb.BATCH_TERMINAL_STATES)
    assert status["detail"] == "executing"
    assert status["elapsed_seconds"] == 300
    assert status["updated_at"] == "2026-09-18T00:05:00Z"
    assert status["instance"]["instance_type"] == "c7i.4xlarge"
    # Read-only: exactly one S3 read, nothing launched or submitted.
    assert len(runner.calls) == 1
    assert runner.argvs_matching("launch") == []


def test_job_status_pending_when_no_status_json_yet(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.queue("status.json", fake_completed("", returncode=1))
    _install_fake_batch_transport(monkeypatch, runner=runner)

    status = sba.read_job_status(_batch_request(tmp_path), "klt-sim-deadbeef")

    assert status["state"] == "pending"
    assert status["terminal"] is False
    assert runner.argvs_matching("head-object")  # job.json confirmed to exist


def test_job_status_unknown_job_is_an_error_with_code(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.queue("status.json", fake_completed("", returncode=1))
    runner.queue(
        "head-object",
        fake_completed("", returncode=254, stderr="An error occurred (404): Not Found"),
    )
    _install_fake_batch_transport(monkeypatch, runner=runner)

    with pytest.raises(sim.SimError) as err:
        sba.read_job_status(_batch_request(tmp_path), "klt-sim-nope")

    assert err.value.code == sba.BATCH_JOB_NOT_FOUND_CODE


def test_job_status_transport_failure_is_not_reported_as_unknown_job(
    tmp_path, monkeypatch
):
    runner = _FakeRunner()
    runner.queue("status.json", fake_completed("", returncode=1))
    runner.queue(
        "head-object", fake_completed("", returncode=255, stderr="AccessDenied (403)")
    )
    _install_fake_batch_transport(monkeypatch, runner=runner)

    with pytest.raises(sim.SimError) as err:
        sba.read_job_status(_batch_request(tmp_path), "klt-sim-deadbeef")

    assert err.value.code != sba.BATCH_JOB_NOT_FOUND_CODE


def test_job_status_rejects_malformed_job_id(tmp_path, monkeypatch):
    runner = _FakeRunner()
    _install_fake_batch_transport(monkeypatch, runner=runner)

    with pytest.raises(sim.SimError) as err:
        sba.read_job_status(_batch_request(tmp_path), "../escape")

    assert err.value.code == sba.BATCH_JOB_ID_INVALID_CODE
    assert runner.calls == []


def test_submit_then_status_round_trip_through_the_cli(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner)

    assert main(["sim", str(path), "--submit-only", "--format", "json"]) == 0
    submitted = json.loads(capsys.readouterr().out)
    job_id = submitted["job_id"]

    runner.default = fake_completed(_status("interrupted", job_id=job_id))
    launches_before = len(runner.argvs_matching("launch"))
    assert main(["sim", str(path), "--job-status", job_id, "--format", "json"]) == 0
    status = json.loads(capsys.readouterr().out)

    assert status["job_id"] == job_id
    assert status["state"] == "interrupted"
    # Reading an interrupted job re-launches nothing.
    assert len(runner.argvs_matching("launch")) == launches_before


def test_submit_only_writes_the_submit_receipt(tmp_path, monkeypatch):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner)

    result = _submit(path)

    receipt = json.loads((tmp_path / ".klt" / "sim" / "batch-receipt.json").read_text())
    assert receipt["job_id"] == result["job_id"]
    assert "bucket" not in receipt
    raw = json.dumps(receipt)
    assert FAKE_BUCKET not in raw
    assert "s3://" not in raw
    assert "arn:" not in raw
    assert receipt["request_sha256"] == result["request_sha256"]
    # state stays null: the client does not know the job's terminal state.
    assert receipt["state"] is None


def test_submit_status_attach_round_trip(tmp_path, monkeypatch):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner)

    job_id = _submit(path)["job_id"]
    launches = len(runner.argvs_matching("launch"))
    uploads = [a for a in runner.argvs_matching("s3", "cp") if "inputs/" in " ".join(a)]
    assert uploads

    # An interrupted job is reported, and nothing re-launches or re-uploads.
    runner.default = fake_completed(_status("interrupted", job_id=job_id))
    status = sba.read_job_status(_batch_request(tmp_path), job_id)
    assert status["state"] == "interrupted"
    assert len(runner.argvs_matching("launch")) == launches

    # Once the fleet finishes, status says done and --attach collects.
    runner.default = fake_completed(_status("done", job_id=job_id))
    assert sba.read_job_status(_batch_request(tmp_path), job_id)["state"] == "done"
    runner.outputs = {"report.json": _sim_report()}
    report = sim.run_sim(str(path), attach_job_id=job_id)

    assert report["environment"]["remote"]["job_id"] == job_id
    assert len(runner.argvs_matching("launch")) == launches
    assert [
        a for a in runner.argvs_matching("s3", "cp") if "inputs/" in " ".join(a)
    ] == uploads


def test_cli_job_status_unknown_job_exits_1_with_code(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner()
    runner.queue("status.json", fake_completed("", returncode=1))
    runner.queue("head-object", fake_completed("", returncode=254, stderr="Not Found"))
    path = _setup(tmp_path, monkeypatch, runner)

    code = main(["sim", str(path), "--job-status", "klt-sim-nope", "--format", "json"])

    assert code == 1
    error = _stderr_json(capsys)["error"]
    assert error["code"] == sba.BATCH_JOB_NOT_FOUND_CODE


def test_cli_submit_only_non_batch_backend_envelope(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner, backend="local")

    code = main(["sim", str(path), "--submit-only", "--format", "json"])

    assert code == 1
    error = _stderr_json(capsys)["error"]
    assert error["code"] == sba.SUBMIT_ONLY_UNSUPPORTED_CODE
    assert runner.calls == []


# --------------------------------------------------------------------------- #
# `--wait-for-capacity` (issue #3564)
# --------------------------------------------------------------------------- #


class _Stop(Exception):
    """Raised by the config spy so a blocking run stops once it has read the
    effective config -- before any S3 write."""


def _spy_capacity_wait(monkeypatch):
    """Record every ``capacity_wait_s`` a batch config resolves to."""
    seen: list[float] = []

    def wrap(real):
        def spy(*args, **kwargs):
            config = real(*args, **kwargs)
            seen.append(config.capacity_wait_s)
            return config

        return spy

    monkeypatch.setattr(sba, "_resolve_batch_config", wrap(sba._resolve_batch_config))
    real_blocking = sb._resolve_batch_config

    def blocking_spy(*args, **kwargs):
        config = real_blocking(*args, **kwargs)
        seen.append(config.capacity_wait_s)
        raise _Stop

    monkeypatch.setattr(sb, "_resolve_batch_config", blocking_spy)
    return seen


def test_wait_for_capacity_reaches_the_submit_only_config(tmp_path, monkeypatch):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner)
    seen = _spy_capacity_wait(monkeypatch)

    sim.run_sim(str(path), submit_only=True, capacity_wait_s=90)

    assert seen == [90.0]


def test_wait_for_capacity_reaches_the_blocking_config(tmp_path, monkeypatch):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner)
    seen = _spy_capacity_wait(monkeypatch)

    with pytest.raises(_Stop):
        sim.run_sim(str(path), capacity_wait_s=45.5)

    assert seen == [45.5]


def test_wait_for_capacity_overrides_the_request_field_including_zero(
    tmp_path, monkeypatch
):
    runner = _FakeRunner()
    request = _batch_request(tmp_path)
    request["batch"]["capacity_wait_s"] = 600
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    path = _write_request(tmp_path, request)
    seen = _spy_capacity_wait(monkeypatch)

    sim.run_sim(str(path), submit_only=True)
    sim.run_sim(str(path), submit_only=True, capacity_wait_s=0)
    sim.run_sim(str(path), submit_only=True, capacity_wait_s=30)

    assert seen == [600.0, 0.0, 30.0]


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_wait_for_capacity_invalid_value_is_refused_before_any_s3_write(
    tmp_path, monkeypatch, value
):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner)

    with pytest.raises(sim.SimError, match="capacity_wait_s"):
        sim.run_sim(str(path), submit_only=True, capacity_wait_s=value)

    assert runner.calls == []


def test_wait_for_capacity_refused_on_a_non_batch_backend(tmp_path, monkeypatch):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner, backend="local")

    with pytest.raises(sim.SimError, match="--wait-for-capacity requires"):
        sim.run_sim(str(path), capacity_wait_s=10)

    assert runner.calls == []


def test_wait_for_capacity_refused_with_attach(tmp_path, monkeypatch):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner)

    with pytest.raises(sim.SimError, match="--attach"):
        sim.run_sim(str(path), attach_job_id="klt-sim-x", capacity_wait_s=10)

    assert runner.calls == []


def test_cli_wait_for_capacity_with_job_status_is_a_usage_error(
    tmp_path, monkeypatch, capsys
):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner)

    code = main(
        [
            "sim",
            str(path),
            "--job-status",
            "klt-sim-x",
            "--wait-for-capacity",
            "5",
            "--format",
            "json",
        ]
    )

    assert code == 2
    assert "--wait-for-capacity" in _stderr_json(capsys)["error"]["message"]
    assert runner.calls == []


def test_cli_wait_for_capacity_with_batch_set_is_a_usage_error(
    tmp_path, monkeypatch, capsys
):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner)

    code = main(
        [
            "sim",
            "--batch-set",
            str(path),
            "--wait-for-capacity",
            "0",
            "--format",
            "json",
        ]
    )

    assert code == 2
    assert "--wait-for-capacity" in _stderr_json(capsys)["error"]["message"]
    assert runner.calls == []


def test_cli_wait_for_capacity_flows_to_submit_only(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner)
    seen = _spy_capacity_wait(monkeypatch)

    code = main(
        [
            "sim",
            str(path),
            "--submit-only",
            "--wait-for-capacity",
            "12",
            "--format",
            "json",
        ]
    )

    assert code == 0
    assert seen == [12.0]
