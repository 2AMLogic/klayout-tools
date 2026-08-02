"""``klt layers`` command: serialise the layers report as text or JSON."""

import argparse

from ..layers import LayersError, layers_report
from .output import emit_error, emit_success, render_table


def run(args: argparse.Namespace) -> int:
    try:
        report = layers_report(args.file)
    except LayersError as exc:
        return emit_error("layers", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"layers: {report['layer_count']}")

    layers = report["layers"]
    if not layers:
        return

    rows = [
        (
            str(entry["layer"]),
            str(entry["datatype"]),
            entry["name"] if entry["name"] is not None else "-",
            str(entry["shapes"]),
            "yes" if entry["annotation"] else "-",
        )
        for entry in layers
    ]
    headers = ("layer", "datatype", "name", "shapes", "annotation")
    # layer/datatype/shapes/annotation right-aligned (numeric-ish), name
    # (col 2) left-aligned.
    render_table(headers, rows, left_aligned={2})
