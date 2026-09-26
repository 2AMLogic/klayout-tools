"""``klt characterize`` command: serialise the single-cell NLDM
characterization report (``klayout_tools.characterize``) as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/characterize.md`` for the full table):
    0 - the cell was characterized and the ``.lib`` was written
    1 - failed to run (bad request, unresolvable netlist, pin metadata that
        does not describe a combinational cell, a simulation that did not
        complete, a grid point with no measured value, or an emitted ``.lib``
        the round-trip reader rejects) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. There is no pass/fail exit code: a characterization is data, not
a verdict -- the run either produced a complete model or it errored.)
"""

import argparse

from ..characterize import CharacterizeError, run_characterize
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = run_characterize(
            args.request,
            outdir=args.outdir,
            lib_path=args.lib,
            backend=args.backend,
            keep_artifacts=True if args.keep_artifacts else None,
        )
    except CharacterizeError as exc:
        return emit_error("characterize", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return 0


def _print_text(report: dict) -> None:
    cell = report["cell"]
    corner = report["corner"]
    grid = report["grid"]
    liberty = report["liberty"]
    simulation = report["simulation"]

    print(f"cell: {cell['name']} (.subckt {cell['subckt']})")
    print(
        f"corner: {corner['name']} "
        f"({corner['supply_v']} V, {corner['temperature_c']} C"
        + (f", {corner['process']}" if corner["process"] else "")
        + ")"
    )
    print(
        f"grid: {len(grid['input_transition_ns'])} input transitions x "
        f"{len(grid['output_load_pf'])} output loads "
        f"= {grid['points']} points x {grid['arc_count']} arc(s) "
        f"({grid['measurement_count']} measurements)"
    )
    print()

    for arc in report["arcs"]:
        side = ", ".join(
            f"{pin}={'1' if value else '0'}"
            for pin, value in sorted(arc["side_inputs"].items())
        )
        print(
            f"arc: {arc['related_pin']} -> {arc['output_pin']} "
            f"[{arc['timing_sense']}]" + (f"  side: {side}" if side else "")
        )
        for table in ("cell_rise", "cell_fall", "rise_transition", "fall_transition"):
            values = arc[table]["values"]
            flat = [value for row in values for value in row]
            print(f"  {table}: min {min(flat):.5g} ns, max {max(flat):.5g} ns")
        print()

    print(f"liberty: {liberty['path']}")
    print(f"  library_name: {liberty['library_name']}")
    roundtrip = liberty["roundtrip"]
    print(f"  roundtrip ({roundtrip['engine']}): {roundtrip['status']}")
    if roundtrip["status"] != "pass":
        print(f"    {roundtrip['message']}")
    print()
    print(f"simulation: {simulation['corner_id']} {simulation['status']}")
    engine_version = simulation["engine_version"] or ""
    print(f"  engine: {simulation['engine']} {engine_version}".rstrip())
    print(f"  runtime_s: {simulation['runtime_s']}")
    print(f"  testbench: {simulation['testbench']}")
    print(f"  report: {simulation['report']}")
