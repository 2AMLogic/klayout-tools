"""``klt sta`` command: serialise the standalone post-route STA/power report
(``klayout_tools.post_route_sta``) as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/sta.md`` for the full table):
    0 - the analysis completed
    1 - failed to run (bad request, unresolvable def/PDK/LEF/spef, a missing
        clock constraint, or an OpenROAD engine error) -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. There is no exit code 3 -- like ``klt place-and-route``, this
command has no pass/fail concept of its own; timing slack/violation counts
are data, not a built-in gate.)
"""

import argparse

from ..post_route_sta import PostRouteStaError, run_sta
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = run_sta(args.request, pdk_variant=args.pdk, pdk_root=args.pdk_root)
    except PostRouteStaError as exc:
        return emit_error("sta", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return 0


def _print_text(report: dict) -> None:
    print(f"engine: {report['engine']} {report['engine_version'] or ''}".rstrip())
    if report["hdl_toplevel"]:
        print(f"hdl_toplevel: {report['hdl_toplevel']}")
    print(f"status: {report['status']}")
    print()

    # Issue #1871: `request.pdk.corners` (a list) response shape -- one
    # `corners[]` entry per requested corner, printed above the
    # shared/hoisted fields (def_path/verilog_path/geometry_source/spef_path)
    # every response (single- or multi-corner) carries once, at the bottom.
    if "corners" in report:
        for entry in report["corners"]:
            print(f"corner: {entry['corner']}")
            _print_slack_fields(entry, indent="  ")
            _print_spef_annotation(entry.get("spef_annotation"), indent="  ")
            print()
    else:
        _print_slack_fields(report, indent="")

    if "corners" not in report:
        print()
    print(f"def_path: {report['def_path']}")
    print(f"verilog_path: {report['verilog_path']}")
    print(f"geometry_source: {report['geometry_source']}")
    if report["geometry_source"] == "netlist_estimate":
        print(f"wire_load_model: {report['wire_load_model']}")
        print(f"wire_load_mode: {report['wire_load_mode']}")
    print(f"spef_path: {report['spef_path']}")

    if "corners" not in report:
        _print_spef_annotation(report.get("spef_annotation"), indent="  ")


def _print_slack_fields(fields: dict, *, indent: str) -> None:
    print(f"{indent}worst_slack_ns: {fields['worst_slack_ns']}")
    print(f"{indent}total_negative_slack_ns: {fields['total_negative_slack_ns']}")
    print(f"{indent}worst_hold_slack_ns: {fields['worst_hold_slack_ns']}")
    print(
        f"{indent}total_negative_hold_slack_ns: "
        f"{fields['total_negative_hold_slack_ns']}"
    )
    print(f"{indent}fmax_mhz: {fields['fmax_mhz']}")
    print(f"{indent}setup_violation_count: {fields['setup_violation_count']}")
    print(f"{indent}hold_violation_count: {fields['hold_violation_count']}")
    print(f"{indent}clock_skew_ns: {fields['clock_skew_ns']}")
    print(f"{indent}estimated_power_mw: {fields['estimated_power_mw']}")


def _print_spef_annotation(annotation: dict | None, *, indent: str) -> None:
    if annotation is None:
        return
    print()
    print(
        "spef_annotation: "
        f"{annotation['design_nets_annotated']}/"
        f"{annotation['design_nets_total']} design nets annotated "
        f"(complete={annotation['annotation_complete']})"
    )
    print(f"{indent}delay_changed: {annotation.get('delay_changed')}")
    print(f"{indent}reader_warning_count: {annotation.get('reader_warning_count')}")
    print(
        f"{indent}unannotated_driver_count: "
        f"{annotation.get('unannotated_driver_count')} "
        f"(partial: {annotation.get('partially_unannotated_driver_count')})"
    )
    if annotation["annotation_warning"]:
        print(f"{indent}warning: {annotation['annotation_warning']}")
    missing_sample = annotation.get("design_nets_missing_sample")
    if missing_sample:
        print(f"{indent}missing nets (sample): {', '.join(missing_sample)}")
    warning_sample = annotation.get("reader_warning_sample")
    if warning_sample:
        print(f"{indent}reader warnings (sample):")
        for line in warning_sample:
            print(f"{indent}  {line}")
