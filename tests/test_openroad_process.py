"""In-process unit tests for ``_openroad_process`` (issue #5726).

The end-to-end signal regressions (``test_place_and_route``'s driver tests and
``test_openroad_container_lifetime``) run the driver in a subprocess, so the
coverage measurement never sees the handler, escalation and best-effort
failure paths exercised here. These tests drive them directly with a fake
process and recorded ``os.killpg`` / ``docker kill`` calls: nothing real is
signaled except this test process's own, handled, signals.
"""

from __future__ import annotations

import os
import signal
import subprocess

import pytest

from klayout_tools import _openroad_process as process

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="process groups and POSIX termination signals"
)

#: ``signal.raise_signal`` as imported, before any test replaces the module
#: attribute: it runs the Python-level handler before it returns, which makes
#: "the signal arrived here" deterministic.
_deliver = signal.raise_signal

CID = "c0ffee" + "0" * 58
PGID = 31337


class _FakeProc:
    """A ``Popen`` stand-in whose ``wait`` follows a script of outcomes."""

    def __init__(self, waits: list[str]) -> None:
        self.pid = PGID
        self.returncode: int | None = None
        self.wait_timeouts: list[float | None] = []
        self._waits = list(waits)

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self._waits.pop(0) == "timeout":
            raise subprocess.TimeoutExpired("openroad", timeout)
        self.returncode = 0
        return self.returncode


def _record_killpg(monkeypatch, error: type[Exception] | None = None):
    """Record ``os.killpg`` calls (raising ``error`` after recording, if set)."""
    calls: list[tuple[int, int]] = []

    def killpg(pgid, sig):
        calls.append((pgid, sig))
        if error is not None:
            raise error

    monkeypatch.setattr(os, "killpg", killpg)
    return calls


@pytest.fixture
def docker_kills(monkeypatch):
    """Record ``docker kill`` argv instead of running docker."""
    argvs: list[list[str]] = []

    def fake_run(argv, **kwargs):
        argvs.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(process.subprocess, "run", fake_run)
    return argvs


@pytest.fixture
def default_dispositions():
    """Known SIGTERM/SIGHUP/SIGINT dispositions, restored afterwards."""
    wanted = {
        signal.SIGTERM: signal.SIG_DFL,
        signal.SIGHUP: signal.SIG_DFL,
        signal.SIGINT: signal.default_int_handler,
    }
    saved = {sig: signal.signal(sig, handler) for sig, handler in wanted.items()}
    yield wanted
    for sig, previous in saved.items():
        signal.signal(sig, previous)


@pytest.fixture
def replayed(monkeypatch):
    """Capture the signal ``_termination_signals`` replays on exit, so the
    test process is never actually terminated."""
    sent: list[int] = []
    monkeypatch.setattr(signal, "raise_signal", sent.append)
    return sent


# -- _PendingSignal -----------------------------------------------------------


def test_pending_signal_defers_until_armed_then_unwinds_conventionally():
    pending = process._PendingSignal()
    pending.handle(signal.SIGTERM, None)  # not armed: remembered, not raised
    pending.handle(signal.SIGHUP, None)  # only the first signal counts
    assert pending.signum == signal.SIGTERM
    with pytest.raises(SystemExit) as exc:
        pending.arm()
    assert exc.value.code == 128 + signal.SIGTERM
    assert pending.armed


def test_pending_sigint_unwinds_as_keyboard_interrupt_when_armed():
    pending = process._PendingSignal()
    pending.handle(signal.SIGINT, None)
    with pytest.raises(KeyboardInterrupt):
        pending.arm()


def test_arming_without_a_pending_signal_does_nothing():
    pending = process._PendingSignal()
    pending.arm()
    assert pending.armed
    assert pending.signum is None


@pytest.mark.parametrize(
    ("name", "unwinds_as"),
    [
        ("SIGTERM", SystemExit),
        ("SIGHUP", SystemExit),
        ("SIGINT", KeyboardInterrupt),
    ],
)
def test_a_signal_after_arming_unwinds_immediately(name, unwinds_as):
    pending = process._PendingSignal()
    pending.arm()
    with pytest.raises(unwinds_as):
        pending.handle(getattr(signal, name), None)


# -- _termination_signals -----------------------------------------------------


def test_termination_signals_install_and_restore_the_handlers(default_dispositions):
    with process._termination_signals():
        for sig in default_dispositions:
            installed = signal.getsignal(sig)
            assert getattr(installed, "__func__", None) is process._PendingSignal.handle
    for sig, expected in default_dispositions.items():
        assert signal.getsignal(sig) == expected


def test_termination_signals_respect_an_embedding_handler(default_dispositions):
    def embedding_handler(signum, frame):  # pragma: no cover - never delivered
        raise AssertionError("not delivered in this test")

    signal.signal(signal.SIGTERM, embedding_handler)
    with process._termination_signals():
        assert signal.getsignal(signal.SIGTERM) is embedding_handler
        # ... while the dispositions it did not customize are still taken over.
        assert signal.getsignal(signal.SIGHUP) != signal.SIG_DFL
    assert signal.getsignal(signal.SIGTERM) is embedding_handler


