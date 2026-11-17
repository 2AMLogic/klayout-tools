"""``klt diff`` argument parser (split out of ``parser.py`` for the size ratchet)."""

import argparse
from collections.abc import Callable
from typing import Any

from . import diff_cmd


def add_diff_parser(
    subparsers: argparse._SubParsersAction,
    _add_format_arg: Callable[..., Any],
) -> None:
    """Register the ``diff`` verb: compare two layout streams for equal geometry."""
    diff_parser = subparsers.add_parser(
        "diff",
        help="compare two GDSII/OASIS streams for equal geometry, labels and hierarchy",
        description=(
            "Compare two layout streams shape-for-shape. Ignores stream "
            "timestamps, element order and library name; never ignores "
            "coordinates, path widths, text, instance transforms, the "
            "database unit or cell names. With --flattened, compares the "
            "instantiated geometry instead (per-layer XOR, drawn layer set, "
            "bounding box, labels) and ignores cell names and hierarchy. "
            "Exit 0 = equivalent, 3 = layouts differ, 1 = unreadable file "
            "or unknown --top."
        ),
    )
    diff_parser.add_argument("file_a", help="first GDSII or OASIS layout file")
    diff_parser.add_argument("file_b", help="second GDSII or OASIS layout file")
    diff_parser.add_argument(
        "--top",
        default=None,
        help="compare only this cell's hierarchy (must exist in both streams)",
    )
    diff_parser.add_argument(
        "--max-items",
        type=int,
        default=50,
        help="cap on listed differences per list (counts stay exact; default 50)",
    )
    diff_parser.add_argument(
        "--flattened",
        action="store_true",
        help=(
            "compare the flattened (instance-placed) geometry, bounding box "
            "and labels instead of the cell-by-cell content: cell names and "
            "hierarchy partitioning are ignored"
        ),
    )
    _add_format_arg(diff_parser)
    diff_parser.set_defaults(func=diff_cmd.run)
