"""``klt economy`` argument parser (split out of ``parser.py`` for the size ratchet)."""

import argparse
from collections.abc import Callable
from typing import Any

from . import economy_cmd


def add_economy_parser(
    subparsers: argparse._SubParsersAction,
    _add_format_arg: Callable[..., Any],
) -> None:
    """Register the ``economy`` verb: quantitative layout-density report (#1012)."""
    economy_parser = subparsers.add_parser(
        "economy",
        help="quantitative layout-density report (issue #1012)",
        description=(
            "Report utilization (per cell and for the top), a whitespace "
            "map (grid + largest exact empty regions), bounding-box "
            "tightness/aspect-ratio/dead-margins, and optional "
            "area-budget/reference-area and AREA-EFF (issue #1086) bounds "
            "checks for one top cell of a GDSII or OASIS layout stream."
        ),
    )
    economy_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    economy_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to report on when the stream has more than one "
            "(required in that case; optional otherwise)"
        ),
    )
    economy_parser.add_argument(
        "--grid-cols",
        type=int,
        default=4,
        dest="grid_cols",
        help="whitespace-grid column count (default: 4)",
    )
    economy_parser.add_argument(
        "--grid-rows",
        type=int,
        default=4,
        dest="grid_rows",
        help="whitespace-grid row count (default: 4)",
    )
    economy_parser.add_argument(
        "--max-empty-regions",
        type=int,
        default=10,
        dest="max_empty_regions",
        help="cap on how many largest empty regions to report (default: 10)",
    )
    economy_parser.add_argument(
        "--budget-um2",
        type=float,
        default=None,
        dest="budget_um2",
        help=(
            "area budget in square micrometres; when given, reports a "
            "PASS/FAIL 'budget' block against the top cell's bbox area"
        ),
    )
    economy_parser.add_argument(
        "--reference-area-um2",
        type=float,
        default=None,
        dest="reference_area_um2",
        help=(
            "a comparable hand-designed reference's area in square "
            "micrometres; when given, reports a 'reference' block with the "
            "ratio to the top cell's bbox area"
        ),
    )
    economy_parser.add_argument(
        "--area-eff-max-dead-margin-um",
        type=float,
        default=None,
        dest="area_eff_max_dead_margin_um",
        help=(
            "AREA-EFF hard bound (issue #1086): cap in micrometres on any "
            "single edge of dead_margins_um; when given, adds an 'area_eff' "
            "block with a 'dead_margins' PASS/FAIL check"
        ),
    )
    economy_parser.add_argument(
        "--area-eff-min-utilization",
        type=float,
        default=None,
        dest="area_eff_min_utilization",
        help=(
            "AREA-EFF calibrated bound (issue #1086): per-block-kind "
            "utilization floor (see the economy-review skill's rubric "
            "table); when given, adds an 'area_eff' block with a "
            "'utilization' PASS/FAIL check"
        ),
    )
    economy_parser.add_argument(
        "--area-eff-max-empty-region-fraction",
        type=float,
        default=None,
        dest="area_eff_max_empty_region_fraction",
        help=(
            "AREA-EFF hard bound (issue #1086): cap on the largest single "
            "empty region's area as a fraction of the top cell's bbox "
            "area; when given, adds an 'area_eff' block with a "
            "'largest_empty_region_fraction' PASS/FAIL check"
        ),
    )
    economy_parser.add_argument(
        "--area-eff-require-bbox-tightness",
        action="store_true",
        dest="area_eff_require_bbox_tightness",
        help=(
            "AREA-EFF hard bound (issue #1086): require bbox_tightness == "
            "1.0; when given, adds an 'area_eff' block with a "
            "'bbox_tightness' PASS/FAIL check"
        ),
    )
    economy_parser.add_argument(
        "--what-if",
        default=None,
        dest="what_if",
        metavar="MOVES_JSON",
        help=(
            "what-if floorplan estimate (issue #4661): apply instance moves "
            "and cell reshapes from MOVES_JSON to an in-memory copy and "
            "report the same figures plus a 'what_if' block (overlaps, "
            "broken contacts); writes nothing, runs no DRC, legality is "
            "never checked"
        ),
    )
    economy_parser.add_argument(
        "--attribute",
        action="store_true",
        help=(
            "add an 'attribution' block partitioning the top cell's bbox "
            "into direct child instance bboxes and labelled gap components "
            "(issue #4660)"
        ),
    )
    economy_parser.add_argument(
        "--flatten-wrappers",
        action="store_true",
        help=(
            "with --attribute, expand wrapper children (cells holding two or "
            "more instances) into leaf placements named by hierarchical path "
            "(issue #5179)"
        ),
    )
    _add_format_arg(economy_parser)
    economy_cmd.register(economy_parser)