@pytest.mark.parametrize("name", ["SIGTERM", "SIGHUP"])
@pytest.mark.parametrize("when", ["before-arming", "after-arming"])
def test_a_terminating_signal_unwinds_then_is_replayed_on_exit(
    default_dispositions, replayed, name, when
):
    """Before arming (the process is still launching) the signal waits for
    ``arm()``; after, it unwinds at once. Either way the caller sees
    ``SystemExit(128 + signum)`` and the signal is replayed once the previous
    disposition is back, so the exit status still reads as death-by-signal."""
    signum = getattr(signal, name)
    with pytest.raises(SystemExit) as exc:
        with process._termination_signals() as arm:
            if when == "after-arming":
                arm()
                _deliver(signum)
            else:
                _deliver(signum)
                arm()
    assert exc.value.code == 128 + signum
    assert signal.getsignal(signum) == signal.SIG_DFL
    assert replayed == [signum]


def test_sigint_after_arming_is_a_plain_keyboard_interrupt(
    default_dispositions, replayed
):
    with pytest.raises(KeyboardInterrupt):
        with process._termination_signals() as arm:
            arm()
            _deliver(signal.SIGINT)
    # KeyboardInterrupt already carries the interrupt: nothing is replayed.
    assert replayed == []
    assert signal.getsignal(signal.SIGINT) == signal.default_int_handler


@pytest.mark.parametrize("name", ["SIGTERM", "SIGHUP", "SIGINT"])
def test_a_signal_that_is_never_armed_is_replayed_on_exit(
    default_dispositions, replayed, name
):
    """A signal that arrived with nothing to unwind (``arm()`` never ran) is
    not swallowed: it is re-raised once the old disposition is restored."""
    signum = getattr(signal, name)
    with process._termination_signals():
        _deliver(signum)
    assert replayed == [signum]


# -- _kill_container ----------------------------------------------------------


def test_kill_container_runs_a_quiet_bounded_docker_kill(monkeypatch):
    seen: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(process.subprocess, "run", fake_run)
    process._kill_container(CID)
    [(argv, kwargs)] = seen
    assert argv == ["docker", "kill", CID]
    assert kwargs["check"] is False
    assert kwargs["timeout"] == process._KILL_GRACE_S
    assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL


@pytest.mark.parametrize(
    "failure",
    [FileNotFoundError("docker"), subprocess.TimeoutExpired("docker", 5.0)],
    ids=["docker-absent", "docker-hangs"],
)
def test_kill_container_is_best_effort(monkeypatch, failure):
    def failing_run(argv, **kwargs):
        raise failure

    monkeypatch.setattr(process.subprocess, "run", failing_run)
    process._kill_container(CID)  # must not raise


# -- _kill_openroad_run -------------------------------------------------------


def test_kill_run_terminates_the_group_and_kills_the_container(
    monkeypatch, docker_kills
):
    killpg_calls = _record_killpg(monkeypatch)
    proc = _FakeProc(["exit"])
    process._kill_openroad_run(proc, CID)
    assert killpg_calls == [(PGID, signal.SIGTERM)]  # reaped in time: no SIGKILL
    assert docker_kills == [["docker", "kill", CID]]
    assert proc.wait_timeouts == [process._KILL_GRACE_S]


def test_kill_run_without_a_container_never_calls_docker(monkeypatch, docker_kills):
    _record_killpg(monkeypatch)
    process._kill_openroad_run(_FakeProc(["exit"]), None)
    assert docker_kills == []


@pytest.mark.parametrize("error", [ProcessLookupError, PermissionError])
def test_kill_run_tolerates_a_group_it_cannot_signal(monkeypatch, docker_kills, error):
    """The group may already be gone (or no longer ours): the SIGTERM failing
    must not skip the container kill or the reap."""
    killpg_calls = _record_killpg(monkeypatch, error)
    proc = _FakeProc(["exit"])
    process._kill_openroad_run(proc, CID)
    assert killpg_calls == [(PGID, signal.SIGTERM)]
    assert docker_kills == [["docker", "kill", CID]]
    assert proc.returncode == 0


def test_kill_run_escalates_to_sigkill_when_the_group_outlives_the_grace(
    monkeypatch, docker_kills
):
    killpg_calls = _record_killpg(monkeypatch)
    proc = _FakeProc(["timeout", "exit"])
    process._kill_openroad_run(proc, CID)
    assert killpg_calls == [(PGID, signal.SIGTERM), (PGID, signal.SIGKILL)]
    assert docker_kills == [["docker", "kill", CID]]
    assert proc.wait_timeouts == [process._KILL_GRACE_S, process._KILL_GRACE_S]
    assert proc.returncode == 0  # reaped by the final wait


@pytest.mark.parametrize("error", [None, ProcessLookupError, PermissionError])
def test_kill_run_gives_up_quietly_on_an_unreapable_group(
    monkeypatch, docker_kills, error
):
    """Neither SIGTERM nor SIGKILL nor the final wait may raise or hang the
    caller's own teardown -- the run was already being aborted."""
    killpg_calls = _record_killpg(monkeypatch, error)
    proc = _FakeProc(["timeout", "timeout"])
    process._kill_openroad_run(proc, CID)
    assert killpg_calls == [(PGID, signal.SIGTERM), (PGID, signal.SIGKILL)]
    assert proc.wait_timeouts == [process._KILL_GRACE_S, process._KILL_GRACE_S]
    assert proc.returncode is None
