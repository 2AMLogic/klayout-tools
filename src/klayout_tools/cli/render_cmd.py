"""``klt render`` command: serialise the render report as text or JSON."""

import argparse

from ..render import RenderError, render_report
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = render_report(
            args.file,
            output_dir=args.output,
            width=args.width,
            height=args.height,
        )
    except RenderError as exc:
        return emit_error("render", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"output_dir: {report['output_dir']}")
    print(f"size: {report['width']}x{report['height']}")
    print(f"layers: {report['layer_count']}")
    print(f"rendered: {report['rendered_count']}")

    layers = report["layers"]
    if not layers:
        return

    rows = [
        (
            str(entry["layer"]),
            str(entry["datatype"]),
            entry["name"] if entry["name"] is not None else "-",
            str(entry["shapes"]),
            entry["path"] if entry["path"] is not None else "-",
        )
        for entry in layers
    ]
    headers = ("layer", "datatype", "name", "shapes", "path")
    widths = [
        max(len(headers[col]), max(len(row[col]) for row in rows))
        for col in range(len(headers))
    ]

    def fmt(row: tuple[str, ...]) -> str:
        # layer/datatype/shapes right-aligned (numeric), name/path left-aligned.
        return "  ".join(
            row[col].ljust(widths[col])
            if col in (2, 4)
            else row[col].rjust(widths[col])
            for col in range(len(headers))
        )

    print()
    print(fmt(headers))
    print("  ".join("-" * widths[col] for col in range(len(headers))))
    for row in rows:
        print(fmt(row))
