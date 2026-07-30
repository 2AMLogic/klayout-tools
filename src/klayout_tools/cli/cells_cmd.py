"""``klt cells`` command: serialise the cells report as text or JSON."""

import argparse
import json
import sys

from ..cells import CellsError, cells_report


def run(args: argparse.Namespace) -> int:
    try:
        report = cells_report(args.file, top=args.top)
    except CellsError as exc:
        print(f"klt cells: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        _print_text(report)
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
    widths = [
        max(len(headers[col]), max(len(row[col]) for row in rows))
        for col in range(len(headers))
    ]

    def fmt(row: tuple[str, ...]) -> str:
        # Numeric-ish columns (index, is_top, shapes, instances) right-aligned;
        # name/children/parents/bbox_um left-aligned.
        right_aligned = {0, 2, 3, 4}
        return "  ".join(
            row[col].rjust(widths[col])
            if col in right_aligned
            else row[col].ljust(widths[col])
            for col in range(len(headers))
        )

    print()
    print(fmt(headers))
    print("  ".join("-" * widths[col] for col in range(len(headers))))
    for row in rows:
        print(fmt(row))
