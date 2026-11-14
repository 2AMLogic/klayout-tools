"""``klt route`` command: route between labelled pins of a placed layout.

Output goes through the shared envelope helpers in :mod:`.output` -- see
``docs/json-contract.md``.

Exit codes (see ``docs/cli/route.md``):
    0 - every net routed with no foreign overlaps
    1 - application error (unreadable layout/request, unknown instance or
        label, unsupported orientation, ...) -- returned by ``emit_error``
    2 - usage error (from argparse)
    3 - a report was emitted but ``unrouted_nets[]`` or ``overlaps[]`` is
        non-empty
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable

from ..route_labelled import RouteError, route
from ._parsing import RequestFileError, load_request_file
from .output import emit_error, emit_success

EXIT_FINDINGS = 3


def run(args: argparse.Namespace) -> int:
    request_dir = os.path.dirname(os.path.abspath(args.request))
    try:
        request = load_request_file(args.request)
    except RequestFileError as exc:
        return emit_error("route", str(exc), args.format)

    try:
        report = route(request, request_dir=request_dir)
    except RouteError as exc:
        return emit_error(
            "route", str(exc), args.format, code=exc.code, details=exc.details
        )

    emit_success(report, args.format, _print_text)
    return 0 if report["status"] == "clean" else EXIT_FINDINGS


def _print_text(report: dict) -> None:
    print(f"layout: {report['layout']['gds_path']} ({report['layout']['top_cell']})")
    if report["output"]:
        print(f"output: {report['output']['gds_path']}")
    print(f"status: {report['status']}")
    for entry in report["routes"]:
        print(f"  {entry['net']}  routed  length={entry['length_um']}um")
    for entry in report["unrouted_nets"]:
        print(f"  {entry['net']}  UNROUTED  {entry['reason']}")
    for entry in report["overlaps"]:
        owner = entry["foreign_owner"]
        box = entry["overlap_bbox_um"]
        print(
            f"  OVERLAP {entry['net']} with cell={owner['cell']} net={owner['net']} "
            f"({box['x0']},{box['y0']})-({box['x1']},{box['y1']}) "
            f"area={entry['overlap_area_um2']}um2"
        )


def add_parser(
    subparsers: argparse._SubParsersAction,
    _add_format_arg: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register the ``route`` verb: route labelled pins in a placed layout."""
    parser = subparsers.add_parser(
        "route",
        help="route between labelled pins of an already-placed layout",
        description=(
            "Read a request document naming a placed layout, derive endpoints "
            "from text labels on direct child instances of its top cell, and "
            "route each two-pin net with the gen-compose router (unchanged). "
            "Reports every positive-area overlap of a drawn route with foreign "
            "metal; optionally writes the routed layout. A diagnostic like "
            "gen-compose's own checks -- run `klt drc` for sign-off. "
            "See docs/cli/route.md."
        ),
    )
    parser.add_argument("request", help="path to a klt route request JSON file")
    _add_format_arg(parser)
    parser.set_defaults(func=run)
