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

    def _refused():
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "setpgrp", _refused)  # inherited by the forked child
    monkeypatch.setattr(os, "killpg", lambda *args: signalled.append(args))

    def _start_the_simulator():
        ran.write_text("started")
        return {}

    outcome = rt._run_bounded(_start_the_simulator)

    assert outcome["status"] == "inconclusive"
    assert "its own process group" in outcome["reason"]
    assert not ran.exists()
    assert signalled == []


def _record_signals(monkeypatch, group_of):
    """Replace the three ways ``_kill_probe_child`` can signal, so nothing is
    ever sent, and return what it tried to."""
    sent = []
    monkeypatch.setattr(os, "getpgid", group_of)
    monkeypatch.setattr(os, "killpg", lambda g, sig: sent.append(("group", g, sig)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(("pid", pid, sig)))
    return sent


def test_kill_probe_child_signals_the_group_the_child_leads(monkeypatch):
    sent = _record_signals(monkeypatch, lambda pid: pid)

    rt._kill_probe_child(4242)

    assert sent == [("group", 4242, signal.SIGKILL)]


def test_kill_probe_child_never_signals_a_group_it_does_not_lead(monkeypatch):
    """A child that stayed in klt's group is signalled alone, never the group
    ``getpgid`` reports for it -- klt's, and its caller's."""
    klt_group = os.getpgrp()
    sent = _record_signals(monkeypatch, lambda pid: klt_group)

    rt._kill_probe_child(4242)

    assert sent == [("pid", 4242, signal.SIGKILL)]


def test_kill_probe_child_survives_a_child_that_is_already_gone(monkeypatch):
    def _gone(pid):
        raise ProcessLookupError(pid)

    sent = _record_signals(monkeypatch, _gone)
    rt._kill_probe_child(None)
    assert sent == []

    rt._kill_probe_child(4242)
    assert sent == [("pid", 4242, signal.SIGKILL)]

    def _already_exited(pid, sig):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "kill", _already_exited)
    rt._kill_probe_child(4242)  # no exception
