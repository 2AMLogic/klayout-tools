"""``klt gen-compose`` command: place already-generated ``klt gen`` blocks.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/gen-compose.md`` for the full table):
    0 - composition succeeded -- every block placed, every net routed;
        gds_path was written and the report is on stdout
    1 - application error: unresolvable PDK, malformed request, an
        unsupported placement.strategy, a blocks[]/connectivity[] reference
        to a nonexistent id/port, or a write failure -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    2 - usage error (missing request path, bad --format value) -- from
        argparse
    3 - partial success: every block placed, but one or more nets could not
        be routed (``unrouted_nets[]`` non-empty). The full success payload
        is still emitted on stdout -- mirrors ``klt drc``'s "ran clean but
        found violations" 3 (spike section 2, "Proposed exit codes").
"""

import argparse
import json
import os

from ..gen_compose import GenComposeError, compose
from .output import emit_error, emit_success

#: Exit code for a partial-success run (blocks placed, some nets unrouted).
PARTIAL_SUCCESS_EXIT_CODE = 3


def run(args: argparse.Namespace) -> int:
    request_dir = os.path.dirname(os.path.abspath(args.request))
    try:
        with open(args.request, encoding="utf-8") as handle:
            request = json.load(handle)
    except OSError as exc:
        return emit_error(
            "gen-compose",
            f"could not read request '{args.request}': {exc}",
            args.format,
        )
    except json.JSONDecodeError as exc:
        return emit_error(
            "gen-compose",
            f"request '{args.request}' is not valid JSON: {exc}",
            args.format,
        )

    try:
        report = compose(request, request_dir=request_dir)
    except GenComposeError as exc:
        return emit_error("gen-compose", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    # Partial success: every block placed, but the router left some nets
    # unrouted. The full payload was still emitted above; signal the
    # unrouted-net condition through the exit code (spike section 2).
    if report.get("unrouted_nets"):
        return PARTIAL_SUCCESS_EXIT_CODE
    return 0


def _print_text(report: dict) -> None:
    print(f"cell_name: {report['cell_name']}")
    print(f"gds_path: {report['gds_path']}")
    pdk = report["pdk"]
    print(f"pdk: {pdk['variant']} ({pdk['version'] or '-'})")
    bbox = report["bbox_um"]
    print(f"bbox_um: ({bbox['x0']}, {bbox['y0']}) - ({bbox['x1']}, {bbox['y1']})")

    print()
    print("blocks:")
    for block in report["blocks"]:
        offset = block["offset_um"]
        bbox = block["bbox_um"]
        print(
            f"  {block['id']} ({block['generator']})  "
            f"offset=({offset['x']}, {offset['y']})  "
            f"bbox=({bbox['x0']}, {bbox['y0']}) - ({bbox['x1']}, {bbox['y1']})"
        )

    nets = report["nets"]
    if nets:
        print()
        print("nets:")
        for net in nets:
            legs = net.get("legs") or []
            # A bundle (>2-pin) net routes as several legs (#1073) -- name the
            # count so the human summary matches the leg detail in the JSON.
            leg_note = f"  ({len(legs)} legs)" if len(legs) > 1 else ""
            if net["routed"]:
                length = net["route_length_um"]
                print(f"  {net['net']}  routed  length={length}um{leg_note}")
            else:
                print(f"  {net['net']}  UNROUTED{leg_note}")

    pins = report.get("pins") or []
    if pins:
        print()
        print("pins:")
        for pin in pins:
            state = "labelled" if pin["labelled"] else "UNLABELLED"
            print(f"  {pin['net']}  ({pin['block']}.{pin['port']})  {state}")

    drc_hints = report["drc_hints"]
    matched_groups = drc_hints.get("matched_groups") or []
    if matched_groups:
        print()
        print("matched_groups:")
        for group in matched_groups:
            blocks = ", ".join(group["blocks"])
            print(f"  {group['matched_group_id']}  ({blocks})")

    notes = drc_hints.get("notes") or []
    if notes:
        print()
        print("notes:")
        for note in notes:
            print(f"  {note}")

    warnings = report["warnings"]
    if warnings:
        print()
        print("warnings:")
        for warning in warnings:
            print(f"  {warning}")
