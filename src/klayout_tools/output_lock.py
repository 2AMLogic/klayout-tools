"""Advisory output-directory ownership for ``klt pex`` / ``sim`` / ``place-and-route``.

Issue #2770. With several agents driving ``klt`` at once, two runs pointed at
the same persistent artifact root can interleave writes and leave mixed
artifacts with no error. This module is the one shared mechanism that
prevents that: a run takes a **non-blocking, OS-backed advisory lock** on its
artifact root for its lifetime and a competing run fails fast with
:class:`OutputDirLockedError` (CLI: ``error.code == "outdir_locked"``).

Design (clean-room; no third-party lock code was consulted):

* **OS lock, not metadata.** The lock is ``flock(2)`` on a lock file that is
  never unlinked or recreated, so every process locks the same inode. A dead
  owner releases the lock automatically, so a stale lock cannot exist; the
  JSON owner record inside the file (``pid``, ``acquired_at``) is purely
  diagnostic, is overwritten by the next successful owner, and is never used
  to decide whether a lock may be broken (no PID-liveness or PID-reuse
  heuristics).
* **Lock files live in a registry directory, not in the artifact root.**
  Taking ownership therefore never creates or modifies anything under the
  protected directory (a refused run leaves it byte-for-byte untouched), and
  ancestors that a process may not write to (``/``, ``/home``) can still be
  part of the exclusion policy. The registry is ``$KLT_LOCK_DIR`` when set,
  else ``/tmp/klt-locks-<uid>``. Processes only exclude each other when they
  resolve the same registry directory on the same host.
* **Identity is the canonical path.** Relative paths, ``..`` and symlink
  aliases are resolved with :func:`os.path.realpath` before hashing, so every
  alias contends on the same lock. (Case-insensitive filesystems are not
  folded: two spellings that differ only in case are distinct identities.)
* **Ancestor/descendant policy.** Owning a root takes an *exclusive* lock on
  that root's lock file and a *shared* lock on the lock file of every
  ancestor directory. Hence: two runs on the same root exclude each other; a
  run on ``/w/pex`` excludes a run on ``/w/pex/x/schematic`` and vice versa;
  two runs on distinct descendants of a common ancestor, or on unrelated
  roots, proceed concurrently. Acquisition is non-blocking throughout, so it
  cannot deadlock; on a partial failure every lock taken so far is released.
  A thread that already owns a root (or an ancestor of it) re-enters for free,
  which is how ``run_pex`` hands its ownership to the ``run_sim`` calls it
  makes on ``<outdir>/<testbench>/schematic|extracted`` without
  self-contention. A thread that owns a *descendant* and then asks for an
  *ancestor* is refused like any other contender; acquire outermost first.
* **Scope.** Cooperating ``klt`` writers on one host sharing one registry
  directory, on a filesystem where ``flock`` works. Arbitrary other tools and
  distributed remote writers are not covered, and no cross-host lock service
  exists. Where ``fcntl.flock`` is unavailable (Windows) or the registry
  cannot provide locking, :class:`OutputLockUnavailableError` is raised
  rather than silently running unprotected. The lock file descriptors are
  close-on-exec, but a ``fork()`` without ``exec`` shares them: forked
  workers that outlive a killed owner keep the lock until they exit.
"""

from __future__ import annotations

import contextlib
import datetime
import errno
import functools
import hashlib
import json
import os
import threading
from collections.abc import Iterator
from typing import Any

try:  # pragma: no cover - exercised implicitly per platform
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

#: Environment variable overriding the lock registry directory.
LOCK_DIR_ENV = "KLT_LOCK_DIR"

#: Environment variable a parent run exports to a child process it launches and
#: blocks on (``klt pex --measure-command``): the canonical root the parent
#: owns. A child ``hold_output_dir`` for that root or a descendant is covered
#: by the parent's lock (#3291). Additive; never an ancestor or sibling.
INHERIT_ENV = "KLT_OUTDIR_LOCK_INHERIT"

#: ``error.code`` of the CLI envelope for contention.
CODE_LOCKED = "outdir_locked"
#: ``error.code`` of the CLI envelope when locking cannot be provided at all
#: (permissions, unsupported platform/filesystem) -- deliberately distinct
#: from contention so a caller does not retry something that cannot succeed.
CODE_UNAVAILABLE = "outdir_lock_unavailable"

_METADATA_VERSION = 1
_METADATA_READ_LIMIT = 4096


class OutputLockError(Exception):
    """Base class for output-directory ownership failures."""

    code = CODE_UNAVAILABLE


