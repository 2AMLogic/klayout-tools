"""Offline tests for ``klt sim --cancel`` and the ``cancelled`` terminal
state (issue #4351). The fleet provision script is faked through the shared
``_FakeRunner`` (as every other batch transport test here): nothing touches
AWS, and the script's exact stdout/stderr shapes are the ones verified
against 2am's ``cmd_cancel`` on 2am ``origin/main`` (PR 2AMLogic/2am#6508).
"""

from __future__ import annotations

import json

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_async as sba
from klayout_tools import sim_batch_cancel as sbc
from klayout_tools.cli import main
from test_sim_batch import (
    FAKE_BUCKET,
    _batch_request,
    _Clock,
    _FakeRunner,
    _install_fake_batch_transport,
    _provision_script,
    _status,
)
from test_sim_batch_async import _setup, _stderr_json
from test_sim_job_status_requestless import _requestless_batch_env

JOB = "klt-sim-deadbeef"

_CANCELLED_STDOUT = (
    f"== cancel job {JOB} (us-east-1) ==\n"
    f"      terminating i-0fake\n"
    f"cancelled job {JOB}: 1 instance(s) terminated; "
    f"s3://{FAKE_BUCKET}/jobs/{JOB}/ left in place\n"
)


@pytest.fixture(autouse=True)
def fleet_job_store(tmp_path):
    (tmp_path / "batch-fleet.env").write_text(
        f"BATCH_JOB_BUCKET={FAKE_BUCKET}\nBATCH_JOBS_PREFIX=jobs\n"
    )


def _cancel_env(tmp_path, monkeypatch, runner):
    """Env-resolution for a request-less ``--cancel``, transport faked."""
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _requestless_batch_env(tmp_path, monkeypatch)


