"""Route-level ``delete`` refusals map to ``unsupported_operation``.

A 404/405/501 on the ``delete`` call itself means the broker has no
``/v1/delete`` route (``stat`` already confirmed the object present), so
``klt artifact delete`` reports error.code ``unsupported_operation``; any
other refusal (403 authorization, ...) stays ``broker_refused``. Split out
of ``test_artifact_store.py`` to keep it under its file-size ceiling.
"""

from __future__ import annotations

import json
import logging

import pytest

from test_artifact_store import REPO, _run, _stored, broker, report  # noqa: F401  (broker/report: pytest fixtures)


@pytest.fixture(autouse=True)
def no_url_leak(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    yield
    assert "fake0secret" not in caplog.text


@pytest.mark.parametrize("status", [404, 405, 501])
def test_route_level_delete_refusal_is_unsupported_operation(
    status, tmp_path, capsys, request
):
    fake_broker = request.getfixturevalue("broker")
    report_path = request.getfixturevalue("report")
    _, sha = _stored(fake_broker, report_path, tmp_path, capsys)
    fake_broker.refuse["delete"] = (
        status,
        "http_404",
        "only POST /v1/{put,get,stat,list}",
    )
    code, _, err = _run(
        ["artifact", "delete", sha, "--repo", REPO, "--yes", "--format", "json"],
        capsys,
    )
    error = json.loads(err)["error"]
    assert code == 1
    assert error["code"] == "unsupported_operation"
    assert error["broker_status"] == status
    assert error["broker_error"] == "http_404"
    assert "does not implement" in error["message"]
    assert "nothing was deleted" in error["message"]


def test_other_delete_refusal_stays_broker_refused(tmp_path, capsys, request):
    fake_broker = request.getfixturevalue("broker")
    report_path = request.getfixturevalue("report")
    _, sha = _stored(fake_broker, report_path, tmp_path, capsys)
    fake_broker.refuse["delete"] = (403, "not_granted", "not your repo")
    code, _, err = _run(
        ["artifact", "delete", sha, "--repo", REPO, "--yes", "--format", "json"],
        capsys,
    )
    error = json.loads(err)["error"]
    assert code == 1
    assert error["code"] == "broker_refused"
    assert error["broker_status"] == 403
