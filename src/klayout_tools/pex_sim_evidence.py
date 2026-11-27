"""Per-leg `klt sim` evidence projected into the ``klt pex`` report.

Split out of ``pex.py`` (file-size ratchet, #2766): the ``testbenches[].sides``
execution evidence (issues #2872 / #2876) and the ``unfinished[]`` shard-loss
entries (issue #5282). Both transport ``klt sim``'s own response unchanged.
Also home to the ``delta[]`` row-label fields (:func:`delta_labels`, issue
#5681), kept here so ``pex.py`` does not grow.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from . import sim_cause
from .pex_errors import PexError

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


def delta_labels(
    testbench: str | None, spec_row_prefix: str | None, name: str
) -> dict[str, Any]:
    """Issue #5681: the identity fields every ``delta[]`` row leads with.

    ``spec_row`` is the display label -- ``<stem>.<name>`` only when the run
    had several testbenches (``spec_row_prefix`` set), else the bare name -- so
    it depends on invocation shape. ``testbench`` (the request's filename
    stem; ``None`` in ``--measure-command`` mode, which has no testbench file)
    and ``measurement`` (the bare ``measurements[].name``) do not, so
    ``(testbench, measurement)`` is a stable key for a row.
    """
    return {
        "spec_row": name if spec_row_prefix is None else f"{spec_row_prefix}.{name}",
        "testbench": testbench,
        "measurement": name,
    }


def _selected_probes(group: dict[str, Any], prefix: str, *, omit: bool) -> list[str]:
    specs = group.get("measurements")
    if not isinstance(specs, list):
        return []  # sim validates the container itself
    selected = []
    portable = []
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict) or "sides" not in spec:
            portable.append(spec)
            continue
        name = spec.get("name")
        if spec["sides"] != "schematic" or not isinstance(name, str) or not name:
            raise PexError(
                f"measurement {name!r} (measurements[{index}].sides) needs a "
                "name and the side selection 'schematic'"
            )
        if spec.get("intermediate"):
            raise PexError(
                f"measurement {name!r} (measurements[{index}]) is 'intermediate' "
                "(never reported or graded), so it cannot declare 'sides'"
            )
        selected.append(prefix + name)
    if omit and len(portable) != len(specs):
        group["measurements"] = portable
    return selected


def schematic_probes(request: dict[str, Any], *, omit: bool = False) -> tuple[str, ...]:
    """List schematic probes and optionally remove them from a leg request copy."""
    selected = []
    steps = request.get("analysis_steps")
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict):
                selected.extend(
                    _selected_probes(step, f"{step.get('name')}.", omit=omit)
                )
    selected.extend(_selected_probes(request, "", omit=omit))
    return tuple(selected)


def schematic_probe_rows(
    names: tuple[str, ...],
    schematic: dict[str, Any],
    extracted: dict[str, Any] | None,
    *,
    testbench: str,
    prefix: str | None,
    device_only_leg: bool,
) -> list[dict[str, Any]]:
    """Record the requested schematic observation without grading an extracted value."""
    from .pex import _device_only_fields, _index_corner_measurements, _side_error

    if not names:
        return []
    by_corner = _index_corner_measurements(schematic)
    corners = dict.fromkeys(
        [*by_corner, *(c["corner_id"] for c in (extracted or {}).get("corners", []))]
    )
    rows = []
    for corner_id in corners:
        for name in names:
            measurement = by_corner.get(corner_id, {}).get(name)
            status = measurement["status"] if measurement else "error"
            value = measurement["value"] if measurement else None
            row = {
                **delta_labels(testbench, prefix, name),
                "corner_id": corner_id,
                "schematic_value": value,
                "schematic_status": status,
                "extracted_value": None,
                "extracted_status": "skipped",
                "delta_pct": None,
                "status": "error" if status == "error" else "skipped",
            }
            if status == "error":
                row["error"] = _side_error(
                    "schematic", schematic, corner_id, measurement=measurement
                )
            if device_only_leg:
                row.update(
                    _device_only_fields(value, None, None), device_only_status="skipped"
                )
            rows.append(row)
    return rows


def explaining_diagnostic(
    diagnostics: Sequence[Mapping[str, Any]],
    measurement: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Choose this row’s diagnostic, preserving the first error on a solver abort."""
    errors = [d for d in diagnostics if d.get("severity") == "error"]
    if any(
        d.get("code") not in ("measurement", "singular_matrix", "nonconvergence")
        for d in errors
    ):
        return errors[0]

    # An optional value missing on just one side is a PEX error. Its own
    # klt sim diagnostic is informational, even beside a required window miss.
    if measurement is not None and measurement.get("status") == "absent":
        prefix = f"optional measurement '{measurement['name']}' "
        for diagnostic in diagnostics:
            if diagnostic.get("code") == "measurement_absent" and str(
                diagnostic.get("message") or ""
            ).startswith(prefix):
                return diagnostic
    if not errors:
        return diagnostics[0] if diagnostics else None

    # An engine-confirmed window miss can explain a missing value beside
    # recovered stepping narration. Each .meas card has its own diagnostic.
    measurements = [d for d in errors if d.get("code") == "measurement"]
    window_miss = next(
        (
            d
            for d in measurements
            if sim_cause.reports_window_miss(str(d.get("message") or ""))
        ),
        None,
    )
    if window_miss is None:
        return errors[0]
    if measurement is not None and measurement.get("name"):
        prefix = f"measurement '{measurement['name']}' "
        for diagnostic in measurements:
            if str(diagnostic.get("message") or "").startswith(prefix):
                return diagnostic
    return window_miss
