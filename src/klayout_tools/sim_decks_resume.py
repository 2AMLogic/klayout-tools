"""Deck-run resume: skip units whose recorded completion still verifies
(issue #3957).

Split out of `sim_decks.py` when that module reached its per-file size
ceiling (scripts/check_file_size_baseline.py, issue #2766); the resume
lookup, the fold-back into a partial report, and the backend gate live
here. Unit completion records (`unit.json`) are written by
`sim_decks.run_deck_unit` and read back by `_find_resumable`.
"""

from __future__ import annotations

import os
from typing import Any


def _find_resumable(
    units: list[dict[str, Any]],
    *,
    staging_root: str,
    outdir: str,
    engine_version: str | None,
) -> dict[int, dict[str, Any]]:
    """Index -> recorded result for every unit already complete under
    ``outdir``."""
    from . import sim_decks

    resumed: dict[int, dict[str, Any]] = {}
    for index, unit in enumerate(units):
        key = {
            **sim_decks._unit_input_key(unit, staging_root=staging_root),
            "engine_version": engine_version,
        }
        done = sim_decks._load_complete_unit(
            unit, unit_dir=os.path.join(outdir, unit["id"]), key=key
        )
        if done is not None:
            resumed[index] = done
    return resumed


def _apply_resume(
    report: dict[str, Any],
    units: list[dict[str, Any]],
    resumed: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Fold skipped units into a report that covers only the units that ran:
    request-order ``units`` (``resumed: false`` on the ones that ran),
    recomputed counts, and the additive ``environment.resume`` block."""
    ran = {item["id"]: item for item in report["units"]}
    merged = []
    for index, unit in enumerate(units):
        if index in resumed:
            merged.append(resumed[index])
        elif unit["id"] in ran:
            merged.append({**ran[unit["id"]], "resumed": False})
    report["units"] = merged
    _recount(report)
    environment = report.setdefault("environment", {})
    if not environment.get("engine_version"):
        environment["engine_version"] = next(
            (item["engine_version"] for item in merged if item["engine_version"]),
            None,
        )
    environment["resume"] = {
        "resumed_units": len(resumed),
        "resumed_ids": [units[index]["id"] for index in sorted(resumed)],
    }
    return report


def _recount(report: dict[str, Any]) -> None:
    filled = report["units"]
    errored = sum(1 for item in filled if item["status"] == "error")
    timed_out = sum(1 for item in filled if item["status"] == "timeout")
    report.update(
        {
            "status": "error" if (errored or timed_out) else "pass",
            "unit_count": len(filled),
            "passed": sum(1 for item in filled if item["status"] == "pass"),
            "errored": errored,
            "timed_out": timed_out,
        }
    )


def _resolve_resumed(
    units: list[dict[str, Any]],
    *,
    backend: str,
    request_dir: str,
    outdir: str,
    ngspice_binary: str,
) -> dict[int, dict[str, Any]]:
    """The ``--resume`` lookup of ``sim_decks.run_deck_request``: unit
    index -> completion record for every unit that can be skipped."""
    from .sim import SimError, _probe_engine_version

    if backend == "remote":
        raise SimError(
            "options.resume is not yet supported for backend 'remote' -- "
            "use resume with backend 'local', 'local-parallel' or 'batch'"
        )
    return _find_resumable(
        units,
        staging_root=request_dir,
        outdir=outdir,
        # The fleet's engine cannot be probed before submit; its units
        # are matched on everything else (see docs/cli/sim.md).
        engine_version=(
            None
            if backend == "batch"
            else _probe_engine_version("ngspice", ngspice_binary)
        ),
    )
