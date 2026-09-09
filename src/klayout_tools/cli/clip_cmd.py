"""``klt clip`` command: write a bbox region or a named cell's subtree out of
a GDSII/OASIS stream into its own top-cell stream, and serialise the result
as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/clip.md`` for the full table):
    0 - the clipped/extracted stream was written successfully
    1 - failed to run (bad file, unknown --cell, malformed/degenerate
        --region, a region matching no geometry, or an unwritable output
        path) -- returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors -- including passing both or
neither of --region/--cell -- as with every other ``klt`` subcommand.)
"""

from __future__ import annotations

import argparse

from ..clip import ClipError, run_clip
from ._parsing import load_region
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        region_um = load_region(args.region, ClipError)
        report = run_clip(
            args.file,
            args.output,
            region_um=region_um,
            cell=args.cell,
            top=args.top,
        )
    except ClipError as exc:
        return emit_error("clip", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"output: {report['output']}")
    print(f"mode: {report['mode']}")
    if report["mode"] == "cell":
        print(f"cell: {report['cell']}")
    else:
        left, bottom, right, top = report["region_um"]
        print(f"region_um: ({left},{bottom})-({right},{top})")
        print(f"top: {report['top']}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"cell_count: {report['cell_count']}")
    print(f"shape_count: {report['shape_count']}")
