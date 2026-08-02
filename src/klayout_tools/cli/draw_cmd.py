"""``klt draw`` command: write a primitive GDSII/OASIS stream from a JSON
shape/label description.

The deliberately-dumb write-side counterpart to the read-side verbs -- no PDK
awareness, no rule checking -- so a DRC flow's negative case (a known-bad
fixture) can be produced with ``klt`` alone (issue #230). Output goes through
the shared envelope helpers in :mod:`.output`, as with every other ``klt``
subcommand (see ``docs/json-contract.md``).

Exit codes (see ``docs/cli/draw.md``):
    0 - the stream was written
    1 - application error: invalid params, bad shape/label, empty shapes, or a
        write failure -- returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
    2 - usage error (bad --format value) -- from argparse
"""

import argparse

from ..draw import REQUEST_SCHEMA, DrawError, draw, load_params_arg
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        params = load_params_arg(args.params)
    except DrawError as exc:
        return emit_error("draw", str(exc), args.format)

    request = {
        "schema": REQUEST_SCHEMA,
        "params": params,
        "options": {"cell_name": args.cell_name, "output": args.output},
    }

    try:
        report = draw(request)
    except DrawError as exc:
        return emit_error("draw", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _print_text(report: dict) -> None:
    print(f"gds_path: {report['gds_path']}")
    print(f"cell_name: {report['cell_name']}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"shape_count: {report['shape_count']}")
    print(f"label_count: {report['label_count']}")

    bbox = report["bbox_um"]
    if bbox is not None:
        print(
            f"bbox_um: ({bbox['x0']}, {bbox['y0']}) - ({bbox['x1']}, {bbox['y1']})"
        )

    layers = report["layers"]
    if layers:
        print()
        print("layers:")
        for layer in layers:
            name = layer["name"] or "-"
            print(
                f"  {layer['layer']}/{layer['datatype']}  {name}  "
                f"shapes={layer['shapes']}"
            )

    warnings = report["warnings"]
    if warnings:
        print()
        print("warnings:")
        for warning in warnings:
            print(f"  {warning}")
