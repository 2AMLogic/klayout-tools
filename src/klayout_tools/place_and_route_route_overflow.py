"""Route-stage global-route overflow regions (issue #5313).

Kept out of ``place_and_route.py`` / ``place_and_route_script.py`` (per-file
size ratchet): resolves the ``-congestion_report_file`` path, builds the macro
boxes congestion regions are attributed to, and parses the report. Issue
#6952 adds the failure-path assembly: the congestion evidence a route stage
that exits nonzero leaves behind, exposed as ``PlaceAndRouteError.details``.
"""

from __future__ import annotations

import os
from typing import Any

from .place_and_route_macro_halo import merge_macro_margins
from .place_and_route_reports import (
    build_congestion,
    parse_congestion_report,
    parse_global_route_congestion,
)
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


#: Why a report file that *is* on disk still yields no regions or path: it was
#: written by an earlier attempt into a reused output directory, and only this
#: attempt's evidence is ever named (issue #6952's test plan).
_STALE_REPORT_REASON = (
    "the report file on disk predates this attempt's route invocation "
    "(reused output directory); only this attempt's report is evidence"
)
_NO_REPORT_REASON = (
    "the route exited before `global_route -congestion_report_file` wrote a report"
)


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

    ``completed`` is the stage's ``_OpenRoadResult`` (duck-typed: the
    captured ``stdout`` plus the retained ``engine_log`` record -- typed
    ``Any`` because the engine module's result type is private to its own
    import-boundary group).

    The two evidence sources are independent, and each is ``null`` with a
    field-specific reason in ``null_reasons`` when this attempt did not
    produce it while the other's data is still returned: the ``GRT-0096``
    per-layer table parsed from this attempt's captured stdout, and the
    worst overflow regions from the ``-congestion_report_file`` report this
    stage's own script writes -- whose raw location is exposed alongside as
    ``congestion_report_path``. A report file that predates the invocation
    (``engine_log["started_at"]``, retained by the engine wrapper) is not
    evidence and is never named. Field names match the success response's
    ``congestion.global_route`` block by construction -- ``build_congestion``
    assembles both -- so a caller diffing a failed attempt against a
    successful one reads the same shape.
    """
    if stage != "route":
        return None
    report_path = congestion_report_path(output_dir, hdl_toplevel)
    started_at = (completed.engine_log or {}).get("started_at")
    produced = os.path.exists(report_path) and (
        started_at is None or os.path.getmtime(report_path) >= started_at
    )
    stale = not produced and os.path.exists(report_path)
    block = build_congestion(
        global_route=parse_global_route_congestion(completed.stdout or ""),
        completeness=None,
        placement={},
        detailed={},
        overflow_regions=(
            grt_overflow(output_dir, hdl_toplevel, macros) if produced else None
        ),
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
        reason = _STALE_REPORT_REASON if stale else _NO_REPORT_REASON
        reasons["global_route.congestion_report_path"] = reason
        if stale:
            reasons["global_route.overflow_regions"] = reason
    return {"congestion": {"global_route": global_route, "null_reasons": reasons}}
