"""``klt arith-gen`` command: emit a parallel-prefix adder (plus its
behavioural reference, a self-checking testbench, a Yosys ``techmap`` rule
file, and a ready-to-run ``klt equiv`` request) from a named architecture or
an explicit cell map -- issue #1722.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes:
    0 - the adder was generated and every artifact written
    1 - failed to generate (bad width, unknown architecture, malformed or
        illegal cell map, unwritable output directory) -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. There is no exit code 3: this command has no pass/fail verdict
of its own -- proving the emitted Verilog correct is ``klt equiv``'s job,
via the request file this command writes.)
"""

from __future__ import annotations

import argparse

from ..arith_gen import ArithGenError, run_arith_gen
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = run_arith_gen(
            width=args.width,
            architecture=args.arch,
            cell_map_path=args.cell_map,
            module_name=args.module_name,
            output_dir=args.output_dir,
            include_cell_map=args.include_cell_map,
        )
    except ArithGenError as exc:
        return emit_error("arith-gen", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _print_text(report: dict) -> None:
    print(f"architecture: {report['architecture']}")
    print(f"width: {report['width']}")
    print(f"module_name: {report['module_name']}")
    print(f"prefix_cells: {report['prefix_cells']}")
    print(f"logic_levels: {report['logic_levels']}")
    print(f"max_fanout: {report['max_fanout']}")
    print()
    print(f"verilog_path: {report['verilog_path']}")
    print(f"reference_path: {report['reference_path']}")
    print(f"testbench_path: {report['testbench_path']}")
    print(f"techmap_path: {report['techmap_path']}")
    print(f"equiv_request_path: {report['equiv_request_path']}")

    cell_map = report.get("cell_map")
    if cell_map is not None:
        print()
        print("cell_map (row i = MSB, column j = LSB; 1 = prefix node (i, j)):")
        for i, row in enumerate(cell_map):
            print(f"  {i:>3}: {''.join(str(entry) for entry in row)}")