class OutputDirLockedError(OutputLockError):
    """Another cooperating run owns the output directory (or a related one).

    ``directory`` is the canonical directory this run asked for;
    ``locked_directory`` the directory whose lock actually refused it (an
    ancestor, or the same directory). ``owner_pid`` / ``owner_acquired_at``
    are best-effort diagnostics and ``None`` when the owner record is
    missing, malformed, not yet written, or the blocker holds only a shared
    (descendant) claim and so wrote none.
    """

    code = CODE_LOCKED

    def __init__(
        self,
        directory: str,
        *,
        locked_directory: str | None = None,
        owner_pid: int | None = None,
        owner_acquired_at: str | None = None,
        relation: str = "same",
    ) -> None:
        self.directory = directory
        self.locked_directory = locked_directory or directory
        self.owner_pid = owner_pid
        self.owner_acquired_at = owner_acquired_at
        self.relation = relation
        super().__init__(self._message())

    def _message(self) -> str:
        if self.relation == "ancestor":
            where = (
                f"output directory '{self.directory}' is inside "
                f"'{self.locked_directory}', which is owned by another run"
            )
        elif self.relation == "descendant":
            where = (
                f"output directory '{self.directory}' contains a "
                "directory owned by another run"
            )
        else:
            where = f"output directory '{self.directory}' is owned by another run"
        if self.owner_pid is not None:
            when = f" since {self.owner_acquired_at}" if self.owner_acquired_at else ""
            who = f" (pid {self.owner_pid}{when})"
        else:
            who = " (owner unknown)"
        return f"{where}{who}; wait for it to finish and retry"


class OutputLockUnavailableError(OutputLockError):
    """Ownership could not be established for a reason other than contention."""

    code = CODE_UNAVAILABLE


def canonical_dir(path: str | os.PathLike[str]) -> str:
    """Return the identity of ``path``: absolute, ``..``/symlinks resolved.

    Works for a directory that does not exist yet (the unresolvable tail is
    kept verbatim), so ownership can be taken before the first ``mkdir``.
    """
    return os.path.realpath(os.path.abspath(os.fspath(path)))


def _registry_dir() -> str:
    explicit = os.environ.get(LOCK_DIR_ENV)
    if explicit:
        return os.path.abspath(explicit)
    uid = os.getuid() if hasattr(os, "getuid") else 0
    base = "/tmp" if os.path.isdir("/tmp") else _tempdir()  # noqa: S108 - shared well-known lock registry; dir is per-uid (klt-locks-<uid>) and created by this module
    return os.path.join(base, f"klt-locks-{uid}")


def _tempdir() -> str:
    import tempfile

    return tempfile.gettempdir()


def _lock_file_for(registry: str, canonical: str) -> str:
    digest = hashlib.sha256(canonical.encode("utf-8", "surrogateescape")).hexdigest()
    return os.path.join(registry, digest[:40] + ".lock")


def _ancestors(canonical: str) -> list[str]:
    """Strict ancestors of ``canonical``, outermost first."""
    parts: list[str] = []
    current = canonical
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            break
        parts.append(parent)
        current = parent
    parts.reverse()
    return parts


_local = threading.local()


def _held_roots() -> list[str]:
    roots = getattr(_local, "roots", None)
    if roots is None:
        roots = []
        _local.roots = roots
    return roots


def _covered(canonical: str) -> bool:
    for held in _held_roots():
        if canonical == held or canonical.startswith(held.rstrip(os.sep) + os.sep):
            return True
    return False


def _inherited(canonical: str) -> bool:
    """True when ``canonical`` is at/under the root a parent run handed down."""
    root = os.environ.get(INHERIT_ENV)
    if not root or not os.path.isabs(root):
        return False
    root = canonical_dir(root)
    return canonical == root or canonical.startswith(root.rstrip(os.sep) + os.sep)


def _open_lock_file(registry: str, canonical: str) -> int:
    try:
        os.makedirs(registry, mode=0o700, exist_ok=True)
        return os.open(
            _lock_file_for(registry, canonical), os.O_RDWR | os.O_CREAT, 0o600
        )
    except OSError as exc:
        raise OutputLockUnavailableError(
            f"cannot open output-directory lock file for '{canonical}' in "
            f"'{registry}': {exc.strerror or exc} (set ${LOCK_DIR_ENV} to a "
            "writable directory)"
        ) from exc


