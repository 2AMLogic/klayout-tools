"""``klt power`` command: serialise the resistive-network extraction report
as text or JSON.

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

    warnings = report["warnings"]
    if warnings:
        print()
        print("warnings:")
        for warning in warnings:
            print(f"  {warning}")
