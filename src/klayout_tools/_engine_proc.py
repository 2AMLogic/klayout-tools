"""Run an engine subprocess in its own process group and reap the whole tree.

``subprocess.run(..., timeout=...)`` kills only the direct child. For yosys
that leaves the ``yosys-abc -s`` grandchild behind as an orphan (issue #5871),
holding a pipe end and host resources on a shared worker.

:func:`run_in_group` keeps calling :func:`subprocess.run` (so existing test
mocks of ``subprocess.run`` keep working) but starts the child as its own
session leader and ``killpg``s the group on every exit path: normal return,
timeout, non-zero exit, and ``KeyboardInterrupt``/any other ``BaseException``.
Exceptions are re-raised unchanged, so callers' ``TimeoutExpired`` / ``OSError``
handling is untouched.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from typing import Any


def _kill_group(pgid: int) -> None:
    """SIGKILL process group ``pgid``. Never raises, never hits our own group."""
    if not hasattr(os, "killpg") or pgid <= 1:
        return
    try:
        if pgid == os.getpgrp():
            return
        os.killpg(pgid, signal.SIGKILL)
    except OSError:  # ProcessLookupError: group already gone; EPERM: not ours
        pass


_REAL_POPEN = subprocess.Popen
_local = threading.local()
_install_lock = threading.Lock()
_install_refs = 0


class _RecordingPopen(_REAL_POPEN):  # type: ignore[valid-type, misc]
    """Popen that records its pid on the calling thread's active recorder."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        pids = getattr(_local, "pids", None)
        if pids is not None:
            pids.append(self.pid)


def _install() -> None:
    global _install_refs
    with _install_lock:
        _install_refs += 1
        subprocess.Popen = _RecordingPopen  # type: ignore[misc]


def _uninstall() -> None:
    global _install_refs
    with _install_lock:
        _install_refs -= 1
        if _install_refs == 0:
            subprocess.Popen = _REAL_POPEN  # type: ignore[misc]


def run_in_group(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run(argv, start_new_session=True, **kwargs)`` + group kill.

    ``subprocess.run`` hides its ``Popen``, and the pid is not carried on a
    ``TimeoutExpired``; a thread-local recording ``Popen`` (installed only
    for the duration of the call) captures it. The child is its own session
    leader, so pgid == pid; ``run`` has already reaped the child by the time
    we kill, but the group persists while any grandchild lives, so
    ``killpg(pid)`` still reaches it. Callers that mock ``subprocess.run``
    simply never record a pid and nothing is killed.
    """
    kwargs["start_new_session"] = True
    previous = getattr(_local, "pids", None)
    pids: list[int] = []
    _local.pids = pids
    _install()
    try:
        return subprocess.run(argv, **kwargs)
    finally:
        _uninstall()
        _local.pids = previous
        for pid in pids:
            _kill_group(pid)
