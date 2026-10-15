"""``klt stats`` command: serialise the stats report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand — see ``docs/json-contract.md``.
"""

import argparse

from ..stats import StatsError, stats_report
from .output import emit_error, emit_success, render_table


def run(args: argparse.Namespace) -> int:
    try:
        window_layers = (
            [_parse_layer_spec(spec) for spec in args.window_layer]
            if args.window_layer
            else None
        )
        report = stats_report(
            args.file,
            per_layer=args.per_layer,
            top=args.top,
            density_window=args.density_window,
            window_layers=window_layers,
        )
    except StatsError as exc:
        return emit_error("stats", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _parse_layer_spec(spec: str) -> tuple[int, int]:
    try:
        layer, datatype = spec.split("/")
        return int(layer), int(datatype)
    except ValueError:
        raise StatsError(f"--window-layer must look like L/D, got: {spec}") from None


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"top_cell: {report['top_cell'] if report['top_cell'] is not None else '-'}")

    bbox = report["bbox_um"]
    print(
        "bbox_um: "
        f"({bbox['left']}, {bbox['bottom']}) - ({bbox['right']}, {bbox['top']})"
        f"  {bbox['width']} x {bbox['height']}"
    )

    total = report["total"]
    print(f"area_um2: {total['area_um2']}")
    print(f"density: {total['density']}")
    print(f"merged_area_um2: {total['merged_area_um2']}")
    print(f"merged_density: {total['merged_density']}")
    print(f"polygons: {total['polygon_count']}")
    print(f"vertices: {total['vertex_count']}")

    windows = report["density_windows"]
    if windows is not None:
        print(
            f"density_windows: {windows['window_um']} um, "
            f"{windows['window_count']} windows per layer"
        )
        for entry in windows["layers"]:
            summary = entry["summary"]
            print(
                f"  {entry['layer']}/{entry['datatype']}: "
                f"min {summary['min']} max {summary['max']} mean {summary['mean']}"
            )

    layers = report["layers"]
    if not layers:
        return

    rows: list[tuple[str, ...]] = [
        (
            str(entry["layer"]),
            str(entry["datatype"]),
            str(entry["name"]) if entry["name"] is not None else "-",
            str(entry["area_um2"]),
            str(entry["density"]),
            str(entry["merged_area_um2"]),
            str(entry["polygon_count"]),
            str(entry["vertex_count"]),
            "yes" if entry["annotation"] else "-",
        )
        for entry in layers
    ]
    headers = (
        "layer",
        "datatype",
        "name",
        "area_um2",
        "density",
        "merged_area_um2",
        "polygons",
        "vertices",
        "annotation",
    )
    # name (col 2) left-aligned; everything else numeric-ish, right-aligned.
    render_table(headers, rows, left_aligned={2})
