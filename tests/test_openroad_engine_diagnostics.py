"""Environment-failure classification for OpenROAD runs (issue #4440).

Subprocess is stubbed throughout: neither Docker nor OpenROAD is required.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from klayout_tools import _openroad_engine as engine
from klayout_tools._openroad_engine import _run_openroad, container_runtime_hint
from klayout_tools.place_and_route import _engine_error_message


class _Err(Exception):
    pass


def _completed(returncode=1, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _no_batch_env(monkeypatch):
    monkeypatch.delenv("KLT_SIM_BACKEND", raising=False)


@pytest.mark.parametrize(
    ("completed", "needle"),
    [
        (
            _completed(
                127, stderr="/usr/bin/openroad: line 3: docker: command not found"
            ),
            "'docker' command is not installed",
        ),
        (
            _completed(125, stderr="docker: permission denied while trying to connect"),
            "not permitted to reach",
        ),
        (
            _completed(
                1,
                stderr="Cannot connect to the Docker daemon. Is it running?",
            ),
            "daemon is running",
        ),
        (
            _completed(125, stderr="Unable to find image 'openroad:x' locally"),
            "image is not present",
        ),
        (
            _completed(125, stderr="Error response: pull access denied for openroad"),
            "image is not present",
        ),
    ],
)
def test_classifies_each_signature(completed, needle):
    hint = container_runtime_hint(completed)
    assert hint is not None and needle in hint


def test_exit_127_without_docker_is_not_classified():
    assert (
        container_runtime_hint(_completed(127, stderr="foo: command not found")) is None
    )


def test_docker_not_found_needs_exit_127():
    assert container_runtime_hint(_completed(1, stderr="docker: not found")) is None


def test_no_match_passthrough():
    assert container_runtime_hint(_completed(1, stderr="[ERROR ORD-1] bad")) is None
    message = _engine_error_message(
        "floorplan", _completed(1, stderr="[ERROR ORD-1] bad")
    )
    assert message == "openroad 'floorplan' stage failed: [ERROR ORD-1] bad"


def test_batch_host_points_at_fleet(monkeypatch):
    monkeypatch.setenv("KLT_SIM_BACKEND", "batch")
    for completed in (
        _completed(125, stderr="docker: permission denied on /var/run/docker.sock"),
        _completed(127, stderr="docker: command not found"),
        _completed(125, stderr="Unable to find image"),
    ):
        hint = container_runtime_hint(completed)
        assert "--backend batch" in hint
        assert "docker' group" not in hint


def test_place_and_route_message_includes_hint():
    message = _engine_error_message(
        "floorplan", _completed(127, stderr="docker: command not found")
    )
    assert "'docker' command is not installed" in message


@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (FileNotFoundError(2, "No such file", "openroad"), "not on PATH"),
        (PermissionError(13, "denied", "openroad"), "not executable"),
    ],
)
def test_launch_failure_named(monkeypatch, tmp_path, exc, needle):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(subprocess, "run", boom)
    script = tmp_path / "s.tcl"
    script.write_text("exit\n")
    with pytest.raises(_Err, match="could not launch openroad") as info:
        _run_openroad(str(script), str(tmp_path / "m.json"), error_cls=_Err)
    assert needle in str(info.value)


def test_launch_failure_other_oserror_unchanged(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise OSError("weird")

    monkeypatch.setattr(subprocess, "run", boom)
    script = tmp_path / "s.tcl"
    script.write_text("exit\n")
    with pytest.raises(_Err) as info:
        _run_openroad(str(script), str(tmp_path / "m.json"), error_cls=_Err)
    assert "could not launch openroad: weird -- openroad invocation" in str(info.value)
    assert engine._launch_failure_hint(OSError("weird")) == ""
