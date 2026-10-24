"""``klt place-and-route`` command: serialise the OpenROAD place-and-route
report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/place-and-route.md`` for the full table):
    0 - place-and-route reached (at least) the requested target_stage
        (for a `pdk.rebuild_corners` request: in every corner)
    1 - failed to run (bad request, unresolvable netlist/PDK/LEF, a
        floorplan spec with more than one method set, or an OpenROAD engine
        error that stops the run before reaching the requested
        target_stage) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``; also a `pdk.rebuild_corners` request in
        which any corner's unit failed (the aggregated report is still
        emitted, with the failure in that corner's `corner_runs` entry)
    3 - ``--check`` only: the committed report no longer reproduces
        (``status: "drifted"``)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. A *fresh* run never exits 3 -- place-and-route has no pass/fail
concept of its own; timing slack/violation counts are data, not a built-in
gate. See ``docs/design/digital-flow-contracts-spike.md`` section 5.)

``--check <report>`` (issue #2224) switches this verb from running a fresh
flow to *verifying a previously committed one* still reproduces from the
request given positionally -- see ``docs/cli/place-and-route.md``,
"--check". Cheap mode (default): re-resolve and re-hash the request's
gate-level netlist and liberty and compare against the recorded
``provenance`` digests, no OpenROAD run. Full mode (``--check <report>
--rerun``): actually re-run the flow and diff verdict-bearing fields.
"""

import argparse
import sys

from ..output_lock import OutputLockError
from ..place_and_route import (
    PlaceAndRouteError,
    check_place_and_route_report,
    rerun_place_and_route_report,
    run_place_and_route,
)
from .output import emit_error, emit_success, render_check_text, render_rerun_drift

#: Aliases for the --check/--rerun outcome (issue #2224) -- the same 0/3
#: split `klt drc --check` uses. This verb has no exit 3 of its own (see this
#: module's docstring: place-and-route has no built-in pass/fail gate), so 3
#: is unambiguous here: it means, and only means, "the committed report no
#: longer reproduces".
EXIT_MATCH = 0
EXIT_DRIFTED = 3


def run(args: argparse.Namespace) -> int:
    if args.check is not None:
        return _run_check(args)

    if args.rerun:
        return emit_error(
            "place-and-route", "--rerun requires --check <report>", args.format
        )

    try:
        report = run_place_and_route(
            args.request,
            pdk_variant=args.pdk,
            pdk_root=args.pdk_root,
            backend=args.backend,
        )
    except PlaceAndRouteError as exc:
        return _emit_flow_error(exc, args.format)
    except OutputLockError as exc:
        return emit_error("place-and-route", str(exc), args.format, code=exc.code)

    emit_success(report, args.format, _print_text)
    if "corner_runs" in report:
        # Issue #3507: a `pdk.rebuild_corners` request emits its aggregated
        # report either way; a unit that failed still fails the command.
        # Each unit's own #2086 warnings go to stderr too, corner-prefixed.
        for run in report["corner_runs"]:
            for warning in run.get("warnings", ()):
                print(
                    f"klt place-and-route: warning: [{run['corner']}] {warning}",
                    file=sys.stderr,
                )
        return 0 if report["status"] == "ok" else 1
    # Issue #2086: the response's own `warnings` list also goes to stderr,
    # in *both* formats. A run that placed no tapcells, no PDN and no
    # fillers is the failure mode this exists for -- it completes, exits 0,
    # and its output looks exactly like data -- so the complaint has to be
    # visible to an operator who never opens the JSON. stderr, not stdout:
    # `--format json`'s stdout stays a single parseable document (see
    # `output.py`'s own envelope note), and the exit code is unchanged.
    for warning in report.get("warnings", ()):
        print(f"klt place-and-route: warning: {warning}", file=sys.stderr)

    return 0


def _emit_flow_error(exc: PlaceAndRouteError, fmt: str) -> int:
    """``emit_error`` carrying the batch backend's ``code``/``details``
    (issue #3518); both are ``None`` for every local failure."""
    return emit_error(
        "place-and-route", str(exc), fmt, code=exc.code, details=exc.details
    )