def test_cancel_request_form_argv_and_envelope(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner()
    runner.default = fake_completed(_CANCELLED_STDOUT)
    path = _setup(tmp_path, monkeypatch, runner)

    assert main(["sim", str(path), "--cancel", JOB, "--format", "json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["job_id"] == JOB
    assert payload["state"] == "cancelled"
    assert payload["bucket"] == FAKE_BUCKET
    assert payload["region"] == "us-east-1"
    assert payload["job_uri"] == f"s3://{FAKE_BUCKET}/jobs/{JOB}"
    assert "warnings" not in payload
    # One cancel invocation, driving the fleet script with --apply.
    argv = runner.argvs_matching("cancel")[0]
    assert argv == [
        str(_provision_script(tmp_path)),
        "cancel",
        JOB,
        "--apply",
        "--profile",
        sb.DEFAULT_BATCH_PROFILE,
        "--region",
        "us-east-1",
    ]


def test_cancel_requestless_matches_the_request_form(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner()
    runner.default = fake_completed(_CANCELLED_STDOUT)
    _install_fake_batch_transport(monkeypatch, runner=runner)
    path = _setup(tmp_path, monkeypatch, runner)
    assert main(["sim", str(path), "--cancel", JOB, "--format", "json"]) == 0
    with_request = json.loads(capsys.readouterr().out)

    runner.calls.clear()
    _cancel_env(tmp_path, monkeypatch, runner)
    assert main(["sim", "--cancel", JOB, "--format", "json"]) == 0
    without_request = json.loads(capsys.readouterr().out)

    assert without_request == with_request
    assert len(runner.argvs_matching("cancel")) == 1


@pytest.mark.parametrize(
    "fleet_config",
    [
        "BATCH_JOB_BUCKET=other-store\nBATCH_JOBS_PREFIX=jobs\n",
        f"BATCH_JOB_BUCKET={FAKE_BUCKET}\nBATCH_JOBS_PREFIX=other-prefix\n",
        None,
    ],
    ids=["different-bucket", "different-prefix", "missing-config"],
)
def test_cancel_refuses_unverified_job_store_before_apply(
    tmp_path, monkeypatch, capsys, fleet_config
):
    runner = _FakeRunner()
    path = _setup(tmp_path, monkeypatch, runner)
    request = json.loads(path.read_text())
    request["batch"]["jobs_prefix"] = "jobs"
    path.write_text(json.dumps(request))
    sidecar = tmp_path / "batch-fleet.env"
    if fleet_config is None:
        sidecar.unlink()
    else:
        sidecar.write_text(fleet_config)

    assert main(["sim", str(path), "--cancel", JOB, "--format", "json"]) == 1
    assert (
        "cannot cancel: the job store must match"
        in _stderr_json(capsys)["error"]["message"]
    )
    assert runner.calls == []


def test_cancel_text_format_prints_the_job_line(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner()
    runner.default = fake_completed(_CANCELLED_STDOUT)
    _cancel_env(tmp_path, monkeypatch, runner)

    assert main(["sim", "--cancel", JOB, "--format", "text"]) == 0

    out = capsys.readouterr().out
    assert f"cancelled: {JOB}" in out
    assert f"s3://{FAKE_BUCKET}/jobs/{JOB}" in out


def test_cancel_surfaces_script_warnings_bucket_redacted(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner()
    runner.default = fake_completed(
        _CANCELLED_STDOUT,
        stderr=(
            "WARNING: could not terminate i-0fake\n"
            f"WARNING: s3://{FAKE_BUCKET}/jobs/{JOB} stayed\n"
        ),
    )
    _cancel_env(tmp_path, monkeypatch, runner)

    assert main(["sim", "--cancel", JOB, "--format", "json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"] == [
        "WARNING: could not terminate i-0fake",
        f"WARNING: s3://<bucket>/jobs/{JOB} stayed",
    ]


def test_cancel_nothing_to_cancel_is_a_distinct_code(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner()
    runner.queue(
        "cancel",
        fake_completed(
            "",
            returncode=1,
            stderr=(
                f"error: job {JOB} has no pending or running instance in "
                "fleet eda-batch -- nothing to cancel, nothing written\n"
            ),
        ),
    )
    _cancel_env(tmp_path, monkeypatch, runner)

    assert main(["sim", "--cancel", JOB, "--format", "json"]) == 1

    error = _stderr_json(capsys)["error"]
    assert error["code"] == "batch_cancel_nothing_to_cancel"
    assert "nothing to cancel" in error["message"]


@pytest.mark.parametrize("status", [1, 75])
def test_cancel_other_failure_surfaces_redacted_stderr(
    tmp_path, monkeypatch, capsys, status
):
    runner = _FakeRunner()
    runner.queue(
        "cancel",
        fake_completed(
            "",
            returncode=status,
            stderr=(
                "error: aws cli failed: user is not authorized to perform "
                f"ec2:TerminateInstances on {FAKE_BUCKET}\n"
            ),
        ),
    )
    _cancel_env(tmp_path, monkeypatch, runner)

    assert main(["sim", "--cancel", JOB, "--format", "json"]) == 1

    error = _stderr_json(capsys)["error"]
    assert "code" not in error
    assert "ec2:TerminateInstances" in error["message"]
    assert FAKE_BUCKET not in error["message"]
    assert "<bucket>" in error["message"]


def test_cancel_invalid_job_id_refused_before_any_subprocess(
    tmp_path, monkeypatch, capsys
):
    runner = _FakeRunner()
    _cancel_env(tmp_path, monkeypatch, runner)

    assert main(["sim", "--cancel", "bad id; echo nope", "--format", "json"]) == 1

    error = _stderr_json(capsys)["error"]
    assert error["code"] == "batch_job_id_invalid"
    assert runner.calls == []


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--dry-run"], "--cancel cannot be combined with --dry-run"),
        (["--submit-only"], "--cancel cannot be combined with --submit-only"),
        (
            ["--wait-for-capacity", "30"],
            "--cancel cannot be combined with --wait-for-capacity",
        ),
        (["--attach", "klt-sim-other"], "--cancel cannot be combined with --attach"),
        (
            ["--job-status", "klt-sim-other"],
            "--cancel cannot be combined with --job-status",
        ),
        (["--backend", "local"], "--cancel drives the batch fleet"),
    ],
)
def test_cancel_refuses_conflicting_flags(
    tmp_path, monkeypatch, capsys, extra, message
):
    runner = _FakeRunner()
    _cancel_env(tmp_path, monkeypatch, runner)

    code = main(["sim", "--cancel", JOB, *extra, "--format", "json"])

    assert code == 2
    assert message in _stderr_json(capsys)["error"]["message"]
    assert runner.calls == []


def test_batch_set_refuses_cancel(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner()
    _cancel_env(tmp_path, monkeypatch, runner)

    code = main(["sim", "--batch-set", "set.json", "--cancel", JOB, "--format", "json"])

    assert code == 2
    assert (
        "--batch-set cannot be combined with --cancel"
        in _stderr_json(capsys)["error"]["message"]
    )
    assert runner.calls == []


def test_read_job_status_reports_cancelled_terminal(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("cancelled"))
    _install_fake_batch_transport(monkeypatch, runner=runner)

    status = sba.read_job_status(_batch_request(tmp_path), JOB)

    assert status["state"] == "cancelled"
    assert status["terminal"] is True
    assert status["known_state"] is True


def test_poll_status_fails_fast_on_cancelled_with_dedicated_code(tmp_path):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("cancelled"))
    clock = _Clock()
    config = sb.BatchConfig(
        provision_script="unused",
        bucket=FAKE_BUCKET,
        jobs_prefix="jobs",
        profile=sb.DEFAULT_BATCH_PROFILE,
        region="us-east-1",
        poll_interval_s=10.0,
        poll_timeout_s=600.0,
    )

    with pytest.raises(sb.BatchError) as err:
        sb.poll_status(
            config,
            JOB,
            runner=runner,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            progress=False,
        )

    assert err.value.code == "batch_job_cancelled"
    assert JOB in str(err.value)
    # Fail fast: the very first poll raised, no sleep was spent waiting.
    assert clock.now == 0.0


@pytest.mark.parametrize("state", ["done", "failed", "timeout"])
def test_terminal_or_cancelled_passes_plain_terminal_states(state):
    status = {"state": state}

    assert sbc.terminal_or_cancelled(JOB, status) is status


def test_terminal_or_cancelled_raises_only_on_cancelled():
    with pytest.raises(sb.BatchError) as err:
        sbc.terminal_or_cancelled(JOB, {"state": "cancelled"})

    assert err.value.code == "batch_job_cancelled"
