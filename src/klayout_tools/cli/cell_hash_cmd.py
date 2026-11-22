"""Print a named cell's canonical geometry digest for signoff manifests."""

import argparse
from collections.abc import Callable

from .._provenance import cell_geometry_digest
from .output import emit_error, emit_success


def add_parser(
    subparsers: argparse._SubParsersAction,
    add_format_arg: Callable[[argparse.ArgumentParser], None],
) -> None:
    parser = subparsers.add_parser(
        "cell-hash",
        help="hash the geometry of a named cell and its descendants",
        description="Compute the canonical cell_hash used by foundry-cell exceptions.",
    )
    parser.add_argument("layout", metavar="LAYOUT", help="input GDSII or OASIS layout")
    parser.add_argument("--cell", required=True, metavar="NAME", help="cell to hash")
    add_format_arg(parser)
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    digest = cell_geometry_digest(args.layout, args.cell)
    if digest is None:
        return emit_error(
            "cell-hash",
            f"Cannot hash cell {args.cell!r} in {args.layout!r}",
            args.format,
        )
    emit_success(
        {"schema_version": 1, "cell_hash": digest},
        args.format,
        lambda result: print(result["cell_hash"]),
    )
    return 0
