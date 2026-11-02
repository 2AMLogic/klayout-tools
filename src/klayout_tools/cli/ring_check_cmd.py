"""``klt ring-check`` command: assert a layer set forms a closed annulus and
serialise the result as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/ring-check.md`` for the full table):
    0 - ring is continuous (one closed annulus per checked top cell)
    1 - failed to run (bad file, empty/invalid --layers, unknown --top,
        empty/unknown --cells, malformed --region) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
    3 - ran successfully, the ring is broken
    4 - ran successfully but nothing could be checked (``--pdk`` for a PDK
        with no published seal-ring rules: ``status: "not_checked"``)
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)
"""

from __future__ import annotations

import argparse

from ..ring_check import RingCheckError, run_ring_check, run_seal_ring_check
from ._parsing import load_json_path_or_inline, load_region, parse_layer_pairs
from .output import emit_error, emit_success, render_table

EXIT_CONTINUOUS = 0
EXIT_BROKEN = 3
EXIT_NOT_CHECKED = 4


def run(args: argparse.Namespace) -> int:
    if args.pdk is not None:
        return _run_pdk(args)
    try:
        if args.layers is None:
            raise RingCheckError("one of --layers or --pdk is required")
        layers = _load_layers(args.layers)
        region_um = _load_region(args.region)
        report = run_ring_check(
            args.file,
            layers,
            region_um=region_um,
            top=args.top,
            ignore_enclosed=args.ignore_enclosed,
            cells=args.cells,
        )
    except RingCheckError as exc:
        return emit_error("ring-check", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return EXIT_BROKEN if report["status"] == "broken" else EXIT_CONTINUOUS


def _run_pdk(args: argparse.Namespace) -> int:
    try:
        if args.layers is not None:
            raise RingCheckError("--pdk and --layers are mutually exclusive")
        if args.region is not None or args.cells is not None or args.ignore_enclosed:
            raise RingCheckError(
                "--region, --cells and --ignore-enclosed apply to --layers mode only"
            )
        report = run_seal_ring_check(args.file, args.pdk, top=args.top)
    except RingCheckError as exc:
        return emit_error("ring-check", str(exc), args.format)

    emit_success(report, args.format, _print_pdk_text)
    if report["status"] == "broken":
        return EXIT_BROKEN
    return EXIT_NOT_CHECKED if report["status"] == "not_checked" else EXIT_CONTINUOUS


def _print_pdk_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"pdk: {report['pdk']}")
    print(f"status: {report['status']}")
    if report["reason"]:
        print(f"reason: {report['reason']}")
    print(f"violations: {report['violation_count']}")
    for rule in report["rules"]:
        extra = f" ({rule['reason']})" if rule["reason"] else ""
        print(f"  {rule['id']}: {rule['status']}{extra} [{rule['source']['file']}]")
    for entry in report["violations"]:
        b = entry["bbox"]
        print(
            f"  {entry['rule']} {entry['cell']} ({b['left']},{b['bottom']})-"
            f"({b['right']},{b['top']}): {entry['description']}"
        )


def _load_layers(value: str) -> list[tuple[int, int]]:
    """Resolve a ``--layers`` value into a list of ``(layer, datatype)`` pairs.

    ``value`` is a path to a JSON file or an inline JSON string, each holding a
    JSON array of two-element ``[layer, datatype]`` arrays, e.g.
    ``[[33, 0], [66, 44]]`` -- the same "path-or-inline-JSON array of
    [layer, datatype] pairs" convention ``klt precheck --allowed-layers`` uses.
    """
    data = load_json_path_or_inline(
        value,
        "--layers",
        RingCheckError,
        inline_hint="an inline JSON array of [layer, datatype] pairs",
    )
    return parse_layer_pairs(data, "--layers", RingCheckError)


def _load_region(value: str | None) -> tuple[float, float, float, float] | None:
    """Resolve an optional ``--region`` value into a micrometre
    ``(left, bottom, right, top)`` clip window, or ``None`` when omitted.

    ``value`` is an inline JSON array of four numbers in micrometres, e.g.
    ``[0, 0, 100, 100]``. Omit it to check every shape on the layer set (the
    common case: a stream whose only geometry on those layers is the ring).
    """
    return load_region(value, RingCheckError)


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"layers: {'+'.join(f'{layer}/{dt}' for layer, dt in report['layers'])}")
    if report["region_um"] is not None:
        left, bottom, right, top = report["region_um"]
        print(f"region_um: ({left},{bottom})-({right},{top})")
    if report.get("cells") is not None:
        print(f"cells: {', '.join(report['cells'])}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"status: {report['status']}")
    print(f"violations: {report['violation_count']}")

    violations = report["violations"]
    if not violations:
        return

    rows = [
        (
            entry["kind"],
            entry["cell"],
            entry["layer"],
            f"({entry['bbox']['left']},{entry['bbox']['bottom']})-"
            f"({entry['bbox']['right']},{entry['bbox']['top']})",
            entry["description"],
        )
        for entry in violations
    ]
    render_table(
        ("Kind", "Cell", "Layer", "BBox", "Description"),
        rows,
        left_aligned={0, 1, 2, 4},
    )
