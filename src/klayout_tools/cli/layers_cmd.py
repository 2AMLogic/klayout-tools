"""``klt layers`` command: serialise the layers report as text or JSON."""

import argparse

from ..layers import LayersError, layers_report
from .output import emit_error, emit_success, render_table


def run(args: argparse.Namespace) -> int:
    try:
        report = layers_report(
            args.file,
            top=args.top,
            flattened=args.flattened,
            include_text=args.include_text,
        )
    except LayersError as exc:
        return emit_error("layers", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _fmt_bbox(bbox: dict) -> str:
    return (
        f"({bbox['left']:g}, {bbox['bottom']:g}) - ({bbox['right']:g}, {bbox['top']:g})"
    )


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"layers: {report['layer_count']}")

    layers = report["layers"]
    if not layers:
        return

    flattened = "flattened_shapes" in layers[0]
    include_text = flattened and "text_count" in layers[0]

    def row(entry: dict) -> tuple[str, ...]:
        base = (
            str(entry["layer"]),
            str(entry["datatype"]),
            entry["name"] if entry["name"] is not None else "-",
            str(entry["shapes"]),
            "yes" if entry["annotation"] else "-",
        )
        if not flattened:
            return base
        contributors = ",".join(
            f"{c['cell']}:{c['shapes']}" for c in entry["contributors"]
        )
        extra = (
            str(entry["flattened_shapes"]),
            _fmt_bbox(entry["bbox_um"]),
            contributors if contributors else "-",
        )
        if not include_text:
            return base + extra
        texts = ",".join(f"{t['text']}:{t['count']}" for t in entry["texts"])
        return base + extra + (str(entry["text_count"]), texts if texts else "-")

    rows = [row(entry) for entry in layers]
    headers = ("layer", "datatype", "name", "shapes", "annotation")
    left_aligned = {2}
    if flattened:
        headers += ("flattened_shapes", "bbox_um", "contributors")
        left_aligned |= {6, 7}
        if include_text:
            headers += ("text_count", "texts")
            left_aligned |= {9}
    # layer/datatype/shapes/annotation/flattened_shapes/text_count
    # right-aligned (numeric-ish); name/bbox_um/contributors/texts
    # left-aligned (free text).
    render_table(headers, rows, left_aligned=left_aligned)
