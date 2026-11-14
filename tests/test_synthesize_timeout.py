"""``yosys_timeout_s`` request-level Yosys budget (issue #5412)."""

from __future__ import annotations

import subprocess

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import synthesize, synthesize_timeout
from klayout_tools.synthesize import SynthesizeError, run_synthesize
from test_synthesize import (
    _ABC_HELP_WITH_DONT_USE,
    _base_request,
    _setup_success_env,
    _stub_yosys_success,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _record_yosys_timeouts(monkeypatch) -> list:
    """Wrap the stubbed ``subprocess.run``; record each ``yosys -s`` timeout."""
    inner = synthesize.subprocess.run
    seen: list = []

    def recording(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-s"]:
            seen.append(kwargs.get("timeout"))
        return inner(cmd, **kwargs)

    monkeypatch.setattr(synthesize.subprocess, "run", recording)
    return seen


def test_yosys_timeout_default_unchanged_and_not_echoed(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    seen = _record_yosys_timeouts(monkeypatch)

    report = run_synthesize(request_path)

    assert seen and set(seen) == {synthesize.DEFAULT_YOSYS_TIMEOUT_S}
    assert "yosys_timeout_s" not in report
    assert "yosys_timeout_s" not in report["provenance"]


def test_yosys_timeout_raised_reaches_every_run_and_is_echoed(tmp_path, monkeypatch):
    _setup_success_env(tmp_path, monkeypatch)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(yosys_timeout_s=1800)
    )
    _stub_yosys_success(monkeypatch)
    seen = _record_yosys_timeouts(monkeypatch)

    report = run_synthesize(request_path)

    assert seen and set(seen) == {1800.0}
    assert report["yosys_timeout_s"] == 1800.0
    assert report["provenance"]["yosys_timeout_s"] == 1800.0
    # The binding does not leak into a later request.
    assert synthesize_timeout._YOSYS_TIMEOUT_VAR.get() is None


def test_run_yosys_default_resolves_to_bound_budget(tmp_path, monkeypatch):
    """Helpers in synthesize_arithmetic/synthesize_memory call `_run_yosys`
    with no timeout, so they pick up the run's bound budget (and the explicit
    keyword still wins)."""
    seen: list = []

    def fake_run(cmd, **kwargs):
        seen.append(kwargs.get("timeout"))
        return fake_completed(returncode=0)

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)
    script = str(tmp_path / "s.ys")
    token = synthesize_timeout._YOSYS_TIMEOUT_VAR.set(900.0)
    try:
        synthesize._run_yosys(script)
        synthesize._run_yosys(script, timeout_s=None)
    finally:
        synthesize_timeout._YOSYS_TIMEOUT_VAR.reset(token)
    synthesize._run_yosys(script)
    assert seen == [900.0, None, synthesize.DEFAULT_YOSYS_TIMEOUT_S]


@pytest.mark.parametrize(
    "bad", [0, -5, True, False, "600", float("nan"), float("inf"), 3600.5, [1]]
)
def test_yosys_timeout_rejects_bad_values(tmp_path, monkeypatch, bad):
    _setup_success_env(tmp_path, monkeypatch)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(yosys_timeout_s=bad)
    )
    with pytest.raises(SynthesizeError, match="yosys_timeout_s.*3600"):
        run_synthesize(request_path)


def test_yosys_timeout_error_names_the_budget(tmp_path, monkeypatch):
    _setup_success_env(tmp_path, monkeypatch)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(yosys_timeout_s=0.5)
    )

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["yosys", "-p", "help abc"]:
            return fake_completed(stdout=_ABC_HELP_WITH_DONT_USE)
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    with pytest.raises(SynthesizeError, match=r"did not complete within 0\.5s"):
        run_synthesize(request_path)
