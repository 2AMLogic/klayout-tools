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

from ..env_provenance import render_path_field
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
            if "interconnect_corner" in entry:
                print(f"interconnect_corner: {entry['interconnect_corner']}")
                print(f"spef_path: {_render_input_path(entry['spef_path'])}")
            _print_slack_fields(entry, indent="  ")
            _print_spef_annotation(entry.get("spef_annotation"), indent="  ")
            print()
    else:
        _print_slack_fields(report, indent="")

    if "corners" not in report:
        print()
    print(f"def_path: {_render_input_path(report['def_path'])}")
    print(f"verilog_path: {_render_input_path(report['verilog_path'])}")
    print(f"geometry_source: {report['geometry_source']}")
    print(f"clock_mode: {report['clock_mode']}")
    if report["geometry_source"] == "netlist_estimate":
        print(f"wire_load_model: {report['wire_load_model']}")
        print(f"wire_load_mode: {report['wire_load_mode']}")
    print(f"spef_path: {_render_input_path(report['spef_path'])}")

    if "corners" not in report:
        _print_spef_annotation(report.get("spef_annotation"), indent="  ")


def _render_input_path(value) -> str:
    """Issue #2879: `def_path`/`verilog_path`/`spef_path` are `{path, scope}`
    envelopes (schema 2) -- rendered through the same `render_path_field`
    helper `klt sim`/`klt pex` use, so an input outside the repository
    prints `<outside repo>`, never an absolute host path. `None` (the input
    is not used by this request) keeps its pre-#2879 `None` rendering."""
    if value is None:
        return "None"
    if isinstance(value, str):
        return value
    return render_path_field(value)


def _print_path_record(label: str, path: dict | None) -> None:
    if not path:
        return
    if path.get("startpoint") is None:
        print(f"{label}: {path.get('status')}")
    else:
        print(
            f"{label}: {path['check_type']} {path['startpoint']} -> "
            f"{path['endpoint']} slack_ns={path['slack_ns']} ({path['status']})"
        )


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
    _print_constraint_completeness(fields.get("constraint_completeness"), indent=indent)
    for key in ("worst_setup_path", "worst_hold_path"):
        _print_path_record(f"{indent}{key}", fields.get(key))
    for port, entry in (fields.get("port_paths") or {}).items():
        for side in ("setup", "hold"):
            _print_path_record(f"{indent}port_paths[{port}].{side}", entry[side])
    print(f"{indent}clock_skew_ns: {fields['clock_skew_ns']}")
    propagation = fields.get("clock_propagation")
    if propagation is not None:
        print(
            f"{indent}clock_propagation: sinks={propagation['clock_sinks']} "
            f"max_network_latency_ns={propagation['max_network_latency_ns']}"
        )
        if propagation["clock_sinks_sample"]:
            print(
                f"{indent}  sinks (sample): "
                f"{', '.join(propagation['clock_sinks_sample'])}"
            )
    print(f"{indent}estimated_power_mw: {fields['estimated_power_mw']}")
    # Issue #2886: the per-corner SDF, only when `request.sdf` asked for one
    # (text output for a request without it stays unchanged).
    sdf = fields.get("sdf")
    if sdf is not None:
        print(f"{indent}sdf: {_render_input_path(sdf)} ({sdf['content_hash']})")


def _print_constraint_completeness(block: dict | None, *, indent: str) -> None:
    """Issue #2780: one summary line plus a sample line per check that found
    offending pins. A check that could not run prints its status
    (``unavailable``) in place of a count -- never ``0``."""
    if not block:
        return
    keys = ("unconstrained_endpoints", "unclocked_registers")
    summary = ", ".join(
        f"{key}="
        + str(
            block[key]["count"]
            if block[key]["status"] == "ok"
            else block[key]["status"]
        )
        for key in keys
    )
    print(f"{indent}constraint_completeness: {block['status']} ({summary})")
    for key in keys:
        check = block[key]
        if check["sample"]:
            more = f" ... ({check['count']} total)" if check["truncated"] else ""
            print(f"{indent}  {key} (sample): {', '.join(check['sample'])}{more}")


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
