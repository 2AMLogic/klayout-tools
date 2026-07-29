"""``klt layers`` command: serialise the layers report as text or JSON."""

import argparse
import json
import sys

from ..layers import LayersError, layers_report


def run(args: argparse.Namespace) -> int:
    try:
        report = layers_report(args.file)
    except LayersError as exc:
        print(f"klt layers: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        _print_text(report)
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
        )
        for entry in layers
    ]
    headers = ("layer", "datatype", "name", "shapes")
    widths = [
        max(len(headers[col]), max(len(row[col]) for row in rows))
        for col in range(len(headers))
    ]

    def fmt(row: tuple[str, ...]) -> str:
        # layer/datatype/shapes right-aligned (numeric), name left-aligned.
        return "  ".join(
            row[col].rjust(widths[col]) if col != 2 else row[col].ljust(widths[col])
            for col in range(len(headers))
        )

    print()
    print(fmt(headers))
    print("  ".join("-" * widths[col] for col in range(len(headers))))
    for row in rows:
        print(fmt(row))
