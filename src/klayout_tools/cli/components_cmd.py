"""``klt components`` command: report geometric connected components across a
caller-selected conductor/via stack, as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/components.md`` for the full table):
    0 - ran successfully (the components report was produced, even if it is
        empty -- this is a report, not a pass/fail check)
    1 - failed to run (bad file, malformed --conductors/--vias/--label-layers,
        unknown --top, malformed --region, or an unknown conductor name
        referenced by a via) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)
"""

from __future__ import annotations

import argparse

from ..components import ComponentsError, run_components_report
from ._parsing import load_json_path_or_inline, load_region
from .output import emit_error, emit_success, render_table


def run(args: argparse.Namespace) -> int:
    try:
        conductors = _load_json_array(args.conductors, "--conductors")
        vias = _load_json_array(args.vias, "--vias") if args.vias is not None else []
        label_layers = (
            _load_json_array(args.label_layers, "--label-layers")
            if args.label_layers is not None
            else []
        )
        region_um = _load_region(args.region)
        report = run_components_report(
            args.file,
            conductors,
            vias=vias,
            label_layers=label_layers,
            region_um=region_um,
            top=args.top,
        )
    except ComponentsError as exc:
        return emit_error("components", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _load_json_array(value: str, flag: str) -> list:
    """Resolve a ``--conductors``/``--vias``/``--label-layers`` value into a
    JSON array: a path to a JSON file, or an inline JSON string -- the same
    "path-or-inline-JSON" convention ``klt ring-check --layers`` uses.
    """
    return load_json_path_or_inline(value, flag, ComponentsError)


def _load_region(value: str | None) -> tuple[float, float, float, float] | None:
    """Resolve an optional ``--region`` value into a micrometre
    ``(left, bottom, right, top)`` crop window, or ``None`` when omitted --
    identical convention to ``klt ring-check --region``.
    """
    return load_region(value, ComponentsError)


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"top: {report['top'] if report['top'] is not None else '-'}")
    if report["region_um"] is not None:
        left, bottom, right, top = report["region_um"]
        print(f"region_um: ({left},{bottom})-({right},{top})")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"components: {report['component_count']}")

    components = report["components"]
    if not components:
        return

    rows = [
        (
            entry["id"],
            entry["cell"],
            f"({entry['bbox_um']['left']},{entry['bbox_um']['bottom']})-"
            f"({entry['bbox_um']['right']},{entry['bbox_um']['top']})",
            "+".join(c["name"] for c in entry["conductors"]) or "-",
            "+".join(v["name"] for v in entry["vias"]) or "-",
            ",".join(entry["labels"]) or "-",
            "yes" if entry["touches_crop_boundary"] else "-",
        )
        for entry in components
    ]
    render_table(
        ("Id", "Cell", "BBox (um)", "Conductors", "Vias", "Labels", "Touches Crop"),
        rows,
        left_aligned={0, 1, 3, 4, 5},
    )
