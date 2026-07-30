"""Argument parser for the ``klt`` CLI.

``--format {text,json}`` is a *per-subcommand* option (kicad-tools convention),
defaulting to ``text``. New subcommands register themselves here and point their
``func`` default at a ``run(args) -> int`` handler.
"""

import argparse

from .. import __version__
from . import cells_cmd, drc_cmd, layers_cmd, stats_cmd


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="klt",
        description="Tools for AI agents to work with IC layout.",
    )
    parser.add_argument("--version", action="version", version=f"klt {__version__}")
    # No default handler: absence of a subcommand is handled by main().
    parser.set_defaults(func=None)

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    layers_parser = subparsers.add_parser(
        "layers",
        help="enumerate layers of a GDSII/OASIS stream",
        description=(
            "Report the layer/datatype pairs, names, and per-cell-definition "
            "shape counts of a GDSII or OASIS layout file."
        ),
    )
    layers_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    layers_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    layers_parser.set_defaults(func=layers_cmd.run)

    stats_parser = subparsers.add_parser(
        "stats",
        help="report area, density, and polygon/vertex counts of a GDSII/OASIS stream",
        description=(
            "Report bounding box, drawn area, density, and polygon/vertex "
            "counts of a GDSII or OASIS layout file, in total and optionally "
            "per layer."
        ),
    )
    stats_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    stats_parser.add_argument(
        "--per-layer",
        action="store_true",
        help="also report the same statistics broken down per layer",
    )
    stats_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    stats_parser.set_defaults(func=stats_cmd.run)

    cells_parser = subparsers.add_parser(
        "cells",
        help="report the cell hierarchy of a GDSII/OASIS stream",
        description=(
            "Report the cell hierarchy of a GDSII or OASIS layout file: "
            "top-cell status, per-cell shape/instance counts, direct "
            "children/parents, and bounding box."
        ),
    )
    cells_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    cells_parser.add_argument(
        "--top",
        action="store_true",
        help="only report top cells (cells with no parent instances)",
    )
    cells_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    cells_parser.set_defaults(func=cells_cmd.run)

    drc_parser = subparsers.add_parser(
        "drc",
        help="run a headless DRC deck against a GDSII/OASIS stream",
        description=(
            "Run a DRC rule deck against a GDSII or OASIS layout file and "
            "report violations as structured data. Runs fully headless via "
            "KLayout's native Region check primitives — no GUI, no Qt, no "
            "standalone klayout binary."
        ),
    )
    drc_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    drc_parser.add_argument(
        "--deck",
        required=True,
        help=(
            "DRC deck to run (currently: sky130). Not validated by argparse "
            "-- an unknown deck name exits 1 with a clean error, per "
            "docs/cli/drc.md's exit-code contract, rather than argparse's "
            "usage-error exit 2."
        ),
    )
    drc_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    drc_parser.set_defaults(func=drc_cmd.run)

    return parser
