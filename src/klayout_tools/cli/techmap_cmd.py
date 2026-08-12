"""``klt techmap`` command: serialise a `native/techmap` technology-mapping
run (issue #874) as text or JSON, optionally gated by `klt equiv` against
the pre-mapping generic netlist (issue #875).

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/design/synth-techmap-stage-contract.md`` section 6
for the underlying Rust binary's own table):
    0 - mapping succeeded (and, with `--verify-equivalence`, proven
        equivalent to its pre-mapping generic netlist)
    1 - failed to run (bad request, unbuilt `klt-techmap` binary, malformed
        generic netlist, an unrealised generic cell, unreadable/unparseable
        liberty) -- or, with `--verify-equivalence`, a non-equivalent or
        inconclusive `klt equiv` verdict against the mapped netlist --
        returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. There is no exit code 3, mirroring ``klt synthesize``'s
identical reasoning: this stage has no pass/fail concept of its own beyond
"did a mapped netlist come out that this run trusts".)
"""

import argparse

from ..techmap import TechmapError, run_techmap
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = run_techmap(
            args.request,
            verify_equivalence=args.verify_equivalence,
            equiv_timeout_s=args.equiv_timeout_s,
        )
    except TechmapError as exc:
        return emit_error("techmap", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return 0


def _print_text(report: dict) -> None:
    print(f"top: {report['top']}")
    print(f"status: {report['status']}")
    print(f"instance_count: {report['instance_count']}")
    print(f"area_um2: {report['area_um2']}")

    instance_counts_by_type = report["instance_counts_by_type"]
    if instance_counts_by_type:
        print()
        print("instance_counts_by_type:")
        for cell_type in sorted(instance_counts_by_type):
            print(f"  {cell_type}: {instance_counts_by_type[cell_type]}")

    print()
    print(f"mapped_netlist_path: {report['mapped_netlist_path']}")
    print(f"generic_netlist_verilog_path: {report['generic_netlist_verilog_path']}")

    equivalence = report.get("equivalence")
    if equivalence is not None:
        print()
        print(f"equivalence: {equivalence['status']}")
