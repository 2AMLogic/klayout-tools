"""Running one declared ``klt signoff --attest`` command, plus the two
host-specific side-channels a run feeds (split out of :mod:`.signoff_attest`
for the file-size ratchet; issues #4848, #5817).

A check dict built by :mod:`.signoff_attest` carries only reproducible,
host-independent facts, because the envelope -- and its content hash -- must
be identical on every host. Two facts about a command run are therefore held
out of band here (never in a check dict built by an attestation builder), and
the CLI adds them to the *live* ``--format json`` result only, after any
``--output`` write and ``--check`` diff:

- a failing command's bounded output tail (``output_tail``, issue #4698);
- the observed wall time of every command that launched, passing, failing or
  timed-out alike (``duration_s``, issue #5817).

Neither is ever written to an ``-o`` file, appears in a ``--check`` diff, or
is produced by an attestation builder: a wall-clock value never repeats, so
either inside the envelope would rewrite the committed bytes on every run and
read as permanent drift.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Mapping
from typing import Any

from .signoff import _COMMAND_EVIDENCE_TIMEOUT_S

# Bounded tail of a failing command's captured output (issue #4698). Held out
# of band (never in a check dict built by an attestation builder) because it
# is host-specific and the envelope -- and its content hash -- must stay
# reproducible. The CLI adds it to the live ``--format json`` result via
# :func:`attach_output_tails`, after any ``--output`` write and ``--check``
# diff.
_OUTPUT_TAIL_MAX_LINES = 20
_OUTPUT_TAIL_MAX_BYTES = 2048
_failed_output: dict[tuple[tuple[str, ...], str], str] = {}


def _bounded_tail(text: str) -> str:
    lines = text.rstrip().splitlines()[-_OUTPUT_TAIL_MAX_LINES:]
    tail = "\n".join(lines)
    encoded = tail.encode("utf-8")
    if len(encoded) > _OUTPUT_TAIL_MAX_BYTES:
        tail = encoded[-_OUTPUT_TAIL_MAX_BYTES:].decode("utf-8", errors="replace")
    return tail


def failed_output_tail(check: Mapping[str, Any]) -> str:
    """Bounded tail of the captured stdout+stderr of the failing command
    ``check`` in the most recent attestation run, or ``""`` (a passing,
    skipped or unlaunchable check has none)."""
    return _failed_output.get(
        (tuple(check.get("argv") or ()), check.get("cwd", "")), ""
    )


def attach_output_tails(envelope: Mapping[str, Any]) -> None:
    """Add the additive ``output_tail`` string to each failing command check of
    ``envelope`` (in place). Live-result only: call after the envelope has
    been written or diffed, never before."""
    for check in (envelope.get("attestation") or {}).get("checks") or []:
        if check.get("check") == "command" and not check.get("passed"):
            tail = failed_output_tail(check)
            if tail:
                check["output_tail"] = tail


# Observed wall time of each command this run launched, in seconds rounded to
# one decimal (issue #5817). Held out of band exactly like ``_failed_output``
# above, and for the same reason -- see the module docstring. The CLI adds it
# to the live ``--format json`` result via :func:`attach_durations`, after any
# ``--output`` write and ``--check`` diff. Unlike the tail it attaches to
# passing checks too: a passing check drifting toward its declared budget is
# the failure this field exists to surface before the timeout does.
_durations: dict[tuple[tuple[str, ...], str], float] = {}


def reset_durations() -> None:
    """Forget every recorded wall time. Called at the start of each
    attestation build: a duration marks the run that produced it, so a later
    build in the same process -- one that skips or carries a check without
    re-running it -- must never serve this build's value to a check that did
    not run. (The per-key ``pop`` in :func:`_run_command` alone cannot give
    that guarantee: a skipped command is never entered, so its key would keep
    the previous build's value alive.)"""
    _durations.clear()


def record_duration(key: tuple[tuple[str, ...], str], started: float) -> None:
    """Record the wall time since ``started`` (a ``time.monotonic()`` reading)
    for the command run identified by ``key``. The timeout path records too:
    the observed time is the interesting number precisely when the budget was
    hit. Only a command that never launched (``OSError``) records nothing."""
    _durations[key] = round(time.monotonic() - started, 1)


def command_duration(check: Mapping[str, Any]) -> float | None:
    """Observed wall time in seconds of the command ``check`` in the most
    recent attestation run, or ``None`` (never launched, skipped, or carried
    from the committed envelope)."""
    return _durations.get((tuple(check.get("argv") or ()), check.get("cwd", "")))


def attach_durations(envelope: Mapping[str, Any]) -> None:
    """Add the additive ``duration_s`` float to each command check of
    ``envelope`` that this run actually launched (in place) -- passing,
    failing and timed-out alike. Live-result only: call after the envelope
    has been written or diffed, never before."""
    for check in (envelope.get("attestation") or {}).get("checks") or []:
        if check.get("check") == "command":
            duration = command_duration(check)
            if duration is not None:
                check["duration_s"] = duration


def _command_env() -> dict[str, str]:
    """Environment for a declared command: the caller's, minus the tool
    venv's ``bin`` directory on ``PATH`` when running inside a venv (as under
    ``uvx``/pipx), so a bare ``python3`` resolves to the repo's interpreter
    rather than the tool's. Outside a venv the environment is unchanged."""
    env = dict(os.environ)
    if sys.prefix == sys.base_prefix or "PATH" not in env:
        return env
    venv_bin = os.path.normcase(os.path.dirname(sys.executable))
    env["PATH"] = os.pathsep.join(
        entry
        for entry in env["PATH"].split(os.pathsep)
        if os.path.normcase(entry) != venv_bin
    )
    return env


def _run_command(root: str, spec: Mapping[str, Any]) -> tuple[int | None, str | None]:
    """Run one declared command; return ``(exit_status, error)``. ``error``
    is ``"launch_failed"``/``"timeout"`` (stable tokens, not host-specific
    messages, so the envelope stays reproducible) or ``None``. A failing
    command's output tail is kept for :func:`failed_output_tail`; every
    launched command's wall time is kept for :func:`command_duration`."""
    cwd = os.path.join(root, spec["cwd"]) if spec["cwd"] != "." else root
    key = (tuple(spec["argv"]), spec["cwd"])
    _failed_output.pop(key, None)
    _durations.pop(key, None)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            spec["argv"],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=spec.get("timeout_s", _COMMAND_EVIDENCE_TIMEOUT_S),
            check=False,
            env=_command_env(),
        )
    except subprocess.TimeoutExpired:
        record_duration(key, started)
        return None, "timeout"
    except OSError:
        return None, "launch_failed"
    record_duration(key, started)
    if completed.returncode != 0:
        tail = _bounded_tail(completed.stdout.decode("utf-8", errors="replace"))
        if tail:
            _failed_output[key] = tail
    return completed.returncode, None
