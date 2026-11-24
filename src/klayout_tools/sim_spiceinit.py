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
from collections.abc import Iterable
from typing import Any

from . import _warn
from .sim_batch_hints import is_model_selection_abort

#: ``source`` values. ``runner`` is for off-host backends: the init file is
#: resolved by the worker, which this client cannot observe.
SOURCES = ("request", "thread_pin", "home", "none", "runner")


BATCH_INIT_UNVERIFIED = "batch_ngspice_init_unverified"
MODEL_SELECTION_INIT_HINT = "ngspice_model_selection_init_hint"


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


def _lines_digest(lines: tuple[str, ...] | list[str]) -> str:
    return _digest("".join(line + "\n" for line in lines).encode("utf-8"))


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
        return {
            "source": "request" if declared else "thread_pin",
            "lines": list(written),
            "sha256": _lines_digest(written),
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
        _warn.warn(message)


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


def _batch_job_init_identity(member: dict[str, Any], declared: tuple[str, ...]) -> str:
    """Compare the worker's recorded request file with these effective init lines."""
    worker = member.get("worker_environment")
    if not isinstance(worker, dict):
        return "unverified"
    observed = worker.get("ngspice_init")
    if not isinstance(observed, dict) or observed.get("source") != "request":
        return "unverified"
    lines = observed.get("lines")
    pinned = with_thread_pin(declared, worker.get("threads_per_worker"), "ngspice")
    if lines in (list(declared), list(pinned)) and observed.get(
        "sha256"
    ) == _lines_digest(lines):
        return "digest_reported"
    return "unverified"


def batch_init_diagnostic(
    backend: str, engine: str, declared: tuple[str, ...], remote: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Qualify init evidence on corner-mode batch collection (issue #7368)."""
    if backend != "batch" or engine != "ngspice" or not declared:
        return None
    remote = remote or {}
    fleet = remote.get("fleet")
    members: list[tuple[int | None, Any]] = (
        list(enumerate(fleet))
        if isinstance(fleet, list) and fleet
        else [(None, remote)]
    )
    jobs = []
    for index, entry in members:
        member = entry if isinstance(entry, dict) else {}
        jobs.append(
            {
                "job_id": member.get("job_id"),
                "fleet_index": index,
                "request_identity": _batch_job_init_identity(member, declared),
            }
        )
    return {
        "code": BATCH_INIT_UNVERIFIED,
        "severity": "warning",
        "feature": "options.ngspice_init",
        "simulator_application": "unverified",
        "jobs": jobs,
        "message": (
            "Worker reports do not confirm that ngspice applied options.ngspice_init. "
            "Jobs marked digest_reported reported the requested init contents. "
            "For jobs marked unverified, the request identity is also unknown."
        ),
    }


def report_init_diagnostic(
    backend: str,
    engine: str,
    declared: tuple[str, ...],
    remote: dict[str, Any] | None,
    corners: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Explain a declared batch init or suggest one on a model-selection abort."""
    if declared:
        return batch_init_diagnostic(backend, engine, declared, remote)
    if engine != "ngspice" or backend not in (
        "local",
        "local-parallel",
        "remote",
        "batch",
    ):
        return None
    return _model_selection_hint(
        diagnostic
        for corner in corners
        if corner.get("status") == "error"
        for diagnostic in corner.get("diagnostics", [])
    )


def _model_selection_hint(
    diagnostics: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    if not any(is_model_selection_abort(diagnostic) for diagnostic in diagnostics):
        return None
    return {
        "code": MODEL_SELECTION_INIT_HINT,
        "severity": "warning",
        "feature": "options.ngspice_init",
        "message": (
            "ngspice could not select a model. If this library needs a "
            "compatibility mode, declare the required lines in options.ngspice_init "
            "(for example, set ngbehavior=hsa for an HSPICE-style library). "
            "See docs/cli/sim.md under ngspice's working directory and .spiceinit."
        ),
    }


def add_deck_init_hints(report: dict, declared: tuple[str, ...]) -> dict:
    """Qualify failed deck units, including units collected from older workers."""
    if declared:
        return report
    for unit in report.get("units", []):
        if unit.get("status") != "error":
            continue
        diagnostics = unit.get("diagnostics", [])
        if any(d.get("code") == MODEL_SELECTION_INIT_HINT for d in diagnostics):
            continue
        if hint := _model_selection_hint(diagnostics):
            diagnostics.append(hint)
    return report
