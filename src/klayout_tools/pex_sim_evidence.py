"""Per-leg `klt sim` evidence projected into the ``klt pex`` report.

Split out of ``pex.py`` (file-size ratchet, #2766): the ``testbenches[].sides``
execution evidence (issues #2872 / #2876) and the ``unfinished[]`` shard-loss
entries (issue #5282). Both transport ``klt sim``'s own response unchanged.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

# Only keys `klt sim` actually emits in its `environment` (see sim.py's
# `run_sim`): which engine/build ran, and -- for an off-host backend that
# fills it -- the `remote` block carrying the job identity.
SIDE_ENVIRONMENT_KEYS = (
    "engine",
    "engine_version",
    "ngspice_binary",
    "engine_host",
    "client_ngspice_binary",
    "remote",
)


def project_side_evidence(
    sim_report: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Issue #2872 / #2876: the per-side execution evidence of one `klt sim`
    response, transported unchanged (diagnostics are never re-classified or
    filtered here -- `sim.py` owns that vocabulary). ``None`` when that side
    was never simulated. ``produced_value`` is derived from the source
    corner's own measurements (any non-null ``value``), never from
    ``delta[]``. Keyed by ``corner_id`` within its testbench entry."""
    if sim_report is None:
        return None
    environment = sim_report.get("environment") or {}
    corners = []
    for corner in sim_report.get("corners", []):
        corners.append(
            {
                "corner_id": corner["corner_id"],
                "status": corner.get("status"),
                "diagnostics": copy.deepcopy(corner.get("diagnostics") or []),
                "produced_value": any(
                    m.get("value") is not None for m in corner.get("measurements", [])
                ),
            }
        )
    return {
        "environment": {
            key: copy.deepcopy(environment[key])
            for key in SIDE_ENVIRONMENT_KEYS
            if key in environment
        },
        "corners": corners,
    }


def project_testbench_sides(
    schematic_report: Mapping[str, Any], extracted_report: Mapping[str, Any] | None
) -> dict[str, Any]:
    """``sides`` block of one ``testbenches[]`` entry, plus
    ``nothing_simulated_corners``: corner ids where *neither* side produced
    any non-null measurement (an execution failure), as opposed to a
    one-sided failure (a real pex finding). Requires both sides to have run."""
    schematic = project_side_evidence(schematic_report)
    extracted = project_side_evidence(extracted_report)
    nothing: list[str] = []
    if schematic is not None and extracted is not None:
        produced = {c["corner_id"]: c["produced_value"] for c in schematic["corners"]}
        for corner in extracted["corners"]:
            cid = corner["corner_id"]
            if not corner["produced_value"] and not produced.get(cid, False):
                nothing.append(cid)
    return {
        "sides": {"schematic": schematic, "extracted": extracted},
        "nothing_simulated_corners": nothing,
    }


def unfinished_leg(
    testbench: str, side: str, sim_report: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Issue #5282: one ``unfinished[]`` entry for a sim leg that lost shards
    (``lost_shard`` corners and/or ``environment.remote.unfinished_shards``),
    else ``None``. The ``resumable`` / ``shard_map`` / ``unfinished_shards``
    data comes from `klt sim`'s own environment, transported unchanged."""
    if sim_report is None:
        return None
    environment = sim_report.get("environment") or {}
    remote = environment.get("remote") or {}

    def _find(key: str) -> Any:
        return remote.get(key, environment.get(key))

    lost = [
        corner["corner_id"]
        for corner in sim_report.get("corners", [])
        if any(d.get("code") == "lost_shard" for d in corner.get("diagnostics") or [])
    ]
    shards = copy.deepcopy(_find("unfinished_shards") or [])
    if not lost and not shards:
        return None
    return {
        "testbench": testbench,
        "side": side,
        "corner_ids": lost,
        "unfinished_shards": shards,
        "resumable": bool(_find("resumable")),
        "shard_map": _find("shard_map"),
    }


def unfinished_legs(
    testbench: str,
    schematic_report: Mapping[str, Any] | None,
    extracted_report: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """``unfinished[]`` entries (schematic leg first) for one testbench."""
    legs = (("schematic", schematic_report), ("extracted", extracted_report))
    entries = (unfinished_leg(testbench, side, report) for side, report in legs)
    return [entry for entry in entries if entry is not None]
