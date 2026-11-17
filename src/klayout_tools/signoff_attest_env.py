"""Declared-command environment for ``klt signoff --attest`` (#4192, #5565).

Split out of :mod:`klayout_tools.signoff_attest` to keep that module under
its file-size ceiling. Owns the environment every declared inventory command
(``cold_start.argv``, ``checks[].argv``) is launched with: the caller's own,
minus the tool venv's ``bin`` directory (#4192 -- a bare ``python3`` in an
``argv`` must resolve to the repo's interpreter, not the tool's), plus a
run-scoped shim directory holding a single ``klt`` symlink to the venv's
entry point (#5565), so the one name the stripping must not remove stays
reachable. The resulting invariant:

    ``klt`` on a declared command's ``PATH`` is always the ``klt`` running
    the attestation -- the venv's when attesting from a venv, the host's
    when not.

The shim is *prepended*, so it wins over a ``klt`` the caller placed earlier
on ``PATH``. When there is nothing to shim -- no ``klt`` beside
``sys.executable`` (e.g. ``python -m klayout_tools.cli``), or the shim
directory cannot be created -- the behaviour falls back to #4192's silently
rather than failing the attest. Only ``klt`` is re-exposed: ``python``,
``python3``, ``pip`` and every other name in the venv's ``bin`` still
resolve to the caller's. The shim is created at most once per attest run
(:func:`klt_shim_scope`) and removed when the run ends, so a run declaring
several commands leaves no residue in the tempdir; the per-run path never
enters the envelope."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

#: The one name the shim directory re-exposes from the stripped venv ``bin``.
_KLT = "klt"

#: Memoised shim directory for the current attest run: ``None`` until (and
#: unless) the first declared command needs it. Reset by
#: :func:`klt_shim_scope`; created once per run, never per command.
_shim_dir: str | None = None
_shim_decided = False


def _entry_point() -> str | None:
    """Absolute path of the ``klt`` console script beside ``sys.executable``
    (the venv's entry point -- the same ``klt`` running this attestation),
    or ``None`` when there is none."""
    entry = os.path.join(os.path.dirname(sys.executable), _KLT)
    if os.path.isfile(entry) and os.access(entry, os.X_OK):
        return os.path.abspath(entry)
    return None


def _shim_directory() -> str | None:
    """The run-scoped shim directory, created and memoised on first use; or
    ``None`` when there is nothing to shim or it cannot be created -- both
    fall back to #4192's behaviour rather than failing the attest."""
    global _shim_dir, _shim_decided
    if _shim_decided:
        return _shim_dir
    _shim_decided = True
    entry = _entry_point()
    if entry is None:
        return None
    shim: str | None = None
    try:
        shim = tempfile.mkdtemp(prefix="klt-attest-shim-")
        os.symlink(entry, os.path.join(shim, _KLT))
    except OSError:
        if shim is not None:
            shutil.rmtree(shim, ignore_errors=True)
        return None
    _shim_dir = shim
    return shim


def discard_shim() -> None:
    """Remove the current run's shim directory (if any) and forget the
    memoised decision, so the next attest run builds a fresh one."""
    global _shim_dir, _shim_decided
    if _shim_dir is not None:
        shutil.rmtree(_shim_dir, ignore_errors=True)
        _shim_dir = None
    _shim_decided = False


@contextmanager
def klt_shim_scope() -> Iterator[None]:
    """One attest run's declared-command sweep. Entering does nothing (the
    shim is built lazily by the first command that needs it); leaving
    retires the shim (:func:`discard_shim`), so a run declaring several
    commands shares one directory and leaves nothing behind."""
    try:
        yield
    finally:
        discard_shim()


def command_env() -> dict[str, str]:
    """Environment for a declared command: the caller's, minus the tool
    venv's ``bin`` directory on ``PATH`` when running inside a venv (as under
    ``uvx``/pipx), so a bare ``python3`` resolves to the repo's interpreter
    rather than the tool's (#4192) -- plus the run-scoped ``klt``-only shim
    prepended, so a bare ``klt`` resolves to the entry point of the very
    ``klt`` running the attestation (#5565). Outside a venv the environment
    is unchanged."""
    env = dict(os.environ)
    if sys.prefix == sys.base_prefix or "PATH" not in env:
        return env
    venv_bin = os.path.normcase(os.path.dirname(sys.executable))
    env["PATH"] = os.pathsep.join(
        entry
        for entry in env["PATH"].split(os.pathsep)
        if os.path.normcase(entry) != venv_bin
    )
    shim = _shim_directory()
    if shim is not None:
        env["PATH"] = shim + os.pathsep + env["PATH"]
    return env
