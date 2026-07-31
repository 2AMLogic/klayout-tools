"""``klt cells`` command: serialise the cells report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand — see ``docs/json-contract.md``.
"""

import argparse

from ..cells import CellsError, cells_report
from .output import emit_error, emit_success, render_table


def run(args: argparse.Namespace) -> int:
    try:
        report = cells_report(args.file, top=args.top)
    except CellsError as exc:
        return emit_error("cells", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"cells: {report['cell_count']}")
    print(f"top_cells: {report['top_cell_count']}")

    cells = report["cells"]
    if not cells:
        return

    def fmt_bbox(bbox: dict | None) -> str:
        if bbox is None:
            return "-"
        return (
            f"({bbox['left']:g}, {bbox['bottom']:g}, "
            f"{bbox['right']:g}, {bbox['top']:g})"
        )

    rows = [
        (
            str(entry["index"]),
            entry["name"],
            "yes" if entry["is_top"] else "no",
            str(entry["shapes"]),
            str(entry["instances"]),
            ",".join(entry["children"]) if entry["children"] else "-",
            ",".join(entry["parents"]) if entry["parents"] else "-",
            fmt_bbox(entry["bbox_um"]),
        )
        for entry in cells
    ]
    headers = (
        "index",
        "name",
        "is_top",
        "shapes",
        "instances",
        "children",
        "parents",
        "bbox_um",
    )
    # Numeric-ish columns (index, is_top, shapes, instances) right-aligned;
    # name/children/parents/bbox_um (cols 1, 5, 6, 7) left-aligned.
    render_table(headers, rows, left_aligned={1, 5, 6, 7})
