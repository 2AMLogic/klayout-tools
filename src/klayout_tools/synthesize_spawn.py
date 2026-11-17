"""The Yosys spawn seam, split out of :mod:`klayout_tools.synthesize`.

Issue #5798: a timed-out ``yosys -s`` run must die with its whole process
group, so a hung ``yosys-abc`` grandchild cannot outlive the watchdog that
killed its parent. Lives in its own module because
``klayout_tools.synthesize`` sits at its trusted per-file size-ratchet
ceiling and cannot grow (the same split precedent as ``e39b08c``). The
seam is new behaviour, not a pure move: on platforms with process groups
the child starts in its own session, which is what makes the group kill
possible.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping

#: Whether this platform can kill a child's whole process group -- the
#: orphan-prevention mechanism of issue #5798. ``os.killpg``/``os.getpgid``
#: exist on POSIX (Linux, macOS) and not on Windows, where the spawn degrades
#: to a plain ``subprocess.run`` (single-child kill, today's behaviour).
_CAN_KILL_PROCESS_GROUP = hasattr(os, "killpg") and hasattr(os, "getpgid")


def _kill_yosys_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the spawned child's whole process group (issue #5798).

    ``proc`` was started in its own session (:func:`_spawn_yosys`), so its
    process group holds every grandchild too -- Yosys runs ``yosys-abc`` as
    a child during ``abc`` passes, and killing the direct child alone is
    what orphaned a spinning abc past a watchdog kill (measured: one such
    orphan at 88% CPU five hours after its run died).

    May raise ``OSError`` (``ProcessLookupError`` if the group is already
    gone, ``PermissionError`` if the sandbox refuses the signal); the
    caller -- :func:`_spawn_yosys` -- is the best-effort boundary and must
    never let that mask the error it is about to raise.
    """
    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def _spawn_yosys(
    cmd: list[str],
    *,
    timeout: float | None,
    cwd: str | None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run ``cmd`` to completion with both streams captured -- the seam
    ``synthesize._run_yosys`` spawns every ``yosys -s`` script run through
    (issue #5798).

    Where process groups are available (:data:`_CAN_KILL_PROCESS_GROUP`)
    the child starts in its own session (``start_new_session=True``), so a
    timeout can SIGKILL the whole group -- Yosys runs ``yosys-abc`` as a
    grandchild, and a direct-child-only kill orphaned it. Otherwise this is
    ``subprocess.run`` verbatim. ``env`` is the child environment, applied
    on both branches (``None`` inherits this process's environment) --
    ``_run_yosys`` pins ``COLUMNS`` (issue #5780). The ``yosys -V`` and
    ``help abc`` capability probes spawn no abc child and never go through
    this seam.

    A raised ``subprocess.TimeoutExpired`` carries the drained
    stdout/stderr: ``communicate`` stops mid-read at the timeout, so after
    the group kill both pipes are drained to EOF and the partial output
    read so far is attached to the exception -- the fields a bare
    ``Popen`` timeout leaves as ``None``, and which
    ``synthesize._write_yosys_log`` would otherwise silently drop.
    """
    if not _CAN_KILL_PROCESS_GROUP:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env
        )
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # communicate(timeout=None) blocks forever and never raises, so
        # this branch is only reachable with a finite timeout.
        assert timeout is not None
        try:
            _kill_yosys_process_group(proc)
        except OSError:
            # ProcessLookupError (group already gone) / PermissionError
            # (sandbox refuses the signal): best effort by contract -- the
            # timeout error below must never be masked.
            pass
        proc.wait()
        # The group is dead (or the kill was refused and the child exited on
        # its own), so both pipes reach EOF and this second communicate()
        # returns what buffered before the timeout fired.
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(
            cmd, timeout, output=stdout, stderr=stderr
        ) from None
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
