"""Process and timeout handling for the Icarus Python runtime probe."""

import os
import subprocess
import time

import pytest

from klayout_tools import functional_verification_python_runtime as rt


def test_run_bounded_returns_what_the_child_measured():
    assert rt._run_bounded(lambda value: {"value": value}, value=7) == {
        "status": "measured",
        "value": 7,
    }


def test_run_bounded_relays_a_child_exception_as_inconclusive():
    def _boom():
        raise RuntimeError("boom")

    outcome = rt._run_bounded(_boom)
    assert outcome["status"] == "inconclusive"
    assert "RuntimeError: boom" in outcome["reason"]


def test_run_bounded_reports_a_child_that_dies_silent():
    def _die():
        os._exit(3)

    outcome = rt._run_bounded(_die)
    assert outcome["status"] == "inconclusive"
    assert "exited without a result (exit code 3)" in outcome["reason"]


def test_run_bounded_kills_a_hung_child_and_its_simulator(tmp_path, monkeypatch):
    """A probe that wedges -- here a child sleeping behind a ``sleep``
    grandchild standing in for ``vvp`` -- costs the bound, not the session,
    and leaves neither process behind."""
    monkeypatch.setattr(rt, "PYTHON_RUNTIME_PROBE_TIMEOUT_S", 1.0)
    pids = tmp_path / "pids"

    def _wedge():
        sleeper = subprocess.Popen(["sleep", "60"])
        pids.write_text(f"{os.getpid()} {sleeper.pid}")
        time.sleep(60)

    started = time.monotonic()
    outcome = rt._run_bounded(_wedge)
    elapsed = time.monotonic() - started

    assert outcome["status"] == "inconclusive"
    assert "no result within 1s" in outcome["reason"]
    assert elapsed < 30
    child, grandchild = map(int, pids.read_text().split())
    for pid in (child, grandchild):
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"process {pid} outlived the probe")
