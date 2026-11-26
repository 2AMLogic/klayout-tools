"""Clean up an isolated OpenROAD process group and its Docker container."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager


class _PendingSignal:
    """Hold the first signal until Popen has returned a process we can clean up."""

    def __init__(self) -> None:
        self.signum: int | None = None
        self.armed = False

    def handle(self, signum: int, frame) -> None:
        if self.signum is None:
            self.signum = signum
            if self.armed:
                self.arm()

    def arm(self) -> None:
        self.armed = True
        if self.signum == signal.SIGINT:
            raise KeyboardInterrupt
        if self.signum is not None:
            raise SystemExit(128 + self.signum)


@contextmanager
def _termination_signals() -> Iterator[Callable[[], None]]:
    """Unwind the main-thread stage on termination, then restore signal status."""
    pending = _PendingSignal()
    if os.name != "posix" or threading.current_thread() is not threading.main_thread():
        yield pending.arm
        return

    installed = {}
    try:
        for sig, default in (
            (signal.SIGTERM, signal.SIG_DFL),
            (signal.SIGHUP, signal.SIG_DFL),
            (signal.SIGINT, signal.default_int_handler),
        ):
            # Respect an embedding application's handler or ignored signal.
            if signal.getsignal(sig) == default:
                installed[sig] = signal.signal(sig, pending.handle)
        yield pending.arm
    finally:
        for sig, previous in installed.items():
            signal.signal(sig, previous)
        # SIGINT already propagates as KeyboardInterrupt after arming.
        if pending.signum is not None and (
            pending.signum != signal.SIGINT or not pending.armed
        ):
            signal.raise_signal(pending.signum)


#: Seconds :func:`_kill_openroad_run` waits after the process-group SIGTERM
#: before escalating to SIGKILL -- enough for the wrapper's EXIT/INT/TERM
#: handler to run its own ``docker kill``.
_KILL_GRACE_S = 5.0


def _kill_container(container_id: str, invocation_id: str) -> None:
    """Best-effort ``docker kill`` of a recorded container; never raises.

    Runs as a subprocess rather than through the group kill because a
    container belongs to no process group of this caller (issue #5726).
    ``docker`` may be absent (the engine cannot know whether ``openroad``
    is a wrapper) -- that failure is as silent as every other
    best-effort step here.
    """
    try:
        owner = subprocess.run(
            [
                "docker",
                "container",
                "inspect",
                "--format",
                '{{ index .Config.Labels "org.klayout-tools.openroad-invocation" }}',
                container_id,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_KILL_GRACE_S,
        )
        if owner.returncode or owner.stdout.strip() != invocation_id.encode("ascii"):
            return
        subprocess.run(
            ["docker", "kill", container_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_KILL_GRACE_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _kill_openroad_run(
    proc: subprocess.Popen, container_id: str | None, invocation_id: str
) -> None:
    """Kill a running engine invocation as thoroughly as possible.

    The child runs under ``start_new_session=True``, so its process group
    (pgid == ``proc.pid``) contains only the wrapper shell and the docker
    client it spawned -- never this process's own group. Three steps, each
    alone sufficient for a working setup, together covering the rest:

    1. SIGTERM to that group: the wrapper's own EXIT/INT/TERM handler
       ``docker kill``s its container on it.
    2. A direct ``docker kill`` of the recorded id: covers an installed
       wrapper whose handler failed. Older wrappers without an invocation
       label receive only the process-group signals.
    3. SIGKILL escalation after :data:`_KILL_GRACE_S`, then a final reap.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    if container_id:
        _kill_container(container_id, invocation_id)
    try:
        proc.wait(timeout=_KILL_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
