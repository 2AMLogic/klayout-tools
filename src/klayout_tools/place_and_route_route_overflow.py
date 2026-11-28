"""Route-stage global-route overflow regions (issues #5313, #6949).

Kept out of ``place_and_route.py`` / ``place_and_route_script.py`` (per-file
size ratchet): resolves the ``-congestion_report_file`` path, builds the macro
boxes congestion regions are attributed to, and parses the report. Issue
#6952 adds the failure-path assembly: the congestion evidence a route stage
that exits nonzero leaves behind, exposed as ``PlaceAndRouteError.details``.
"""

from __future__ import annotations

import os
from typing import Any

from .place_and_route_base import PlaceAndRouteError
from .place_and_route_macro_halo import merge_macro_margins
from .place_and_route_reports import (
    build_congestion,
    parse_congestion_report_records,
    parse_global_route_congestion,
    rank_overflow_regions,
)
from .place_and_route_script import _macro_footprint

#: Issue #6949: the region frame `overflow_by_region` always carries -- a
#: group with no record reads as all-zero counters, never as a missing key.
OVERFLOW_REGION_NAMES = ("inside_footprint", "keepout_band", "elsewhere")
_OVERFLOW_DIRECTIONS = ("horizontal", "vertical")
_OVERFLOW_GROUP_COUNTERS = (
    "overflow_sum",
    "violation_tile_count",
    "zero_capacity_tile_count",
    "zero_capacity_overflow_sum",
)


def congestion_report_path(output_dir: str, hdl_toplevel: str) -> str:
    """Where the route stage's ``global_route -congestion_report_file`` lands."""
    return os.path.join(output_dir, f"{hdl_toplevel}_route_congestion.rpt")


def _macro_boxes(macros: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Footprint + keep-out margin per macro, for attributing congestion
    regions to macro channels. Macros whose LEF has no ``SIZE`` are skipped
    (no footprint to test against)."""
    boxes: list[dict[str, Any]] = []
    for macro in macros:
        if macro["width_um"] is None or macro["height_um"] is None:
            continue
        margin = merge_macro_margins(macro["halo"], macro.get("keepout"))
        boxes.append(
            {
                "instance": macro["instance"],
                "footprint": _macro_footprint(macro),
                "margin": margin
                or {"left": 0.0, "right": 0.0, "top": 0.0, "bottom": 0.0},
            }
        )
    return boxes


def overflow_by_region(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Group every parsed congestion record by direction and region (issue
    #6949), using the GCell-centre attribution each record already carries
    (``inside_footprint`` wins over any ``keepout_band``; an unattributed
    record is ``elsewhere``). Each record lands in exactly one group -- a
    GCell shared by both directions contributes once per directional record
    -- so sums over all groups are the full-report totals, the denominator
    the worst-20 ``overflow_regions`` sample cannot provide. Zero-capacity
    tiles are counted apart: on them all tracked usage sits over a capacity
    of 0, the floorplan comparison's "how much of the overflow cannot be
    fixed by widening channels" share."""
    groups = {
        direction: {
            region: dict.fromkeys(_OVERFLOW_GROUP_COUNTERS, 0)
            for region in OVERFLOW_REGION_NAMES
        }
        for direction in _OVERFLOW_DIRECTIONS
    }
    for record in records:
        counters = groups[record["direction"]][record["where"] or "elsewhere"]
        counters["overflow_sum"] += record["overflow"]
        counters["violation_tile_count"] += 1
        if record["capacity"] == 0:
            counters["zero_capacity_tile_count"] += 1
            counters["zero_capacity_overflow_sum"] += record["overflow"]
    return groups


def grt_overflow(
    output_dir: str, hdl_toplevel: str, macros: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The route stage's congestion-report digest (issue #6949), from one
    read of the report: ``overflow_regions`` (the capped worst-N list) and
    ``overflow_by_region`` (aggregates over *every* record, computed before
    truncation). ``None`` when the report is absent, unreadable or holds no
    parsable violation."""
    records = parse_congestion_report_records(
        congestion_report_path(output_dir, hdl_toplevel), _macro_boxes(macros)
    )
    if records is None:
        return None
    return {
        "overflow_regions": rank_overflow_regions(records),
        "overflow_by_region": overflow_by_region(records),
    }


_NO_REPORT_REASON = (
    "the route exited before `global_route -congestion_report_file` wrote a report"
)


def clear_congestion_report(stage: str, output_dir: str, hdl_toplevel: str) -> None:
    """Remove any pre-existing ``*_route_congestion.rpt`` before the route
    stage launches OpenROAD, so a leftover report in a reused output directory
    can never be mistaken for this attempt's evidence (issue #6952)."""
    if stage != "route":
        return
    try:
        os.unlink(congestion_report_path(output_dir, hdl_toplevel))
    except FileNotFoundError:
        pass


def route_failure_congestion(
    stage: str,
    completed: Any,
    output_dir: str,
    hdl_toplevel: str,
    macros: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The congestion evidence a failed route stage left behind, as a
    ``PlaceAndRouteError.details`` payload (issue #6952); ``None`` for any
    other stage, whose error envelope stays byte-identical.

    ``completed`` is the stage's ``OpenRoadResult`` (duck-typed for
    ``stdout`` -- typed ``Any`` because the engine module's result type is
    private to its own import-boundary group).

    The two evidence sources are independent, and each is ``null`` with a
    field-specific reason in ``null_reasons`` when this attempt did not
    produce it while the other's data is still returned: the ``GRT-0096``
    per-layer table parsed from this attempt's captured stdout, and the
    worst overflow regions from the ``-congestion_report_file`` report this
    stage's own script writes -- whose raw location is exposed alongside as
    ``congestion_report_path``. :func:`clear_congestion_report` removes any
    leftover report from an earlier attempt before OpenROAD launches, so
    file presence on disk is this attempt's evidence without cross-clock
    timestamp comparisons. Field names match the success response's
    ``congestion.global_route`` block by construction -- ``build_congestion``
    assembles both -- so a caller diffing a failed attempt against a
    successful one reads the same shape.
    """
    if stage != "route":
        return None
    report_path = congestion_report_path(output_dir, hdl_toplevel)
    produced = os.path.isfile(report_path)
    block = build_congestion(
        global_route=parse_global_route_congestion(completed.stdout or ""),
        completeness=None,
        placement={},
        detailed={},
        overflow=(grt_overflow(output_dir, hdl_toplevel, macros) if produced else None),
    )
    global_route = block["global_route"]
    reasons = {
        key: reason
        for key, reason in block["null_reasons"].items()
        if key.startswith("global_route.")
    }
    if produced:
        global_route["congestion_report_path"] = report_path
    else:
        global_route["congestion_report_path"] = None
        reasons["global_route.congestion_report_path"] = _NO_REPORT_REASON
    return {"congestion": {"global_route": global_route, "null_reasons": reasons}}


def record_corner_failure(entry: dict[str, Any], exc: PlaceAndRouteError) -> None:
    """Attach failure evidence while preserving the corner's metadata."""
    entry.update(status="failed", error=str(exc))
    if exc.code is not None:
        entry["error_code"] = exc.code
    if exc.job_id is not None:
        entry["job_id"] = exc.job_id
    for key, value in (exc.details or {}).items():
        # Only the exception attributes can supply these optional fields.
        if key not in ("error_code", "job_id"):
            entry.setdefault(key, value)