def _try_flock(fd: int, op: int, canonical: str) -> bool:
    """Non-blocking ``flock``; ``False`` on contention, raise otherwise."""
    assert fcntl is not None
    try:
        fcntl.flock(fd, op | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            return False
        raise OutputLockUnavailableError(
            f"cannot lock output directory '{canonical}': {exc.strerror or exc} "
            "(the lock registry's filesystem may not support flock)"
        ) from exc


def _read_owner(fd: int) -> tuple[int | None, str | None]:
    """Best-effort read of the owner record; never raises."""
    try:
        raw = os.pread(fd, _METADATA_READ_LIMIT, 0)
        data = json.loads(raw.decode("utf-8"))
        pid = data.get("pid")
        acquired = data.get("acquired_at")
        if not isinstance(pid, int) or isinstance(pid, bool):
            pid = None
        if not isinstance(acquired, str):
            acquired = None
        return pid, acquired
    except Exception:  # noqa: BLE001 - diagnostics must never mask contention
        return None, None


def _write_owner(fd: int, canonical: str) -> None:
    """Replace the owner record; failure only loses diagnostics."""
    record: dict[str, Any] = {
        "version": _METADATA_VERSION,
        "pid": os.getpid(),
        "acquired_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "directory": canonical,
    }
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(record).encode("utf-8"), 0)
    except OSError:
        pass


def _contention_error(
    fd: int, canonical: str, locked: str, relation: str
) -> OutputDirLockedError:
    """Build the error for a refused ``locked`` lock file open as ``fd``.

    The owner record is only trusted when a *shared* probe also fails, i.e. an
    exclusive owner (who wrote the record) is the blocker. If the shared
    probe succeeds the blocker holds only shared (descendant) claims and any
    record in the file belongs to a long-gone previous owner.
    """
    pid: int | None = None
    acquired: str | None = None
    try:
        assert fcntl is not None
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                pid, acquired = _read_owner(fd)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            if relation == "same":
                relation = "descendant"
    except OSError:
        pass
    return OutputDirLockedError(
        canonical,
        locked_directory=locked,
        owner_pid=pid,
        owner_acquired_at=acquired,
        relation=relation,
    )


@contextlib.contextmanager
def hold_output_dir(path: str | os.PathLike[str]) -> Iterator[str]:
    """Own the artifact root ``path`` for the duration of the ``with`` block.

    Yields the canonical directory. Raises :class:`OutputDirLockedError` at
    once (never blocks) if another run owns it, an ancestor, or a
    descendant, and :class:`OutputLockUnavailableError` if ownership cannot
    be provided at all. Nothing under ``path`` is created or modified.
    Re-entrant per thread: a thread already owning ``path`` or one of its
    ancestors gets a no-op, as is a child process whose parent exported
    ``$KLT_OUTDIR_LOCK_INHERIT`` for ``path`` or one of its ancestors (#3291).
    """
    canonical = canonical_dir(path)
    if _covered(canonical) or _inherited(canonical):
        yield canonical
        return
    if fcntl is None:
        raise OutputLockUnavailableError(
            "output-directory locking requires fcntl.flock, which this "
            "platform does not provide"
        )

    registry = _registry_dir()
    fds: list[int] = []
    try:
        # Outermost ancestor first: shared claims that make an enclosing
        # run's exclusive lock (and ours, if one exists) conflict.
        for ancestor in _ancestors(canonical):
            fd = _open_lock_file(registry, ancestor)
            fds.append(fd)
            if not _try_flock(fd, fcntl.LOCK_SH, ancestor):
                raise _contention_error(fd, canonical, ancestor, "ancestor")
        fd = _open_lock_file(registry, canonical)
        fds.append(fd)
        if not _try_flock(fd, fcntl.LOCK_EX, canonical):
            raise _contention_error(fd, canonical, canonical, "same")
        _write_owner(fd, canonical)
    except BaseException:
        for held in fds:
            os.close(held)
        raise

    roots = _held_roots()
    roots.append(canonical)
    try:
        yield canonical
    finally:
        roots.remove(canonical)
        # Closing releases the flock; the files are intentionally left in
        # place (unlinking would let two processes lock different inodes).
        for held in fds:
            os.close(held)


def owns_run(fn: Any) -> Any:
    """Decorator: give a public ``run_*`` entry point an ownership stack.

    The wrapped function takes a keyword-only ``_ownership``
    :class:`contextlib.ExitStack`. A top-level call (``_ownership`` omitted)
    gets a fresh stack that is closed -- releasing every lock entered on it,
    on success or exception -- when the call returns; an internal nested call
    passes its parent's stack through unchanged so ownership is inherited,
    never re-acquired. Kept out of the entry points so their (baselined)
    cyclomatic complexity does not grow for each integration site.
    """

    @functools.wraps(fn)
    def wrapper(
        *args: Any, _ownership: contextlib.ExitStack | None = None, **kwargs: Any
    ) -> Any:
        if _ownership is not None:
            return fn(*args, _ownership=_ownership, **kwargs)
        with contextlib.ExitStack() as stack:
            return fn(*args, _ownership=stack, **kwargs)

    return wrapper
