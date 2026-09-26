"""``klt characterize`` command: serialise the NLDM characterization report
(``klayout_tools.characterize``) as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/characterize.md`` for the full table):
    0 - every requested cell was characterized and the ``.lib`` was written
        (with ``--compare-to``, regardless of whether the comparison is
        within tolerance -- the comparison is data, reported in the
        response's ``comparison`` block)
    1 - failed to run (bad request, unresolvable netlist, pin metadata that
        does not describe a combinational cell, a simulation that did not
        complete, a grid point with no measured value, an emitted ``.lib``
        the round-trip reader rejects, or an unreadable ``--compare-to``
        library) -- returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. There is no pass/fail exit code: a characterization is data, not
a verdict -- the run either produced a complete model or it errored.)
"""

import argparse

from ..characterize import TABLE_NAMES, CharacterizeError, run_characterize
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = run_characterize(
            args.request,
            outdir=args.outdir,
            lib_path=args.lib,
            backend=args.backend,
            keep_artifacts=True if args.keep_artifacts else None,
            cells=getattr(args, "cell", None),
            compare_to=getattr(args, "compare_to", None),
        )
    except CharacterizeError as exc:
        return emit_error("characterize", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return 0


_UNIT = {
    "cell_rise": "ns",
    "cell_fall": "ns",
    "rise_transition": "ns",
    "fall_transition": "ns",
    "rise_power": "pJ",
    "fall_power": "pJ",
}


def _print_text(report: dict) -> None:
    corner = report["corner"]
    grid = report["grid"]
    liberty = report["liberty"]

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
        f"({grid['total_measurement_count']} measurements)"
    )
    print()

    for entry in report["cells"]:
        _print_cell(entry)

    print(f"liberty: {liberty['path']}")
    print(f"  library_name: {liberty['library_name']}")
    print(f"  cell_count: {liberty['cell_count']}")
    roundtrip = liberty["roundtrip"]
    print(f"  roundtrip ({roundtrip['engine']}): {roundtrip['status']}")
    if roundtrip["status"] != "pass":
        print(f"    {roundtrip['message']}")

    comparison = report.get("comparison")
    if comparison is not None:
        print()
        _print_comparison(comparison)


def _print_cell(entry: dict) -> None:
    cell = entry["cell"]
    simulation = entry["simulation"]
    print(f"cell: {cell['name']} (.subckt {cell['subckt']})")
    for arc in entry["arcs"]:
        side = ", ".join(
            f"{pin}={'1' if value else '0'}"
            for pin, value in sorted(arc["side_inputs"].items())
        )
        print(
            f"  arc: {arc['related_pin']} -> {arc['output_pin']} "
            f"[{arc['timing_sense']}]" + (f"  side: {side}" if side else "")
        )
        for table in TABLE_NAMES:
            values = arc[table]["values"]
            flat = [value for row in values for value in row]
            unit = _UNIT[table]
            print(
                f"    {table}: min {min(flat):.5g} {unit}, max {max(flat):.5g} {unit}"
            )
    leakage = entry["leakage"]
    print(f"  cell_leakage_power: {leakage['cell_leakage_power_pw']:.5g} pW")
    for state in leakage["states"]:
        print(f"    when {state['when']}: {state['value_pw']:.5g} pW")
    print(
        f"  simulation: {simulation['corner_id']} {simulation['status']} "
        f"({simulation['runtime_s']} s, "
        f"{simulation['engine']} {simulation['engine_version'] or ''}".rstrip()
        + ")"
    )
    print(f"    testbench: {simulation['testbench']}")
    print(f"    report: {simulation['report']}")
    print()


def _print_comparison(comparison: dict) -> None:
    summary = comparison["summary"]
    print(f"comparison against: {comparison['reference']}")
    for name, field in summary["fields"].items():
        tolerance = field["tolerance"]
        bound = f"rel {tolerance['rel']:g} / abs {tolerance['abs']:g}"
        if not field["compared"]:
            print(f"  {name}: NOT COMPARED")
            continue
        max_rel = field["max_rel_delta"]
        rel_text = f"{max_rel * 100:.1f}%" if max_rel is not None else "n/a"
        verdict = "ok" if field["within_tolerance"] else "OUT OF TOLERANCE"
        print(
            f"  {name}: {field['compared']} point(s), max |rel| {rel_text}, "
            f"max |abs| {field['max_abs_delta']:.4g}, "
            f"{field['violations']} outside ({bound}) -- {verdict}"
        )
    for entry in summary["missing"]:
        print(f"  missing: {entry}")
    print("  within_tolerance: " + ("yes" if summary["within_tolerance"] else "no"))
