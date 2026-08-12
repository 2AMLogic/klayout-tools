"""``klt mom`` command: serialise the Method-of-Moments / PEEC report as text
or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/mom.md`` for the full table):
    0 - solve succeeded, capacitance matrix (and, in PEEC mode, the partial
        inductance matrix and DC resistances) returned
    1 - failed to run (bad file/spec, native extension not installed, a
        stackup entry matching no shapes, or a solver-level failure such as
        a singular potential-coefficient matrix) -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)
"""

import argparse

from ..mom import MomError, run_mom
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = run_mom(args.file, args.spec, top=args.top)
    except MomError as exc:
        return emit_error("mom", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"spec: {report['spec']}")
    print(f"background_permittivity: {report['background_permittivity']}")
    print(f"panel_size_um: {report['panel_size_um']}")
    print(f"panel_count: {report['panel_count']}")
    if report.get("filament_count"):
        print(f"filament_subdivisions: {report['filament_subdivisions']}")
        print(f"filament_count: {report['filament_count']}")

    conductors = report["conductors"]
    print(f"conductors: {len(conductors)}")
    if not conductors:
        return

    name_width = max(len("conductor"), max(len(name) for name in conductors))
    _print_matrix(
        conductors, report["capacitance_matrix_ff"], name_width, "(femtofarads)"
    )

    inductance = report.get("inductance_matrix_nh")
    if inductance is not None:
        _print_matrix(conductors, inductance, name_width, "(nanohenries, partial)")

    resistance = report.get("resistance_ohm")
    if resistance is not None:
        print()
        print("DC resistance (ohms):")
        for name, value in zip(conductors, resistance, strict=True):
            print(f"  {name.ljust(name_width)}  {value:12.6g}")

    warnings = report["warnings"]
    if warnings:
        print()
        print("warnings:")
        for warning in warnings:
            print(f"  {warning}")


def _print_matrix(
    conductors: list[str], matrix: list[list[float]], name_width: int, units: str
) -> None:
    header = " " * name_width + "  " + "  ".join(name.rjust(12) for name in conductors)
    print()
    print(header)
    for name, row in zip(conductors, matrix, strict=True):
        cells = "  ".join(f"{value:12.6g}" for value in row)
        print(f"{name.ljust(name_width)}  {cells}")
    print(units)
