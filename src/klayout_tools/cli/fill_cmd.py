"""``klt fill`` command: insert density fill on a PDK's fill layers and
serialise the result as text or JSON (shared envelope, ``docs/json-contract.md``).

Exit codes (see ``docs/cli/fill.md``):
    0 - fill written (``target_reached`` may still be false; see the report)
    1 - failed to run (bad file/pdk/layers/boundary, unmapped layer, ...)
(2 is reserved for argparse usage errors.)
"""

from __future__ import annotations

import argparse
import json

from ..fill import FillError, run_fill
from ._parsing import load_json_path_or_inline, load_region, parse_layer_pairs
from .output import emit_error, emit_success, render_table

EXIT_OK = 0


def run(args: argparse.Namespace) -> int:
    try:
        layers = parse_layer_pairs(
            load_json_path_or_inline(
                args.layers,
                "--layers",
                FillError,
                inline_hint="an inline JSON array of [layer, datatype] pairs",
            ),
            "--layers",
            FillError,
        )
        report = run_fill(
            args.file,
            args.pdk,
            layers,
            args.output,
            boundary=args.boundary,
            extent_um=_load_extent(args.extent),
            exclude_um=_load_exclude(args.exclude),
            spacing_um=args.spacing,
            target_density=args.target_density,
            window_um=args.window,
            fill_size_um=args.fill_size,
            fill_gap_um=args.fill_gap,
            top=args.top,
        )
    except FillError as exc:
        return emit_error("fill", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return EXIT_OK


def _load_extent(value: str | None):
    if value is None:
        return None
    if value.lstrip().startswith("["):
        return load_region(value, FillError, "--extent")
    return load_region("[" + value + "]", FillError, "--extent")


def _load_exclude(value: str | None):
    if value is None:
        return None
    data = load_json_path_or_inline(
        value, "--exclude", FillError, inline_hint="an inline JSON array of boxes"
    )
    return [load_region(json.dumps(box), FillError, "--exclude entry") for box in data]


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"output: {report['output']}")
    print(f"pdk: {report['pdk']}")
    print(f"top_cell: {report['top_cell']}")
    print(f"window_um: {report['window_um']}")
    print(f"spacing_um: {report['spacing_um']}")
    rows = [
        (
            e["layer"],
            e["fill_layer"],
            str(e["fill_shape_count"]),
            f"{e['density_before']['mean']:.4f}",
            f"{e['density_after']['mean']:.4f}",
            str(e["target_reached"]),
        )
        for e in report["layers"]
    ]
    render_table(
        ("Layer", "Fill", "Shapes", "Mean before", "Mean after", "Target reached"),
        rows,
        left_aligned={0, 1, 5},
    )
