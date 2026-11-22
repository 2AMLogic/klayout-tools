"""Route-stage global-route overflow regions (issues #5313, #6949).

Kept out of ``place_and_route.py`` / ``place_and_route_script.py`` (per-file
size ratchet): resolves the ``-congestion_report_file`` path, builds the macro
boxes congestion regions are attributed to, and parses the report.
"""

from __future__ import annotations

import os
from typing import Any

from .place_and_route_macro_halo import merge_macro_margins
from .place_and_route_reports import (
    parse_congestion_report_records,
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