def _run_check(args: argparse.Namespace) -> int:
    """`--check <report>` (and `--rerun`): verify committed flow evidence
    against the request still on disk (issue #2224).

    The positional `request` stays *required*: a `klt place-and-route` report
    echoes its output artifacts and provenance hashes but never the request
    that produced it. See `_report_verify.py`'s module docstring.
    """
    try:
        if args.rerun:
            result = rerun_place_and_route_report(
                args.check,
                args.request,
                pdk_variant=args.pdk,
                pdk_root=args.pdk_root,
                backend=args.backend,
            )
        else:
            result = check_place_and_route_report(
                args.check,
                args.request,
                pdk_variant=args.pdk,
                pdk_root=args.pdk_root,
            )
    except PlaceAndRouteError as exc:
        return _emit_flow_error(exc, args.format)
    except OutputLockError as exc:
        return emit_error("place-and-route", str(exc), args.format, code=exc.code)

    text_renderer = render_rerun_drift if args.rerun else render_check_text
    emit_success(result, args.format, text_renderer)

    return EXIT_MATCH if result["status"] == "match" else EXIT_DRIFTED


def _print_corner_runs_text(report: dict) -> None:
    """`--format text` rendering of a `pdk.rebuild_corners` report (#3507)."""
    print(f"engine: {report['engine']} {report['engine_version'] or ''}".rstrip())
    print(f"hdl_toplevel: {report['hdl_toplevel']}")
    print(f"status: {report['status']}")
    print()
    print("corner_runs:")
    for run in report["corner_runs"]:
        line = f"  {run['corner']}: {run['status']}"
        if run["status"] == "ok":
            line += f", worst_slack_ns={run['worst_slack_ns']}"
        else:
            line += f", error={run['error']}"
        print(f"{line} ({run['output_dir']})")


def _print_text(report: dict) -> None:
    if "corner_runs" in report:
        _print_corner_runs_text(report)
        return
    print(f"engine: {report['engine']} {report['engine_version'] or ''}".rstrip())
    print(f"hdl_toplevel: {report['hdl_toplevel']}")
    print(f"status: {report['status']}")
    print(f"stage_reached: {report['stage_reached']}")
    print(f"seed: {report['seed']}")
    print()
    print(f"die_area_um2: {report['die_area_um2']}")
    print(f"core_area_um2: {report['core_area_um2']}")
    print(f"utilization_pct: {report['utilization_pct']}")
    print(f"wirelength_um: {report['wirelength_um']}")
    print(f"worst_slack_ns: {report['worst_slack_ns']}")
    print(f"total_negative_slack_ns: {report['total_negative_slack_ns']}")
    print(f"fmax_mhz: {report['fmax_mhz']}")
    print(f"setup_violation_count: {report['setup_violation_count']}")
    print(f"hold_violation_count: {report['hold_violation_count']}")
    print(f"estimated_power_mw: {report['estimated_power_mw']}")
    print(f"clock_skew_ns: {report['clock_skew_ns']}")

    stages = report["stages"]
    if stages:
        print()
        print("stages:")
        for stage in stages:
            fields = ", ".join(
                f"{key}={value}" for key, value in stage.items() if key != "name"
            )
            print(f"  {stage['name']}: {fields}")

    # Issue #2086, suggestion 3: the placed power-delivery counts appear in
    # the run summary *regardless* of whether they are a problem, so a
    # power-less run is visible in the artifact rather than only in the
    # absence of a complaint. `null` counts (unavailable evidence) render as
    # `unknown`, never as `0`.
    placed = report["power"]["placed"]
    print()
    print(f"power delivery ({placed['status']}, evidence: {placed['evidence']}):")
    for label, key in (
        ("tapcells", "tapcells"),
        ("endcaps", "endcaps"),
        ("fillers", "fillers"),
    ):
        value = placed[key]
        print(f"  {label}: {'unknown' if value is None else value}")
    nets = placed["special_nets"]
    if nets is None:
        print("  pdn_special_nets: unknown")
    else:
        print(f"  pdn_special_nets: {len(nets)}")
        for net in nets:
            print(
                f"    {net['name']}: {net['followpin_segments']} followpin, "
                f"{net['stripe_segments']} stripe, {net['vias']} via"
            )

    print()
    print(f"def_path: {report['def_path']}")
    print(f"gds_path: {report['gds_path']}")
    print(f"verilog_path: {report['verilog_path']}")
