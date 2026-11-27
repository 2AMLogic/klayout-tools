"""Process and timeout handling for the Icarus Python runtime probe."""

import errno
import multiprocessing
import os
import signal
import subprocess
import time
import types

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


@pytest.mark.parametrize("driver_exits", [False, True], ids=["timeout", "driver-exit"])
def test_run_bounded_stops_the_driver_and_its_simulator(
    tmp_path, monkeypatch, driver_exits
):
    """The simulator is cleaned up even if the driver exits before it."""
    monkeypatch.setattr(rt, "PYTHON_RUNTIME_PROBE_TIMEOUT_S", 1.0)
    pids = tmp_path / "pids"

    def _launch_simulator():
        sleeper = subprocess.Popen(["sleep", "15"])
        pids.write_text(f"{os.getpid()} {sleeper.pid}")
        if driver_exits:
            os._exit(3)
        time.sleep(60)

    started = time.monotonic()
    outcome = rt._run_bounded(_launch_simulator)
    elapsed = time.monotonic() - started

    assert outcome["status"] == "inconclusive"
    reason = (
        "exited without a result (exit code 3)"
        if driver_exits
        else "no result within 1s"
    )
    assert reason in outcome["reason"]
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


def test_run_bounded_reports_a_pipe_it_cannot_open_as_inconclusive(monkeypatch):
    def _no_pipe(duplex):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(
        multiprocessing,
        "get_context",
        lambda method: types.SimpleNamespace(Pipe=_no_pipe),
    )

    outcome = rt._run_bounded(lambda: {"never": "reached"})

    assert outcome["status"] == "inconclusive"
    assert "result pipe" in outcome["reason"]
    assert "Too many open files" in outcome["reason"]


def test_a_child_that_cannot_isolate_its_group_measures_nothing(tmp_path, monkeypatch):
    """Still in klt's own process group, the child must not go on to start a
    simulator that a later cleanup could only reach by signalling that group."""
    ran = tmp_path / "ran"
    signalled = []

    def _refused(pid, pgid):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "setpgid", _refused)
    monkeypatch.setattr(os, "killpg", lambda *args: signalled.append(args))

    def _start_the_simulator():
        ran.write_text("started")
        return {}

    outcome = rt._run_bounded(_start_the_simulator)

    assert outcome["status"] == "inconclusive"
    assert "its own process group" in outcome["reason"]
    assert not ran.exists()
    assert signalled == []


@pytest.mark.parametrize("group_created", [True, False])
def test_kill_probe_child_targets_only_the_isolated_group_or_driver(
    monkeypatch, group_created
):
    sent = []
    monkeypatch.setattr(os, "killpg", lambda g, sig: sent.append(("group", g, sig)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(("pid", pid, sig)))

    rt._kill_probe_child(4242, group_created)

    assert sent == [("group" if group_created else "pid", 4242, signal.SIGKILL)]


def test_kill_probe_child_handles_an_already_exited_group(monkeypatch):
    def _gone(pgid, sig):
        raise ProcessLookupError(pgid)

    monkeypatch.setattr(os, "killpg", _gone)
    monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("group is gone"))
    rt._kill_probe_child(4242, group_created=True)
