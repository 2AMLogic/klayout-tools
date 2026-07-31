"""Argument parser for the ``klt`` CLI.

``--format {text,json}`` is a *per-subcommand* option (kicad-tools convention),
defaulting to ``text``. New subcommands register themselves here and point their
``func`` default at a ``run(args) -> int`` handler.
"""

import argparse
import sys

from .. import __version__
from . import cells_cmd, drc_cmd, layers_cmd, layout_metrics_cmd, pdk_cmd, stats_cmd


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
            "DRC deck to run (currently: sky130, gf180mcu). Not validated by "
            "argparse -- an unknown deck name exits 1 with a clean error, "
            "per docs/cli/drc.md's exit-code contract, rather than "
            "argparse's usage-error exit 2."
        ),
    )
    drc_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    drc_parser.set_defaults(func=drc_cmd.run)

    layout_metrics_parser = subparsers.add_parser(
        "layout-metrics",
        help="emit a normalized layout.json per block from existing klt output",
        description=(
            "Aggregate klt layers/cells/drc output for a block directory into "
            "a single normalized layout.json, the gallery site's data "
            "contract (epic #13). Never recomputes metrics ad hoc -- it "
            "calls the same library functions that back klt layers/klt "
            "cells/klt drc."
        ),
    )
    layout_metrics_parser.add_argument(
        "block", help="path to a block directory (e.g. blocks/example-block)"
    )
    layout_metrics_parser.add_argument(
        "--deck",
        default=None,
        help=(
            "DRC deck to run for the drc.violation_count field (currently: "
            "sky130, gf180mcu). Omit to skip DRC entirely."
        ),
    )
    layout_metrics_parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="override the output path (default: <block>/output/layout.json)",
    )
    layout_metrics_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print layout.json without writing any file",
    )
    layout_metrics_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    layout_metrics_parser.set_defaults(func=layout_metrics_cmd.run)

    _add_pdk_parser(subparsers)

    return parser


def _add_pdk_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``pdk`` verb with nested ``find``/``list``/``env`` subcommands.

    The other verbs are flat; ``pdk`` groups discovery operations under one
    verb (kicad-tools convention for multi-operation capabilities), so it uses
    argparse sub-subparsers. Each ``--format`` stays a per-subcommand option,
    matching the flat verbs.
    """
    pdk_parser = subparsers.add_parser(
        "pdk",
        help="discover and resolve an installed PDK",
        description=(
            "Locate an installed open_pdks-layout PDK (open_pdks, volare, or "
            "ciel) and report its root, variant, version stamp, and per-tool "
            "asset directories as structured data. This is the one shared "
            "PDK_ROOT resolver every downstream tool imports instead of "
            "re-implementing the lookup. Fully headless; safe in CI."
        ),
    )
    pdk_sub = pdk_parser.add_subparsers(dest="pdk_command", metavar="<subcommand>")

    def _no_subcommand(_args: argparse.Namespace) -> int:
        pdk_parser.print_help(sys.stderr)
        return 2

    pdk_parser.set_defaults(func=_no_subcommand)

    find_parser = pdk_sub.add_parser(
        "find",
        help="resolve one PDK install/variant and report its paths",
        description=(
            "Resolve a single PDK install and variant via the documented "
            "resolution order and emit its root, variant, version, how it was "
            "resolved, and its per-tool asset directories."
        ),
    )
    find_parser.add_argument(
        "--pdk",
        help="variant to resolve (e.g. sky130A); overrides $PDK",
    )
    find_parser.add_argument(
        "--pdk-root",
        dest="pdk_root",
        help="explicit install root; overrides $PDK_ROOT and the search order",
    )
    find_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    find_parser.set_defaults(func=pdk_cmd.run_find)

    list_parser = pdk_sub.add_parser(
        "list",
        help="enumerate every PDK install and variant discovered",
        description=(
            "Enumerate every install and variant found across the full search "
            "order. An empty result is success (exit 0), not an error."
        ),
    )
    list_parser.add_argument(
        "--pdk-root",
        dest="pdk_root",
        help="restrict the scan to this install root",
    )
    list_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    list_parser.set_defaults(func=pdk_cmd.run_list)

    env_parser = pdk_sub.add_parser(
        "env",
        help="emit the resolved paths as eval-able shell exports",
        description=(
            "Emit the resolved install as shell `export` lines "
            '(`eval "$(klt pdk env)"`) so an interactive simulator or '
            "schematic-editor session uses the same install the automated "
            "tooling picked. --format json emits the same payload as `find`."
        ),
    )
    env_parser.add_argument(
        "--pdk",
        help="variant to resolve (e.g. sky130A); overrides $PDK",
    )
    env_parser.add_argument(
        "--pdk-root",
        dest="pdk_root",
        help="explicit install root; overrides $PDK_ROOT and the search order",
    )
    env_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text; text emits shell exports)",
    )
    env_parser.set_defaults(func=pdk_cmd.run_env)
