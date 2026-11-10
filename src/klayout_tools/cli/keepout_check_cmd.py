"""``klt keepout-check`` command: assert no forbidden-layer shape lies within a
margin of a guarded cell/region and serialise the result as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/keepout-check.md`` for the full table):
    0 - keep-out is clear
    1 - failed to run (bad file, empty/invalid --forbidden, no guard, unknown
        --cells/--top, bad --margin, malformed --region) -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - ran successfully, forbidden shapes intrude on the keep-out
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable

from ..keepout_check import KeepoutCheckError, run_keepout_check
from ._parsing import load_json_path_or_inline, load_region, parse_layer_pairs
from .output import emit_error, emit_success, render_table

EXIT_CLEAR = 0
EXIT_VIOLATED = 3


def run(args: argparse.Namespace) -> int:
    try:
        layers = _load_layers(args.forbidden)
        region_um = load_region(args.region, KeepoutCheckError)
        allow = [_load_allow(value) for value in args.allow or []]
        report = run_keepout_check(
            args.file,
            layers,
            _load_margin(args.margin),
            region_um=region_um,
            top=args.top,
            cells=args.cells,
            allow=allow,
        )
    except KeepoutCheckError as exc:
        return emit_error("keepout-check", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return EXIT_VIOLATED if report["status"] == "violated" else EXIT_CLEAR


def _load_layers(value: str) -> list[tuple[int, int]]:
    """Resolve ``--forbidden`` (path or inline JSON array of
    ``[layer, datatype]`` pairs) -- the ``klt ring-check --layers`` convention."""
    data = load_json_path_or_inline(
        value,
        "--forbidden",
        KeepoutCheckError,
        inline_hint="an inline JSON array of [layer, datatype] pairs",
    )
    return parse_layer_pairs(data, "--forbidden", KeepoutCheckError)


def _load_allow(value: str) -> tuple:
    """Parse one ``--allow`` value: inline JSON
    ``{"bbox": [l, b, r, t], "layers": [[l, d], ...]?}`` (micrometres)."""
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise KeepoutCheckError(
            f"--allow must be an inline JSON object: {exc}"
        ) from exc
    if not isinstance(data, dict) or "bbox" not in data:
        raise KeepoutCheckError(
            f'--allow must be a JSON object with a "bbox" key, got {value!r}'
        )
    unknown = sorted(set(data) - {"bbox", "layers"})
    if unknown:
        raise KeepoutCheckError(f"--allow has unknown key(s): {', '.join(unknown)}")
    bbox = load_region(json.dumps(data["bbox"]), KeepoutCheckError, "--allow bbox")
    layers = None
    if data.get("layers") is not None:
        if not isinstance(data["layers"], list):
            raise KeepoutCheckError("--allow layers must be a JSON array of pairs")
        layers = parse_layer_pairs(data["layers"], "--allow layers", KeepoutCheckError)
    return bbox, layers


def _load_margin(value: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise KeepoutCheckError(
            f"--margin must be a number in micrometres, got {value!r}"
        ) from exc


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"forbidden: {', '.join(f'{layer}/{dt}' for layer, dt in report['layers'])}")
    print(f"margin_um: {report['margin_um']}")
    if report["region_um"] is not None:
        left, bottom, right, top = report["region_um"]
        print(f"region_um: ({left},{bottom})-({right},{top})")
    if report["cells"] is not None:
        print(f"cells: {', '.join(report['cells'])}")
    for window in report["allow"]:
        left, bottom, right, top = window["bbox_um"]
        scope = ", ".join(f"{layer}/{dt}" for layer, dt in window["layers"])
        print(f"allow: ({left},{bottom})-({right},{top}) layers {scope}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"status: {report['status']}")
    print(f"violations: {report['violation_count']}")

    violations = report["violations"]
    if not violations:
        return

    rows: list[tuple[str, ...]] = [
        (
            str(entry["kind"]),
            str(entry["cell"]),
            str(entry["layer"]),
            f"({entry['bbox']['left']},{entry['bbox']['bottom']})-"
            f"({entry['bbox']['right']},{entry['bbox']['top']})",
            str(entry["description"]),
        )
        for entry in violations
    ]
    render_table(
        ("Kind", "Cell", "Layer", "BBox (um)", "Description"),
        rows,
        left_aligned={0, 1, 2, 4},
    )


def add_parser(
    subparsers: argparse._SubParsersAction,
    _add_format_arg: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register the ``keepout-check`` verb: no forbidden shapes near a guard."""
    keepout_parser = subparsers.add_parser(
        "keepout-check",
        help="assert no forbidden-layer shape lies within a margin of a cell/region",
        description=(
            "Assert that no shape on a forbidden layer set lies within a "
            "margin of a guarded cell or region (a keep-out around a spiral "
            "inductor, a sensitive analog block, ...) and report each "
            "intruding shape with its layer and micrometre coordinates. The "
            "guard is the bounding box of every placement of the --cells "
            "names and/or the --region window, sized outward by --margin; "
            "the guarded cells' own shapes are never counted. Purely "
            "geometric; fully headless via KLayout's batch database API. "
            "See docs/cli/keepout-check.md."
        ),
    )
    keepout_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    keepout_parser.add_argument(
        "--forbidden",
        "--layers",
        dest="forbidden",
        required=True,
        help=(
            "forbidden layer set as a path to a JSON file, or an inline JSON "
            "array of [layer, datatype] pairs (e.g. '[[67, 20]]'). Not "
            "validated by argparse -- an empty/malformed value exits 1."
        ),
    )
    keepout_parser.add_argument(
        "--margin",
        required=True,
        help=(
            "keep-out margin in micrometres (>= 0); a shape at distance "
            "exactly the margin is clear. Not validated by argparse -- a "
            "non-numeric or negative value exits 1."
        ),
    )
    keepout_parser.add_argument(
        "--cells",
        action="append",
        default=None,
        metavar="CELL",
        help=(
            "repeatable exact cell name to guard (every placement under each "
            "checked top cell, by bounding box). At least one of --cells / "
            "--region is required."
        ),
    )
    keepout_parser.add_argument(
        "--region",
        default=None,
        help=(
            "guarded window as an inline JSON array of four micrometre "
            "coordinates [left, bottom, right, top]; may be combined with "
            "--cells"
        ),
    )
    keepout_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to check when the stream has more than one; omit to "
            "check every top cell"
        ),
    )
    keepout_parser.add_argument(
        "--allow",
        action="append",
        default=None,
        metavar="JSON",
        help=(
            "repeatable pin-access window exempt from the check, as inline "
            'JSON {"bbox": [left, bottom, right, top], "layers": [[l, d], ...]} '
            "in micrometres; omit layers to cover every forbidden layer. "
            "Geometry outside the window is still checked; windows are "
            "echoed in the report's allow array."
        ),
    )
    _add_format_arg(keepout_parser)
    keepout_parser.set_defaults(func=run)
