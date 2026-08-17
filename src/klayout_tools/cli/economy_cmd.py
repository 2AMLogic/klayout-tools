"""``klt economy`` command: serialise the layout-economy report as text or JSON."""

import argparse

from ..economy import EconomyError, economy_report
from .output import emit_error, emit_success, render_table


def run(args: argparse.Namespace) -> int:
    try:
        report = economy_report(
            args.file,
            top=args.top,
            grid_cols=args.grid_cols,
            grid_rows=args.grid_rows,
            max_empty_regions=args.max_empty_regions,
            budget_um2=args.budget_um2,
            reference_area_um2=args.reference_area_um2,
            area_eff_max_dead_margin_um=args.area_eff_max_dead_margin_um,
            area_eff_min_utilization=args.area_eff_min_utilization,
            area_eff_max_empty_region_fraction=(
                args.area_eff_max_empty_region_fraction
            ),
            area_eff_require_bbox_tightness=args.area_eff_require_bbox_tightness,
        )
    except EconomyError as exc:
        return emit_error("economy", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _fmt_bbox(bbox: dict) -> str:
    return (
        f"({bbox['left']:g}, {bbox['bottom']:g}) - ({bbox['right']:g}, {bbox['top']:g})"
    )


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"top: {report['top']}")
    print(f"dbu_um: {report['dbu_um']}")
    bbox = report["bbox_um"]
    print(
        f"bbox_um: {_fmt_bbox(bbox)}  {bbox['width']:g} x {bbox['height']:g}"
        f"  ({report['bbox_area_um2']:.3f} um2 / {report['bbox_area_mm2']:.6f} mm2)"
    )
    print(f"aspect_ratio: {report['aspect_ratio']}")
    print(f"utilization: {report['utilization']:.4f}")
    print(f"bbox_tightness: {report['bbox_tightness']:.4f}")
    m = report["dead_margins_um"]
    print(
        f"dead_margins_um: left={m['left']:.3f} right={m['right']:.3f} "
        f"bottom={m['bottom']:.3f} top={m['top']:.3f}"
    )
    print(
        f"whitespace: {report['whitespace_area_um2']:.3f} um2 "
        f"({report['whitespace_fraction']:.4f} fraction)"
    )
    print(
        f"empty_regions: showing {len(report['largest_empty_regions'])} of "
        f"{report['empty_region_count']}"
    )
    for region in report["largest_empty_regions"][:5]:
        rb = region["bbox_um"]
        print(
            f"  {_fmt_bbox(rb)}  area={region['area_um2']:.3f} um2  "
            f"fill={region['fill_fraction']:.3f}"
        )

    row_util = report["row_utilization"]
    if row_util is not None:
        print(
            f"row_utilization: {row_util['row_count']} rows, "
            f"mean={row_util['mean_utilization']:.4f}"
        )

    budget = report.get("budget")
    if budget is not None:
        print(
            f"budget: {budget['status']} "
            f"(actual={budget['actual_um2']:.3f} um2, "
            f"budget={budget['budget_um2']:.3f} um2, "
            f"margin={budget['margin_um2']:.3f} um2)"
        )

    reference = report.get("reference")
    if reference is not None:
        print(
            f"reference: {reference['ratio']:.3f}x "
            f"(actual={reference['actual_area_um2']:.3f} um2, "
            f"reference={reference['reference_area_um2']:.3f} um2)"
        )

    area_eff = report.get("area_eff")
    if area_eff is not None:
        print(f"area_eff: {area_eff['status']}")
        checks = area_eff["checks"]
        if "dead_margins" in checks:
            c = checks["dead_margins"]
            print(
                f"  dead_margins: {c['status']} "
                f"(actual_max={c['actual_max_um']:.3f} um, "
                f"max_allowed={c['max_allowed_um']:.3f} um)"
            )
        if "utilization" in checks:
            c = checks["utilization"]
            print(
                f"  utilization: {c['status']} "
                f"(actual={c['actual']:.4f}, floor={c['floor']:.4f})"
            )
        if "largest_empty_region_fraction" in checks:
            c = checks["largest_empty_region_fraction"]
            print(
                f"  largest_empty_region_fraction: {c['status']} "
                f"(actual={c['actual_fraction']:.4f}, "
                f"max_allowed={c['max_allowed_fraction']:.4f})"
            )
        if "bbox_tightness" in checks:
            c = checks["bbox_tightness"]
            print(f"  bbox_tightness: {c['status']} (actual={c['actual']:.4f})")

    cells = report["cells"]
    if not cells:
        return
    rows = [
        (
            entry["name"],
            f"{entry['drawn_area_um2']:.3f}",
            f"{entry['bbox_area_um2']:.3f}",
            f"{entry['utilization']:.4f}",
        )
        for entry in cells
    ]
    headers = ("cell", "drawn_area_um2", "bbox_area_um2", "utilization")
    render_table(headers, rows, left_aligned={0})
