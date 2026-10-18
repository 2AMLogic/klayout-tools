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

from ..keepout_check import KeepoutCheckError, run_keepout_check
from ._parsing import load_json_path_or_inline, load_region, parse_layer_pairs
from .output import emit_error, emit_success, render_table

EXIT_CLEAR = 0
EXIT_VIOLATED = 3


def run(args: argparse.Namespace) -> int:
    try:
        layers = _load_layers(args.forbidden)
        region_um = load_region(args.region, KeepoutCheckError)
        report = run_keepout_check(
            args.file,
            layers,
            _load_margin(args.margin),
            region_um=region_um,
            top=args.top,
            cells=args.cells,
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
