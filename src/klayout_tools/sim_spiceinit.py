"""Effective ngspice init-file provenance for ``klt sim`` (issue #5648).

ngspice reads one ``.spiceinit`` -- the run directory's, else
``$HOME/.spiceinit`` -- and a run-directory file *replaces* the home one
rather than merging with it. Which file was in effect changes results (e.g.
``set wnflag=1`` decides whether ``W`` is total or per-finger width), yet it
was the one input that changes numbers and was absent from the report.

:func:`effective_init` reports what ngspice will actually read, as
``environment.ngspice_init``: ``{source, lines, sha256}``. ``source`` is a
path-free label on purpose -- the block is committed evidence and an absolute
``$HOME`` path names the resolving machine's home directory (the same rule
``provenance.pdk.ambiguous_sources`` follows). The path is named only in the
stderr warnings from :func:`warn`.
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import Any

#: ``source`` values. ``runner`` is for off-host backends: the init file is
#: resolved by the worker, which this client cannot observe.
SOURCES = ("request", "thread_pin", "home", "none", "runner")


def home_spiceinit_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".spiceinit")


def _read_home() -> bytes | None:
    """Bytes of a readable ``$HOME/.spiceinit``; ``None`` when absent or
    unreadable (ngspice reads nothing from a file it cannot open)."""
    try:
        with open(home_spiceinit_path(), "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def with_thread_pin(
    ngspice_init: tuple[str, ...], threads_per_worker: int | None, engine: str
) -> tuple[str, ...]:
    """Also pin the thread count through ``.spiceinit`` (issue #3619).

    ``OMP_NUM_THREADS`` alone did not stop ngspice-46 from oversubscribing
    on the batch fleet, while ``set num_threads=1`` did. The pin line goes
    first so a user ``options.ngspice_init`` line still wins; a netlist
    ``.control`` ``set num_threads`` runs later still.
    """
    if threads_per_worker is None or engine == "xyce":
        return ngspice_init
    return (f"set num_threads={threads_per_worker}", *ngspice_init)


def effective_init(
    declared: tuple[str, ...],
    written: tuple[str, ...],
    *,
    engine: str,
    offhost: bool,
) -> dict[str, Any] | None:
    """The ``environment.ngspice_init`` block.

    ``declared`` is the validated ``options.ngspice_init``; ``written`` is
    what klt writes to the run directory's ``.spiceinit`` (declared lines
    plus the #3619 thread pin) -- ``source`` follows ``written``, never
    ``declared``, so a declared option that reached no file is not reported.
    ``None`` for Xyce, which has no init-file lookup.
    """
    if engine == "xyce":
        return None
    if offhost:
        return {"source": "runner", "lines": None, "sha256": None}
    if written:
        data = "".join(line + "\n" for line in written).encode("utf-8")
        return {
            "source": "request" if declared else "thread_pin",
            "lines": list(written),
            "sha256": _digest(data),
        }
    home = _read_home()
    if home is None:
        return {"source": "none", "lines": None, "sha256": None}
    return {"source": "home", "lines": None, "sha256": _digest(home)}


def warning_for(block: dict[str, Any] | None, *, declared: bool) -> str | None:
    """The stderr warning for ``block``, or ``None`` when no
    ``$HOME/.spiceinit`` is in play (the ordinary case)."""
    if not block or block["source"] in ("runner", "none"):
        return None
    path = home_spiceinit_path()
    if block["source"] == "home":
        return (
            f"klt: warning: this run reads the ambient init file {path} "
            "(options.ngspice_init is not declared), so its results depend "
            "on this host's settings. Declare the needed lines in "
            "options.ngspice_init to make the request self-contained."
        )
    if _read_home() is None:
        return None
    return (
        f"klt: warning: klt wrote its own .spiceinit for this run, which "
        f"replaces (does not merge with) the ambient init file {path}; "
        "settings only that file holds were not applied. Declare the needed "
        "lines in options.ngspice_init."
    )


def warn(block: dict[str, Any] | None, *, declared: bool) -> None:
    message = warning_for(block, declared=declared)
    if message is not None:
        print(message, file=sys.stderr)


def record(
    declared: tuple[str, ...],
    threads_per_worker: int | None,
    engine: str,
    offhost: bool,
) -> dict[str, Any] | None:
    """Build ``environment.ngspice_init`` for a run and emit its warning."""
    block = effective_init(
        declared,
        with_thread_pin(declared, threads_per_worker, engine),
        engine=engine,
        offhost=offhost,
    )
    warn(block, declared=bool(declared))
    return block
