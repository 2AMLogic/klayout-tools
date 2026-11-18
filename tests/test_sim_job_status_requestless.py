"""Request-less ``klt sim --job-status`` (issue #5810): bucket, region, and
profile resolve from the ``KLT_BATCH_*`` environment and the fleet config,
so a status read needs no positional request file. Split out of
``test_sim_batch_async.py`` to keep that file at its file-size ceiling --
the request-form tests stay there."""

from __future__ import annotations

import json

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_async as sba
from klayout_tools.cli import main
from test_sim_batch import (
    FAKE_BUCKET,
    _FakeRunner,
    _install_fake_batch_transport,
    _provision_script,
    _status,
)
from test_sim_batch_async import _setup, _stderr_json

# --------------------------------------------------------------------------- #
# --job-status without a request file (issue #5810): bucket/region/profile
# resolve from the KLT_BATCH_* environment and the fleet config.
# --------------------------------------------------------------------------- #


def _requestless_batch_env(tmp_path, monkeypatch, *, bucket=FAKE_BUCKET):
    """Env-resolution for a request-less ``--job-status``: the fleet stub as
    the provision script (no ``batch-fleet.env`` beside it) plus the bucket
    and region a request would otherwise carry."""
    monkeypatch.setenv(sb.PROVISION_SCRIPT_ENV, str(_provision_script(tmp_path)))
    if bucket is None:
        monkeypatch.delenv(sb.BUCKET_ENV, raising=False)
    else:
        monkeypatch.setenv(sb.BUCKET_ENV, bucket)
    monkeypatch.setenv(sb.REGION_ENV, "us-east-1")


def test_cli_job_status_without_request_matches_the_request_form(
    tmp_path, monkeypatch, capsys
):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    path = _setup(tmp_path, monkeypatch, runner)

    assert (
        main(["sim", str(path), "--job-status", "klt-sim-deadbeef", "--format", "json"])
        == 0
    )
    with_request = json.loads(capsys.readouterr().out)

    _requestless_batch_env(tmp_path, monkeypatch)
    assert main(["sim", "--job-status", "klt-sim-deadbeef", "--format", "json"]) == 0
    without_request = json.loads(capsys.readouterr().out)

    assert without_request == with_request
    assert without_request["state"] == "done"
    # A status read launches nothing, whatever resolved the config.
    assert runner.argvs_matching("launch") == []


def test_cli_job_status_without_request_unknown_job_exits_1_with_code(
    tmp_path, monkeypatch, capsys
):
    runner = _FakeRunner()
    runner.queue("status.json", fake_completed("", returncode=1))
    runner.queue("head-object", fake_completed("", returncode=254, stderr="Not Found"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _requestless_batch_env(tmp_path, monkeypatch)

    code = main(["sim", "--job-status", "klt-sim-nope", "--format", "json"])

    assert code == 1
    error = _stderr_json(capsys)["error"]
    assert error["code"] == sba.BATCH_JOB_NOT_FOUND_CODE


def test_cli_job_status_without_request_invalid_job_id(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner()
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _requestless_batch_env(tmp_path, monkeypatch)

    code = main(["sim", "--job-status", "../escape", "--format", "json"])

    assert code == 1
    error = _stderr_json(capsys)["error"]
    assert error["code"] == sba.BATCH_JOB_ID_INVALID_CODE
    assert runner.calls == []


def test_cli_job_status_without_request_and_without_bucket_is_the_bucket_error(
    tmp_path, monkeypatch, capsys
):
    """No request file and no bucket in the environment or fleet config: the
    existing job-bucket error (naming all three sources), not the argparse
    'request required' usage error."""
    runner = _FakeRunner()
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _requestless_batch_env(tmp_path, monkeypatch, bucket=None)

    code = main(["sim", "--job-status", "klt-sim-deadbeef", "--format", "json"])

    assert code == 1
    error = _stderr_json(capsys)["error"]
    assert "requires a job bucket" in error["message"]
    assert sb.BUCKET_ENV in error["message"]
    assert runner.calls == []


def test_cli_job_status_without_request_keeps_the_flag_conflicts(
    tmp_path, monkeypatch, capsys
):
    runner = _FakeRunner()
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _requestless_batch_env(tmp_path, monkeypatch)

    assert (
        main(
            [
                "sim",
                "--job-status",
                "klt-sim-deadbeef",
                "--pdk-root",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 2
    )
    error = _stderr_json(capsys)["error"]
    assert "--pdk-root has no effect with --job-status" in error["message"]
    assert runner.calls == []


def test_request_is_still_required_without_job_status(capsys):
    """A bare ``klt sim`` (and ``--op-lint``, which needs the request
    document) keep the argparse-style usage failure."""
    with pytest.raises(SystemExit) as exc:
        main(["sim"])
    assert exc.value.code == 2
    assert "arguments are required: request" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc:
        main(["sim", "--op-lint"])
    assert exc.value.code == 2
    assert "arguments are required: request" in capsys.readouterr().err
