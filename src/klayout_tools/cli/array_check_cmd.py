"""``klt array-check`` command: assert mirror/translation symmetry and
pitch-matched abutment of a cell array and serialise the result as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/array-check.md`` for the full table):
    0 - the array is symmetric / abuts as declared
    1 - failed to run (bad file, no check requested, empty layer set, unknown
        cell/top, bad pitch/count/tolerance/translate) -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - ran successfully, at least one violation
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)
"""

from __future__ import annotations

import argparse
import json

from ..array_check import ArrayCheckError, run_array_check
from ._parsing import load_json_path_or_inline, parse_layer_pairs
from .output import emit_error, emit_success, render_table

EXIT_CLEAR = 0
EXIT_VIOLATED = 3


def run(args: argparse.Namespace) -> int:
    try:
        layers = _load_layers(args.layers) if args.layers is not None else None
        report = run_array_check(
            args.file,
            layers,
            top=args.top,
            cell=args.cell,
            mirror_axis=args.mirror_axis,
            at_um=_number(args.at, "--at"),
            translate_um=_load_translate(args.translate),
            tolerance_um=_number(args.tolerance, "--tolerance") or 0.0,
            pitch_um=_number(args.pitch, "--pitch"),
            count=_load_count(args.count),
            direction=args.direction,
        )
    except ArrayCheckError as exc:
        return emit_error("array-check", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return EXIT_VIOLATED if report["status"] == "violated" else EXIT_CLEAR


def _load_layers(value: str) -> list[tuple[int, int]]:
    data = load_json_path_or_inline(
        value,
        "--layers",
        ArrayCheckError,
        inline_hint="an inline JSON array of [layer, datatype] pairs",
    )
    return parse_layer_pairs(data, "--layers", ArrayCheckError)


def _number(value: str | None, flag: str) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ArrayCheckError(
            f"{flag} must be a number in micrometres, got {value!r}"
        ) from exc


def _load_count(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ArrayCheckError(f"--count must be an integer, got {value!r}") from exc


def _load_translate(value: str | None) -> tuple[float, float] | None:
    if value is None:
        return None
    parts = value.split(",")
    try:
        if len(parts) != 2:
            raise ValueError
        return float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise ArrayCheckError(
            f"--translate must be 'dx,dy' in micrometres, got {value!r}"
        ) from exc


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    if report["layers"] is not None:
        print(f"layers: {', '.join(f'{layer}/{dt}' for layer, dt in report['layers'])}")
    for key in ("cell", "mirror_axis", "at_um", "translate_um", "pitch_um", "count"):
        if report[key] is not None:
            print(f"{key}: {json.dumps(report[key])}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"status: {report['status']}")
    print(f"violations: {report['violation_count']}")

    violations = report["violations"]
    if not violations:
        return
    rows: list[tuple[str, ...]] = [
        (
            str(v["check"]),
            str(v["kind"]),
            str(v["cell"]),
            str(v["layer"] or "-"),
            f"({v['bbox']['left']},{v['bbox']['bottom']})-"
            f"({v['bbox']['right']},{v['bbox']['top']})",
            str(v["description"]),
        )
        for v in violations
    ]
    render_table(
        ("Check", "Kind", "Cell", "Layer", "BBox (um)", "Description"),
        rows,
        left_aligned={0, 1, 2, 3, 5},
    )
