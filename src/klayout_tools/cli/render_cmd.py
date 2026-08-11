"""``klt render`` command: serialise the render report as text or JSON."""

import argparse
import json
import os

from ..render import RenderError, render_report
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        layers = _load_layers(args.layers)
        bbox = _load_bbox(args.bbox)
        report = render_report(
            args.file,
            output_dir=args.output,
            width=args.width,
            height=args.height,
            background=args.background,
            top=args.top,
            layers=layers,
            bbox=bbox,
        )
    except RenderError as exc:
        return emit_error("render", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _load_layers(value: str | None) -> list[tuple[int, int]] | None:
    """Resolve an optional ``--layers`` value into ``(layer, datatype)`` pairs,
    or ``None`` when the flag was omitted (renders every layer, unchanged).

    ``value`` is a path to a JSON file or an inline JSON string, each holding
    a JSON array of two-element ``[layer, datatype]`` arrays, e.g.
    ``[[33, 0], [66, 44]]`` -- the same "path-or-inline-JSON array of
    [layer, datatype] pairs" convention ``klt ring-check --layers`` /
    ``klt precheck --allowed-layers`` use.
    """
    if value is None:
        return None

    if os.path.isfile(value):
        try:
            with open(value, encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError as exc:
            raise RenderError(f"could not read --layers file '{value}': {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RenderError(
                f"--layers file '{value}' is not valid JSON: {exc}"
            ) from exc
    else:
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RenderError(
                "--layers must be a path to a JSON file or an inline JSON "
                f"array of [layer, datatype] pairs: {exc}"
            ) from exc

    if not isinstance(data, list):
        raise RenderError("--layers must decode to a JSON array")

    layers: list[tuple[int, int]] = []
    for entry in data:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in entry)
        ):
            raise RenderError(
                "--layers entries must each be a [layer, datatype] pair of "
                f"integers, got {entry!r}"
            )
        layers.append((entry[0], entry[1]))

    if not layers:
        raise RenderError("--layers must contain at least one layer")

    return layers


def _load_bbox(value: str | None) -> tuple[float, float, float, float] | None:
    """Resolve an optional ``--bbox`` value into a micrometre
    ``(left, bottom, right, top)`` crop window, or ``None`` when omitted
    (renders the whole layout, unchanged).

    ``value`` is a comma-separated ``xmin,ymin,xmax,ymax`` string in
    micrometres (e.g. ``"0,0,10,10"``), matching the comma-separated
    convention ``klt extract --pins`` already uses for a flat list value.
    """
    if value is None:
        return None

    parts = value.split(",")
    if len(parts) != 4:
        raise RenderError(
            "--bbox must be four comma-separated numbers "
            f"'xmin,ymin,xmax,ymax' in micrometres, got {value!r}"
        )

    try:
        xmin, ymin, xmax, ymax = (float(part.strip()) for part in parts)
    except ValueError as exc:
        raise RenderError(
            "--bbox must be four comma-separated numbers "
            f"'xmin,ymin,xmax,ymax' in micrometres, got {value!r}: {exc}"
        ) from exc

    if xmax <= xmin or ymax <= ymin:
        raise RenderError(
            "--bbox must have xmax > xmin and ymax > ymin, got "
            f"({xmin}, {ymin}, {xmax}, {ymax})"
        )

    return (xmin, ymin, xmax, ymax)


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"output_dir: {report['output_dir']}")
    print(f"size: {report['width']}x{report['height']}")
    print(f"background: {report['background']}")
    print(f"overview: {report['overview']}")
    print(f"layers: {report['layer_count']}")
    print(f"rendered: {report['rendered_count']}")
    if report["requested_layers"] is not None:
        requested = "+".join(
            f"{layer}/{datatype}" for layer, datatype in report["requested_layers"]
        )
        print(f"requested_layers: {requested}")
    if report["requested_bbox"] is not None:
        b = report["requested_bbox"]
        print(f"requested_bbox: ({b['left']},{b['bottom']})-({b['right']},{b['top']})")
    extent = report["actual_extent"]
    print(
        f"actual_extent: ({extent['left']},{extent['bottom']})-"
        f"({extent['right']},{extent['top']})"
    )

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
            entry["path"] if entry["path"] is not None else "-",
        )
        for entry in layers
    ]
    headers = ("layer", "datatype", "name", "shapes", "annotation", "path")
    widths = [
        max(len(headers[col]), max(len(row[col]) for row in rows))
        for col in range(len(headers))
    ]

    def fmt(row: tuple[str, ...]) -> str:
        # layer/datatype/shapes/annotation right-aligned (numeric-ish),
        # name/path left-aligned.
        return "  ".join(
            row[col].ljust(widths[col])
            if col in (2, 5)
            else row[col].rjust(widths[col])
            for col in range(len(headers))
        )

    print()
    print(fmt(headers))
    print("  ".join("-" * widths[col] for col in range(len(headers))))
    for row in rows:
        print(fmt(row))
