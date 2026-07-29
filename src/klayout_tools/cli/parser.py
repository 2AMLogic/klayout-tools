"""Argument parser for the ``klt`` CLI.

``--format {text,json}`` is a *per-subcommand* option (kicad-tools convention),
defaulting to ``text``. New subcommands register themselves here and point their
``func`` default at a ``run(args) -> int`` handler.
"""

import argparse

from .. import __version__
from . import layers_cmd


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="klt",
        description="Tools for AI agents to work with IC layout.",
    )
    parser.add_argument(
        "--version", action="version", version=f"klt {__version__}"
    )
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
    layers_parser.add_argument(
        "file", help="path to a GDSII or OASIS layout file"
    )
    layers_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    layers_parser.set_defaults(func=layers_cmd.run)

    return parser
