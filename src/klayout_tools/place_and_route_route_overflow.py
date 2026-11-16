"""Route-stage global-route overflow regions (issue #5313).

Kept out of ``place_and_route.py`` / ``place_and_route_script.py`` (per-file
size ratchet): resolves the ``-congestion_report_file`` path, builds the macro
boxes congestion regions are attributed to, and parses the report.
"""

from __future__ import annotations

import os
from typing import Any

from .place_and_route_macro_halo import merge_macro_margins
from .place_and_route_reports import parse_congestion_report
from .place_and_route_script import _macro_footprint


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


def grt_overflow(
    output_dir: str, hdl_toplevel: str, macros: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Worst overflow regions of this run's congestion report (or ``None``)."""
    return parse_congestion_report(
        congestion_report_path(output_dir, hdl_toplevel), _macro_boxes(macros)
    )
