"""An empty job id on the request-less job-by-id modes (issue #7651).

``klt sim --job-status ""`` / ``--cancel ""`` must reach the job-id validator
and fail in the JSON envelope (``batch_job_id_invalid``, exit 1) -- not fall
into the argparse-style "request is required" usage error (stderr, exit 2).
The request-optional exemption in ``cli/sim_cmd.py`` has to test the flags'
*presence*, not their truthiness: ``""`` is the one falsy ``str``.

Split out of ``test_sim_job_status_requestless.py`` (which keeps the
non-empty-id request-less cases) to stay under that file's size ceiling.
"""

from __future__ import annotations

import pytest

from klayout_tools import sim_batch_async as sba
from klayout_tools.cli import main
from test_sim_batch import _FakeRunner, _install_fake_batch_transport
from test_sim_batch_async import _stderr_json
from test_sim_job_status_requestless import _requestless_batch_env


@pytest.mark.parametrize("flag", ["--job-status", "--cancel"])
def test_empty_job_id_without_request_is_batch_job_id_invalid(
    flag, tmp_path, monkeypatch, capsys
):
    runner = _FakeRunner()
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _requestless_batch_env(tmp_path, monkeypatch)

    code = main(["sim", flag, "", "--format", "json"])

    assert code == 1
    error = _stderr_json(capsys)["error"]
    assert error["code"] == sba.BATCH_JOB_ID_INVALID_CODE
    # Rejected before any subprocess: nothing reached the fleet transport.
    assert runner.calls == []


def test_request_is_still_required_without_a_job_id_flag(capsys):
    """Unchanged: a bare ``klt sim`` and ``--op-lint`` with no request keep
    the argparse-style usage failure (exit 2, same stderr message)."""
    for argv in (["sim"], ["sim", "--op-lint"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2
        assert "arguments are required: request" in capsys.readouterr().err
