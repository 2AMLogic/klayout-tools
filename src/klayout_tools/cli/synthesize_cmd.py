"""``klt synthesize`` command: serialise the Yosys synthesis report as text
or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/synthesize.md`` for the full table):
    0 - synthesis succeeded, netlist written (and, with `--verify-
        equivalence`, proven equivalent to its source RTL)
    1 - failed to run (bad request, unreadable RTL source, elaboration/
        hierarchy error, unresolvable pdk.cell_library/corner, a Yosys/ABC
        engine error) -- or, with `--verify-equivalence`, a non-equivalent
        or inconclusive `klt equiv` verdict against the produced netlist --
        returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. There is no exit code 3 -- synthesis has no pass/fail concept of
its own, matching ``klt extract``'s reasoning exactly; see
``docs/design/digital-flow-contracts-spike.md`` section 4. A failed
equivalence gate is folded into exit 1 rather than reusing `klt equiv`'s own
3/4 split -- `klt synthesize` itself still has no pass/fail concept beyond
"did a netlist come out that this run trusts", see docs/cli/synthesize.md's
"Equivalence gate" section.)
"""

import argparse

from ..synthesize import SynthesizeError, run_synthesize
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = run_synthesize(
            args.request,
            pdk_variant=args.pdk,
            pdk_root=args.pdk_root,
            verify_equivalence=args.verify_equivalence,
            equiv_timeout_s=args.equiv_timeout_s,
        )
    except SynthesizeError as exc:
        return emit_error("synthesize", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return 0


def _print_text(report: dict) -> None:
    print(f"engine: {report['engine']} {report['engine_version'] or ''}".rstrip())
    print(f"hdl_toplevel: {report['hdl_toplevel']}")
    print(f"status: {report['status']}")
    print(f"instance_count: {report['instance_count']}")
    print(f"area_um2: {report['area_um2']}")
    print(f"sequential_area_um2: {report['sequential_area_um2']}")

    timing = report["timing"]
    if timing:
        target = timing["delay_target_ps"]
        print(
            f"critical_path_ps: {timing['critical_path_ps']} "
            f"(source: {timing['source']}, target: "
            f"{'none' if target is None else f'{target} ps'}, "
            f"wire_load: {timing['wire_load'] or 'none'} -- pre-layout estimate)"
        )

    instance_counts_by_type = report["instance_counts_by_type"]
    if instance_counts_by_type:
        print()
        print("instance_counts_by_type:")
        for cell_type in sorted(instance_counts_by_type):
            print(f"  {cell_type}: {instance_counts_by_type[cell_type]}")

    print()
    print(f"netlist_path: {report['netlist_path']}")
    print(f"script_path: {report['script_path']}")

    equivalence = report.get("equivalence")
    if equivalence is not None:
        print()
        print(f"equivalence: {equivalence['status']}")
