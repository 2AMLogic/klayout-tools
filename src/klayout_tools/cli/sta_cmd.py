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
    print(f"worst_slack_ns: {report['worst_slack_ns']}")
    print(f"total_negative_slack_ns: {report['total_negative_slack_ns']}")
    print(f"fmax_mhz: {report['fmax_mhz']}")
    print(f"setup_violation_count: {report['setup_violation_count']}")
    print(f"hold_violation_count: {report['hold_violation_count']}")
    print(f"clock_skew_ns: {report['clock_skew_ns']}")
    print(f"estimated_power_mw: {report['estimated_power_mw']}")
    print()
    print(f"def_path: {report['def_path']}")
    print(f"spef_path: {report['spef_path']}")

    annotation = report.get("spef_annotation")
    if annotation is not None:
        print()
        print(
            "spef_annotation: "
            f"{annotation['design_nets_annotated']}/"
            f"{annotation['design_nets_total']} design nets annotated "
            f"(complete={annotation['annotation_complete']})"
        )
        if annotation["annotation_warning"]:
            print(f"  warning: {annotation['annotation_warning']}")
        missing_sample = annotation.get("design_nets_missing_sample")
        if missing_sample:
            print(f"  missing nets (sample): {', '.join(missing_sample)}")
