"""``klt power`` command: serialise the resistive-network extraction, static
IR-drop report, and per-net EM current-density verdict as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/power.md`` for the full table):
    0 - extraction succeeded, at least one power net resolved to at least
        one island
    1 - failed to run (bad file/spec, malformed stackup/via/power-net
        declaration, ambiguous top cell, or every declared power net
        matched no geometry at all) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)
"""

import argparse

from ..power import PowerError, run_power
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = run_power(args.file, args.spec, top=args.top)
    except PowerError as exc:
        return emit_error("power", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"spec: {report['spec']}")
    print(f"power_nets: {', '.join(report['power_nets'])}")
    print(
        f"islands: {report['island_count']}  nodes: {report['node_count']}  "
        f"edges: {report['edge_count']}"
    )

    for net_entry in report["networks"]:
        print()
        print(f"net {net_entry['net']}: {net_entry['island_count']} island(s)")
        for island in net_entry["islands"]:
            print(
                f"  {island['island_id']}: nodes={island['node_count']} "
                f"edges={island['edge_count']}"
            )

    _print_ir_drop(report)
    _print_em_verdict(report)

    warnings = report["warnings"]
    if warnings:
        print()
        print("warnings:")
        for warning in warnings:
            print(f"  {warning}")


def _print_ir_drop(report: dict) -> None:
    """The static IR-drop summary (issue #845). Silent when the spec asked
    for no solve (no `pads`, no `current_model`) -- there is nothing to say
    beyond the extraction above."""
    ir_drop = report.get("ir_drop_map")
    if ir_drop is None:
        return

    print()
    print(
        f"ir drop: {ir_drop['instance_count']} instance(s) drawing "
        f"{ir_drop['total_current_a'] * 1e3:g} mA through "
        f"{len(ir_drop['pads'])} pad(s)"
    )
    worst = ir_drop["worst_case"]
    if worst is None:
        print("  worst-case droop: (nothing solved)")
    else:
        print(
            f"  worst-case droop: {worst['droop_mv']:.6g} mV at "
            f"{worst['net']} {worst['island_id']}/{worst['node_id']} "
            f"({worst['x_um']:g}, {worst['y_um']:g}) um -> "
            f"{worst['voltage_v']:.6g} V"
        )
    if ir_drop["unsolved_current_a"]:
        print(
            f"  unsolved: {ir_drop['unsolved_current_a'] * 1e3:g} mA lands on "
            "islands with no pad"
        )

    for net_entry in ir_drop["nets"]:
        droop = net_entry["worst_case_droop_mv"]
        print(
            f"  net {net_entry['net']}: "
            f"{net_entry['solved_island_count']}/"
            f"{net_entry['solved_island_count'] + net_entry['unsolved_island_count']}"
            " island(s) solved, worst droop "
            + ("n/a" if droop is None else f"{droop:.6g} mV")
        )


def _print_em_verdict(report: dict) -> None:
    """The per-net EM current-density verdict (issue #846). Silent when
    there was no IR-drop solve at all (`em_verdict` is `None`, same
    condition as `_print_ir_drop`'s early return) -- there are no branch
    currents to check."""
    em_verdict = report.get("em_verdict")
    if em_verdict is None:
        return

    print()
    print(
        f"em verdict: {em_verdict['status'].upper()} -- "
        f"{em_verdict['checked_edge_count']} edge(s) checked, "
        f"{em_verdict['fail_count']} over limit, "
        f"{em_verdict['unchecked_edge_count']} unchecked (no declared limit "
        "or unsolved)"
    )
    worst = em_verdict["worst_case"]
    if worst is not None:
        print(
            f"  worst case: {worst['net']} {worst['island_id']}/"
            f"{worst['edge_id']} ({worst['layer']}) "
            f"{worst['current_a'] * 1e3:.6g} mA vs "
            f"{worst['current_limit_a'] * 1e3:.6g} mA limit "
            f"[{worst['status']}]"
            + (
                f" -- {worst['current_limit_source']}"
                if worst["current_limit_source"]
                else ""
            )
        )

    for net_entry in em_verdict["nets"]:
        print(
            f"  net {net_entry['net']}: {net_entry['status']} "
            f"({net_entry['fail_count']}/{net_entry['checked_edge_count']} "
            "checked edge(s) over limit)"
        )
